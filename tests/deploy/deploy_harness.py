"""A scripted Docker host for the deploy.py file-choreography tests.

The pure-logic tests in ``test_deploy.py`` stand on the argument that a mock of Docker
proves nothing about Docker. That still holds: whether compose accepts the file is proven by
the real deployment. The file choreography, though -- which compose file a rollback uses,
what lands in ``backup/``, what a crash between two renames leaves behind -- is ours, and a
fake Docker does prove it. So this module provides:

* ``FakeDocker``: a replacement for ``deploy.run`` that records every command and answers
  ``pull``, ``image inspect``, ``compose ps/up/down``, ``inspect``, the backup ``exec``,
  ``cp`` and ``rm`` from a scripted host state. It refuses what real compose would refuse:
  a compose file that does not exist, a missing ``env_file``, an unset required variable.
* ``RecordingLock``: records every directory ``deployment_lock`` is taken in. On POSIX it
  delegates to the real ``fcntl`` lock, so CI exercises it on every deployment; elsewhere
  it only records, because ``fcntl`` does not exist there.
* ``Host``: a temporary ``$HOME`` holding ``portfolio-app`` and/or ``portfolio-app-deploy``,
  an upload directory standing in for the one the runner copies ``deploy.py`` into, and
  builders for a legacy host as the pre-#94 script left it.
* ``FilesystemTap``: counts deploy.py's own filesystem mutations and can kill the
  "process" before the k-th one, to prove a crash at any point leaves a state the next
  deployment recovers from.

Plain standard library, like everything under ``tests/deploy``: the gate runs these on
Windows as well as on Linux CI.
"""

from __future__ import annotations

import builtins
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_PY = REPO_ROOT / "deploy" / "deploy.py"
KIT_COMPOSE = REPO_ROOT / "deploy" / "compose.yml"
POSIX = os.name == "posix"

# Stands in for exchange credentials in secrets.env. It must never appear anywhere the
# script writes, prints or passes to Docker.
SENTINEL_SECRET = b"SENTINEL_VALUE=never-leave-secrets-env-4417\n"

LEGACY_ATTEMPT_IDS = (
    "20260901T101500Z-0123456789ab",
    "20260910T101500Z-123456789abc",
    "20260920T101500Z-23456789abcd",
)


def load_deploy() -> types.ModuleType:
    """Load deploy/deploy.py once, sharing the module object test_deploy.py registers."""
    existing = sys.modules.get("deploy")
    if existing is not None and Path(existing.__file__ or "").resolve() == DEPLOY_PY.resolve():
        return existing
    spec = importlib.util.spec_from_file_location("deploy", DEPLOY_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy"] = module
    spec.loader.exec_module(module)
    return module


deploy = load_deploy()
REAL_DEPLOYMENT_LOCK = deploy.deployment_lock
REAL_PREPARE_SECRETS = deploy.prepare_secrets_env_file


@dataclass(frozen=True)
class Release:
    """One build the pipeline could deploy. ``number`` picks every identifying value."""

    number: int

    def __post_init__(self) -> None:
        assert 1 <= self.number <= 15, "one hex digit per release keeps the digests readable"

    @property
    def digit(self) -> str:
        return format(self.number, "x")

    @property
    def image(self) -> str:
        return f"ghcr.io/emanuelturtula/portfolio@sha256:{self.digit * 64}"

    @property
    def revision(self) -> str:
        return (self.digit * 2 + "0") * 13 + self.digit

    @property
    def version(self) -> str:
        return f"v1.{self.number}.0"

    @property
    def run_number(self) -> int:
        return 100 + self.number

    @property
    def source_run_url(self) -> str:
        return f"https://github.com/emanuelturtula/portfolio/actions/runs/{5000 + self.number}"

    @property
    def labels(self) -> dict[str, str]:
        return {
            "org.opencontainers.image.revision": self.revision,
            "org.opencontainers.image.version": self.version,
        }

    @property
    def compose(self) -> bytes:
        """The kit compose file this release ships: the real one, told apart by a marker."""
        return KIT_COMPOSE.read_bytes() + f"# test release {self.number}\n".encode()

    def arguments(self, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "environment": "prod",
            "image": self.image,
            "revision": self.revision,
            "version": self.version,
            "run_number": self.run_number,
            "source_run_url": self.source_run_url,
        }
        arguments.update(overrides)
        return arguments


@dataclass
class Container:
    id: str
    image: str
    health: str


@dataclass
class Call:
    """One command deploy.py ran, with what compose would have seen."""

    kind: str
    argv: tuple[str, ...]
    env: dict[str, str]
    project: str | None = None
    compose_file: str | None = None
    compose_bytes: bytes | None = None
    compose_args: tuple[str, ...] = ()

    @property
    def image(self) -> str | None:
        return self.env.get("PORTFOLIO_IMAGE")


UP_ARGS = ("up", "--detach", "--wait", "--wait-timeout", "180", "app")


class FakeDocker:
    """Answers deploy.run() from a scripted host state and records every command."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.published: dict[str, dict[str, str]] = {}
        self.containers: dict[str, Container] = {}
        self.running: str | None = None
        self.database: bytes | None = b"live database, never deployed\n"
        self.snapshots: list[bytes] = []
        self.inner_files: dict[str, bytes] = {}
        self.fail_up: set[str] = set()
        self.unhealthy: set[str] = set()
        self.substitute: dict[str, str] = {}
        self.fail_backup = False
        self.unexpected: list[tuple[str, ...]] = []
        self.on_healthy: Callable[[Container], None] | None = None
        self._counter = 0

    # -- scripting ----------------------------------------------------------------------

    def publish(self, release: Release) -> None:
        self.published[release.image] = dict(release.labels)

    def start(self, image: str, health: str = "healthy") -> Container:
        self._counter += 1
        container = Container(f"container{self._counter:04d}", image, health)
        self.containers[container.id] = container
        self.running = container.id
        return container

    @property
    def running_image(self) -> str | None:
        return self.containers[self.running].image if self.running else None

    def kinds(self) -> list[str]:
        return [call.kind for call in self.calls]

    def of_kind(self, kind: str) -> list[Call]:
        return [call for call in self.calls if call.kind == kind]

    # -- the fake -----------------------------------------------------------------------

    def __call__(self, args: list[str], *, env: dict[str, str] | None = None) -> str:
        argv = tuple(args)
        portfolio_env = {k: v for k, v in (env or {}).items() if k.startswith("PORTFOLIO_")}
        if argv[:2] == ("docker", "compose"):
            return self._compose(argv, portfolio_env)
        call = Call(kind="unexpected", argv=argv, env=portfolio_env)
        self.calls.append(call)
        if argv[:2] == ("docker", "pull") and len(argv) == 3:
            call.kind = "pull"
            if argv[2] not in self.published:
                raise deploy.DeploymentError("manifest unknown")
            return ""
        if argv[:3] == ("docker", "image", "inspect") and len(argv) == 4:
            call.kind = "image-inspect"
            labels = self.published[argv[3]]
            return json.dumps([{"Config": {"Labels": labels}}])
        if argv[:2] == ("docker", "inspect") and len(argv) == 3:
            call.kind = "inspect"
            container = self.containers[argv[2]]
            if container.health == "healthy" and self.on_healthy is not None:
                self.on_healthy(container)
            return json.dumps(
                [
                    {
                        "Config": {"Image": container.image},
                        "State": {"Health": {"Status": container.health}},
                    }
                ]
            )
        if argv[:2] == ("docker", "exec") and argv[3:5] == ("python", "-c") and len(argv) == 7:
            call.kind = "exec-backup"
            if argv[2] != self.running:
                raise deploy.DeploymentError("No such container")
            if self.fail_backup:
                raise deploy.DeploymentError("database disk image is malformed")
            if self.database is None:
                return "absent"
            self.inner_files[argv[6]] = self.database
            self.snapshots.append(self.database)
            return "ok"
        if argv[:2] == ("docker", "cp") and len(argv) == 4:
            call.kind = "cp"
            container_id, _, inner = argv[2].partition(":")
            destination = Path(argv[3])
            if container_id != self.running or inner not in self.inner_files:
                raise deploy.DeploymentError("Could not find the file in the container")
            if not destination.parent.is_dir():
                raise deploy.DeploymentError("no such directory")
            destination.write_bytes(self.inner_files[inner])
            # docker cp keeps the container file's mode, not the host umask; sqlite3 in the
            # container creates it 0644. Only the directories above it keep it private.
            os.chmod(destination, 0o644)
            return ""
        if argv[:2] == ("docker", "exec") and argv[3:5] == ("rm", "-f") and len(argv) == 6:
            call.kind = "exec-rm"
            self.inner_files.pop(argv[5], None)
            return ""
        self.unexpected.append(argv)
        raise deploy.DeploymentError(f"the fake Docker does not know {argv!r}")

    def _compose(self, argv: tuple[str, ...], env: dict[str, str]) -> str:
        call = Call(kind="compose-unexpected", argv=argv, env=env)
        self.calls.append(call)
        if len(argv) < 7 or argv[2] != "--project-name" or argv[4] != "--file":
            self.unexpected.append(argv)
            raise deploy.DeploymentError(f"unexpected compose invocation {argv!r}")
        call.project, call.compose_file, call.compose_args = argv[3], argv[5], argv[6:]
        compose_file = Path(call.compose_file)
        if not compose_file.is_file():
            raise deploy.DeploymentError(f"open {compose_file}: no such file or directory")
        call.compose_bytes = compose_file.read_bytes()
        for name in ("PORTFOLIO_IMAGE", "PORTFOLIO_PORT", "PORTFOLIO_ENVIRONMENT"):
            if not env.get(name):
                raise deploy.DeploymentError(f"required variable {name} is missing a value")
        verb = call.compose_args
        if verb == ("ps", "--quiet", "app"):
            call.kind = "compose-ps"
            return self.running or ""
        if verb == UP_ARGS:
            call.kind = "compose-up"
            secrets = env.get("PORTFOLIO_SECRETS_ENV_FILE", "")
            if secrets and not Path(secrets).is_file():
                raise deploy.DeploymentError(f"env file {secrets} not found")
            image = env["PORTFOLIO_IMAGE"]
            health = "unhealthy" if image in self.unhealthy else "healthy"
            container = self.start(self.substitute.get(image, image), health)
            if image in self.fail_up or health != "healthy":
                raise deploy.DeploymentError("container portfolio-app-prod-app-1 is unhealthy")
            if self.database is not None:
                self.database = f"live database once {container.id} ran {image}\n".encode()
            return ""
        if verb == ("down",):
            call.kind = "compose-down"
            self.running = None
            return ""
        self.unexpected.append(argv)
        raise deploy.DeploymentError(f"unexpected compose verb {verb!r}")


class RecordingLock:
    """Stands in for deploy.deployment_lock and records where every lock was taken."""

    def __init__(self, before: Callable[[int, Path], None] | None = None) -> None:
        self.taken: list[Path] = []
        self.states: list[dict[str, Any]] = []
        self.before = before

    @contextlib.contextmanager
    def __call__(self, directory: Path, timeout_seconds: float = 240) -> Iterator[None]:
        directory = Path(directory)
        self.taken.append(directory)
        if self.before is not None:
            self.before(len(self.taken) - 1, directory)
        if POSIX:
            with REAL_DEPLOYMENT_LOCK(directory, timeout_seconds):
                self.states.append({"directory": directory, "exists": directory.exists()})
                yield
            return
        # No fcntl here. Mirror the real lock's contract without holding the file open,
        # because Windows refuses to rename a directory with an open file inside it.
        try:
            with builtins.open(directory / "deploy.lock", "a", encoding="utf-8"):
                pass
        except FileNotFoundError:
            raise deploy.RootMoved(f"{directory} moved while waiting for its lock") from None
        self.states.append({"directory": directory, "exists": directory.exists()})
        yield


# The last commit on main before #94. Every delivery run up to it uploads a deploy.py that
# defaults to ~/portfolio-app-deploy, and GitHub lets anyone with write access re-run one.
PRE_94_COMMIT = "edcf88930097473562b36c4c8a04e95e468fd8ce"


PRE_94_FIXTURE = Path(__file__).with_name("fixtures") / "deploy_pre94.py"
FIXTURE_MARKER = b"# --- verbatim below this line ---\n"


def pre_94_source() -> bytes:
    """The vendored pre-#94 deploy.py, without the fixture's explanatory header."""
    header, marker, source = PRE_94_FIXTURE.read_bytes().partition(FIXTURE_MARKER)
    assert marker and header.startswith(b"# Test fixture"), "the fixture lost its header"
    return source


def pre_94_source_from_git() -> bytes | None:
    """The same file from git, or None where the object is absent (a shallow clone)."""
    try:
        return subprocess.run(
            ["git", "show", f"{PRE_94_COMMIT}:deploy/deploy.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def load_pre_94_deploy(directory: Path) -> types.ModuleType:
    """The deploy.py an old delivery run uploads, as a module of its own.

    It is written beside a compose.yml, as the runner uploads it, and loaded under its own
    name so it never replaces the module under test.
    """
    path = directory / "deploy.py"
    path.write_bytes(pre_94_source())
    (directory / "compose.yml").write_bytes(KIT_COMPOSE.read_bytes())
    spec = importlib.util.spec_from_file_location("pre_94_deploy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def fcntl_available() -> Iterator[None]:
    """Let the old script import fcntl on Windows, so its own code runs unchanged.

    The stand-in's flock never blocks. On POSIX the real module is used.
    """
    if POSIX:
        yield
        return
    stand_in = types.ModuleType("fcntl")
    stand_in.LOCK_EX = 2  # type: ignore[attr-defined]
    stand_in.LOCK_NB = 4  # type: ignore[attr-defined]
    stand_in.flock = lambda file, operation: None  # type: ignore[attr-defined]
    with mock.patch.dict(sys.modules, {"fcntl": stand_in}):
        yield


def windows_prepare_secrets(root: Path, environment: str) -> Path:
    """prepare_secrets_env_file without the mode check Windows cannot express.

    Windows reports every writable file as 0o666, so the real check refuses any existing
    secrets.env there. The real function still runs whenever the file is created, and
    Linux CI runs it unmodified on every deployment.
    """
    path = Path(root) / environment / "secrets.env"
    if path.exists():
        return path
    result: Path = REAL_PREPARE_SECRETS(root, environment)
    return result


def tree(path: Path) -> dict[str, bytes]:
    """Every file under ``path`` with its bytes, keyed by its relative POSIX path."""
    if not path.exists():
        return {}
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def read_manifest(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_bytes().decode("utf-8"))
    return data


class Host:
    """A temporary $HOME standing in for the Raspberry Pi's."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home"
        self.home.mkdir()
        self.root = self.home / "portfolio-app"
        self.legacy = self.home / "portfolio-app-deploy"
        self.kit = base / "upload"
        self.kit.mkdir()
        self.kit_compose = self.kit / "compose.yml"
        self.docker = FakeDocker()
        self.lock = RecordingLock()
        self.extra_patches: list[contextlib.AbstractContextManager[Any]] = []
        # What a crash test deploys after the crash, to prove the host recovers.
        self.next_release: Release | None = None
        # What each deploy() printed to stderr, in order.
        self.stderr: list[str] = []

    @property
    def prod(self) -> Path:
        return self.root / "prod"

    # -- running deploy.py --------------------------------------------------------------

    @contextlib.contextmanager
    def patched(self) -> Iterator[None]:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(deploy, "run", self.docker))
            stack.enter_context(mock.patch.object(deploy, "deployment_lock", self.lock))
            stack.enter_context(
                mock.patch.object(deploy, "kit_compose_file", lambda: self.kit_compose)
            )
            if not POSIX:
                stack.enter_context(
                    mock.patch.object(deploy, "prepare_secrets_env_file", windows_prepare_secrets)
                )
            for patch in self.extra_patches:
                stack.enter_context(patch)
            yield

    def stage_kit(self, release: Release, compose: bytes | None = None) -> None:
        self.kit_compose.write_bytes(release.compose if compose is None else compose)

    def deploy(
        self,
        release: Release,
        *,
        migrate: bool = True,
        publish: bool = True,
        tap: FilesystemTap | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        if publish:
            self.docker.publish(release)
        self.stage_kit(release)
        stderr = io.StringIO()
        try:
            with (
                self.patched(),
                tap.installed() if tap else contextlib.nullcontext(),
                contextlib.redirect_stderr(stderr),
            ):
                result: dict[str, Any] = deploy.deploy(
                    **release.arguments(**overrides),
                    root=self.root,
                    legacy_root=self.legacy if migrate else None,
                )
        finally:
            self.stderr.append(stderr.getvalue())
        return result

    # -- host layouts -------------------------------------------------------------------

    def write_secrets(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "secrets.env"
        path.write_bytes(SENTINEL_SECRET)
        os.chmod(path, 0o600)
        return path

    def build_legacy(
        self, releases: tuple[Release, ...] = (Release(1), Release(2), Release(3))
    ) -> None:
        """Lay out ~/portfolio-app-deploy the way the pre-#94 deploy.py left it.

        One attempt per release, each with its compose file, request and result, and every
        attempt after the first with previous.json and a full database copy. current.json
        is the last release's manifest, with the absolute paths the old script stored, and
        that release is the healthy running container.
        """
        assert len(releases) <= len(LEGACY_ATTEMPT_IDS)
        root = self.legacy
        prod = root / "prod"
        prod.mkdir(parents=True)
        (root / "deploy.lock").write_bytes(b"")
        secrets = self.write_secrets(prod)
        previous: dict[str, Any] | None = None
        for index, release in enumerate(releases):
            attempt = prod / "attempts" / LEGACY_ATTEMPT_IDS[index]
            attempt.mkdir(parents=True)
            compose = attempt / "compose.yml"
            compose.write_bytes(release.compose)
            manifest: dict[str, Any] = {
                "environment": "prod",
                "image": release.image,
                "revision": release.revision,
                "version": release.version,
                "compose": str(compose),
                "secrets_env_file": str(secrets),
                "status": "pending",
                "attempt": str(attempt),
                "delivery": {
                    "run_number": release.run_number,
                    "source_run_url": release.source_run_url,
                },
            }
            legacy_write_json(attempt / "request.json", manifest)
            if previous is not None:
                legacy_write_json(attempt / "previous.json", previous)
                database = attempt / "database.sqlite3"
                database.write_bytes(f"legacy backup taken by attempt {index}\n".encode())
                manifest["backup"] = str(database)
            manifest.update(status="healthy", deployed_at=f"2026-09-{index + 1:02d}T10:15:00+00:00")
            legacy_write_json(attempt / "result.json", manifest)
            previous = manifest
        assert previous is not None
        legacy_write_json(prod / "current.json", previous)
        for release in releases:
            self.docker.publish(release)
        self.docker.start(releases[-1].image)

    def legacy_compose_path(self, index: int = -1) -> Path:
        return self.prod / "attempts" / LEGACY_ATTEMPT_IDS[index] / "compose.yml"

    # -- observations -------------------------------------------------------------------

    def sqlite_files(self) -> list[Path]:
        return sorted(self.home.rglob("*.sqlite3"))

    def current(self) -> dict[str, Any]:
        return read_manifest(self.prod / "current.json")


def legacy_write_json(path: Path, value: dict[str, Any]) -> None:
    """The pre-#94 script's JSON format, byte for byte."""
    path.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def expected_compose_sh(release: Release, compose_file: str = "compose.yml") -> bytes:
    """The exact compose.sh spec 018 specifies, with R6's symlink- and CDPATH-proof cd.

    ``compose_file`` is relative to prod/: the live deployment's own file, which for a
    host still on the legacy layout is ``attempts/<id>/compose.yml``.
    """
    lines = [
        "#!/bin/sh",
        "# Written by deploy.py for the live deployment: docker compose against it.",
        "#   ./compose.sh up -d --force-recreate app   apply a secrets.env change",
        "#   ./compose.sh ps",
        "#   ./compose.sh exec app python -m portfolio create-user --username <name>",
        "set -eu",
        'CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")"',
        f"export PORTFOLIO_IMAGE='{release.image}'",
        "export PORTFOLIO_PORT='8083'",
        "export PORTFOLIO_ENVIRONMENT='prod'",
        'export PORTFOLIO_SECRETS_ENV_FILE="$PWD/secrets.env"',
        f'exec docker compose --project-name portfolio-app-prod --file "$PWD/{compose_file}" "$@"',
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


# -- crash injection ----------------------------------------------------------------------


class Crash(BaseException):
    """The process dying: a BaseException, so no ``except Exception`` in deploy.py sees it."""


class _Proxy:
    def __init__(self, real: types.ModuleType, overrides: dict[str, Any]) -> None:
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


OS_MUTATIONS = (
    "rename",
    "replace",
    "renames",
    "remove",
    "unlink",
    "rmdir",
    "mkdir",
    "makedirs",
    "chmod",
    "fchmod",
    "fsync",
    "symlink",
    "link",
    "truncate",
    "ftruncate",
    "write",
)
SHUTIL_MUTATIONS = (
    "rmtree",
    "copyfile",
    "copy",
    "copy2",
    "copytree",
    "move",
    "copymode",
    "copystat",
)
# A copy is not atomic: a crash during one leaves a partial destination file.
TORN_BY_A_CRASH = ("shutil.copyfile", "shutil.copy", "shutil.copy2")
PATH_MUTATIONS = (
    "mkdir",
    "rename",
    "replace",
    "unlink",
    "rmdir",
    "touch",
    "chmod",
    "write_text",
    "write_bytes",
    "symlink_to",
    "hardlink_to",
)


@dataclass
class Step:
    name: str
    args: tuple[Any, ...]
    # For fsync_file/fsync_directory, the inode synced; for shutil.rmtree, the database
    # copies it was about to delete.
    extra: Any = None


@dataclass
class FilesystemTap:
    """Observes deploy.py's filesystem mutations, and kills the process before the k-th.

    It intercepts ``os``, ``shutil`` and ``open`` as deploy.py's own globals, and the
    mutating ``Path`` methods, so a mutation reached any of those ways is a numbered step.
    Once it has crashed, every later step crashes too: a dead process changes nothing, so
    no ``finally`` in deploy.py gets to tidy up on its behalf. A crash at a copy leaves half
    the data at the destination, because that is what a copy cut short leaves.
    """

    crash_at: int | None = None
    steps: list[Step] = field(default_factory=list)
    crashed: bool = False

    def step(self, name: str, args: tuple[Any, ...], extra: Any = None) -> None:
        if self.crashed:
            raise Crash(name)
        if self.crash_at is not None and len(self.steps) == self.crash_at:
            self.crashed = True
            raise Crash(f"killed before {name}{args!r}")
        self.steps.append(Step(name, args, extra))

    def _wrap(self, name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            alive = not self.crashed
            extra = None
            if name == "shutil.rmtree" and args and Path(args[0]).is_dir():
                extra = sorted(Path(args[0]).rglob("*.sqlite3"))
            try:
                self.step(name, args, extra)
            except Crash:
                if alive and name in TORN_BY_A_CRASH:
                    # Killed part-way through a copy: the destination holds half the data
                    # under whatever name the caller chose for it.
                    data = Path(args[0]).read_bytes()
                    with builtins.open(args[1], "wb") as partial:
                        partial.write(data[: len(data) // 2])
                raise
            return function(*args, **kwargs)

        return wrapper

    def _open(self, file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in "wxa+"):
            self.step("open", (file, mode))
        return builtins.open(file, mode, *args, **kwargs)

    @contextlib.contextmanager
    def installed(self) -> Iterator[FilesystemTap]:
        os_proxy = _Proxy(
            os,
            {n: self._wrap(f"os.{n}", getattr(os, n)) for n in OS_MUTATIONS if hasattr(os, n)},
        )
        shutil_proxy = _Proxy(
            shutil,
            {n: self._wrap(f"shutil.{n}", getattr(shutil, n)) for n in SHUTIL_MUTATIONS},
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(deploy, "os", os_proxy))
            stack.enter_context(mock.patch.object(deploy, "shutil", shutil_proxy))
            stack.enter_context(mock.patch.object(deploy, "open", self._open, create=True))
            for name in PATH_MUTATIONS:
                original = getattr(Path, name, None)
                if original is None:
                    continue
                stack.enter_context(
                    mock.patch.object(Path, name, _method(self, f"Path.{name}", original))
                )
            for name in ("fsync_file", "fsync_directory"):
                if hasattr(deploy, name):
                    stack.enter_context(
                        mock.patch.object(deploy, name, self._synced(name, getattr(deploy, name)))
                    )
            yield self

    def _synced(self, name: str, function: Callable[[Path], None]) -> Callable[[Path], None]:
        def wrapper(path: Path) -> None:
            self.step(name, (path,), os.stat(path).st_ino if os.path.exists(path) else None)
            function(path)

        return wrapper

    def named(self, name: str) -> list[Step]:
        return [step for step in self.steps if step.name == name]


def _method(tap: FilesystemTap, name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def method(path: Path, *args: Any, **kwargs: Any) -> Any:
        tap.step(name, (path, *args))
        return original(path, *args, **kwargs)

    return method
