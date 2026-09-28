#!/usr/bin/env python3
"""Deploy an immutable portfolio image on the Raspberry Pi with Docker Compose.

Runs on the Pi, standard library only, uploaded fresh for every deployment so the host
keeps no copy of the tooling. State lives under ``~/portfolio-app/``, and the environment
directory holds the live deployment and exactly one backup::

    deploy.lock              host-wide lock, so two deployments can never interleave
    prod/
      compose.yml            the live deployment's compose file
      current.json           the live deployment's manifest
      secrets.env            operator-managed secrets (chmod 600, never read by this script)
      compose.sh             docker compose against the live deployment, for a person
      last-attempt.json      the latest attempt's request and outcome
      backup/                the previous deployment, and the database as it was before
                             the live one: compose.yml, current.json, database.sqlite3
      failed/                only after a failed deployment, replaced by the next failure:
                             compose.yml, request.json, result.json, database.sqlite3
      incoming/              only while a deployment runs: the candidate being staged

Every path is computed from the root and this layout at the moment it is used; a manifest
stores none. So renaming the root, or moving a file between directories, never leaves a
stale path for a later deployment to follow.

The previous layout kept every attempt in ``prod/attempts/<id>/`` under
``~/portfolio-app-deploy``, and its manifests store absolute paths. The first deployment
that runs this version migrates such a host: under the lock it renames the old root to
the new one with a single ``os.rename``, rolls back (if it must) to the compose file the
old manifest names, rebased onto the new root, and on success deletes ``attempts/``. If
that deployment could not snapshot the live database, the newest attempt holding a copy
becomes ``backup/`` first, so deleting ``attempts/`` never leaves the host without one it
had. If both roots exist it refuses and changes nothing, so a person decides.

Safety properties, in the order they are enforced:

* every argument is validated before any command runs;
* the image must be a digest belonging to this repository, never a mutable tag;
* the live deployment's compose file must exist, or there is nothing to roll back to;
* an older workflow run can never replace a newer deployment (rerun protection);
* the image's OCI labels must match the requested revision and version, so a digest that
  does not correspond to the commit CI claims is refused;
* the live SQLite database is backed up, with an integrity check, before anything is
  replaced;
* the new container must report healthy *and* be running the exact digest, otherwise the
  previous deployment is restored;
* every file this script writes is written to a temporary file and then renamed over the
  old one, so a crash leaves the old file or the new one, never a torn one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

REPOSITORY = "emanuelturtula/portfolio"
IMAGE = re.compile(rf"ghcr\.io/{re.escape(REPOSITORY)}@sha256:[0-9a-f]{{64}}")
REVISION = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"v\d+\.\d+\.\d+")
RUN_URL = re.compile(rf"https://github\.com/{re.escape(REPOSITORY)}/actions/runs/[1-9][0-9]*")
ENVIRONMENTS = {"prod": "8083"}
PROJECT_PREFIX = "portfolio-app"
DATABASE = "/app/data/portfolio.db"
WAIT_TIMEOUT_SECONDS = "180"

ROOT_NAME = "portfolio-app"
LEGACY_ROOT_NAME = "portfolio-app-deploy"
# A manifest carrying this layout was written by this version: its compose file is the one
# beside it. A manifest without it was written by the attempts/ layout.
MANIFEST_LAYOUT = 2
# The only attempt id the attempts/ layout ever wrote. Matching it exactly is also what
# stops a manifest's stored path from pointing anywhere outside prod/attempts/.
LEGACY_ATTEMPT = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
# The lock directory can move at most once (the migration), so one retry is enough;
# the third is margin.
LOCK_ATTEMPTS = 3

Manifest = dict[str, Any]


class DeploymentError(RuntimeError):
    pass


class RootMoved(DeploymentError):
    """The directory whose lock this process meant to take was renamed before it opened it."""


class Live(NamedTuple):
    """The deployment ``prod/`` describes: its manifest and the compose file it runs from."""

    manifest: Manifest
    compose_file: Path


def run(args: list[str], *, env: dict[str, str] | None = None) -> str:
    """Run a command and return its output. Every docker call goes through here."""
    try:
        return subprocess.run(
            args, env=env, check=True, text=True, capture_output=True, timeout=900
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "No diagnostic output").strip()
        detail = "".join(c for c in detail if c.isprintable() or c == "\n")
        raise DeploymentError(f"Command failed ({error.returncode}): {detail[-4000:]}") from error


def read_json(path: Path) -> Manifest:
    data: Manifest = json.loads(path.read_text(encoding="utf-8"))
    return data


def json_bytes(value: Manifest) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Replace ``path`` with ``data`` so that a crash leaves the old file or the new one.

    The bytes are written as given, so line endings are LF on every platform.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def remove_tree(path: Path) -> None:
    if os.path.lexists(path):
        shutil.rmtree(path)


def kit_compose_file() -> Path:
    """The compose file uploaded alongside this script for this deployment."""
    return Path(__file__).with_name("compose.yml")


def default_roots() -> tuple[Path, Path]:
    """The root and the legacy root, computed from the home directory at call time."""
    home = Path.home()
    return home / ROOT_NAME, home / LEGACY_ROOT_NAME


def display(path: Path) -> str:
    """``path`` for a message, written ``~/...`` when it is under the home directory.

    Messages reach the public Actions log through ssh, and the home directory names the
    host user. That user is a repository secret GitHub masks; this does not rely on it.
    """
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except (ValueError, RuntimeError):
        return str(path)


def describe(error: OSError) -> str:
    """An ``OSError`` for a message, with its paths written as ``display`` writes them."""
    names = [
        display(Path(os.fsdecode(name)))
        for name in (error.filename, error.filename2)
        if name is not None and not isinstance(name, int)
    ]
    reason = error.strerror or type(error).__name__
    return f"{reason}: {' -> '.join(names)}" if names else reason


def both_roots_exist(root: Path, legacy_root: Path) -> DeploymentError:
    return DeploymentError(
        f"Both {display(legacy_root)} and {display(root)} exist, so this host cannot be "
        "migrated safely. Nothing was changed. Keep the one holding the live deployment, "
        "move the other away, and retry."
    )


def lock_directory(root: Path, legacy_root: Path | None) -> Path:
    """The directory whose ``deploy.lock`` serialises deployments right now.

    A host still on the legacy root is locked there, so the migration itself happens
    under the lock. Read-only: it creates, renames and runs nothing.
    """
    if legacy_root is None or legacy_root == root or not os.path.lexists(legacy_root):
        return root
    if os.path.lexists(root):
        raise both_roots_exist(root, legacy_root)
    return legacy_root


@contextmanager
def deployment_lock(directory: Path, timeout_seconds: float = 240) -> Iterator[None]:
    """Hold the host-wide lock in ``directory``, which must already exist.

    It never creates the directory: a legacy root that vanished between choosing it and
    opening its lock was renamed by the deployment that migrated it, and recreating it
    would leave both roots on the host. That raises ``RootMoved`` so the caller looks
    again.

    ``flock`` locks the open file, not its path, so a lock taken in the legacy root is
    still held after that root is renamed, and a process already waiting on it waits on
    the same file.
    """
    import fcntl  # Linux only; imported lazily so the unit tests run on any platform.

    try:
        lock = (directory / "deploy.lock").open("a", encoding="utf-8")
    except FileNotFoundError:
        raise RootMoved(f"{display(directory)} moved before its lock could be opened") from None
    with lock:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        "The deployment host is busy; retry once the active "
                        "deployment finishes"
                    ) from None
                time.sleep(0.25)
        yield


def settle_root(root: Path, legacy_root: Path | None) -> Path:
    """With the lock held, re-resolve the root, migrating the legacy one if it is there.

    The migration is a single ``os.rename`` of the whole directory, which leaves the
    running container untouched: it mounts nothing from the root, and reads the secrets
    file only when it is created. A process that waited on the legacy root's lock may
    find another process migrated it in the meantime; it then renames nothing and carries
    on in the new root, holding the same lock file.
    """
    if legacy_root is not None and legacy_root != root and os.path.lexists(legacy_root):
        if os.path.lexists(root):
            raise both_roots_exist(root, legacy_root)
        os.rename(legacy_root, root)
    if not root.is_dir():
        raise DeploymentError(f"The deployment root {display(root)} is not a directory")
    root.chmod(0o700)
    return root


def validate(
    environment: str,
    image: str,
    revision: str,
    version: str,
    run_number: int,
    source_run_url: str,
) -> None:
    if environment not in ENVIRONMENTS:
        raise DeploymentError(f"Environment must be one of {sorted(ENVIRONMENTS)}")
    if not IMAGE.fullmatch(image):
        raise DeploymentError("The image must be an immutable digest belonging to this repository")
    if not REVISION.fullmatch(revision):
        raise DeploymentError("A full 40-character commit SHA is required")
    if not VERSION.fullmatch(version):
        raise DeploymentError(f"Version {version!r} is not a valid release version")
    if type(run_number) is not int or run_number < 1:
        raise DeploymentError("A positive workflow run number is required")
    if not RUN_URL.fullmatch(source_run_url):
        raise DeploymentError("A workflow run URL for this repository is required")


def prepare_secrets_env_file(root: Path, environment: str) -> Path:
    """Guarantee ``<root>/<env>/secrets.env`` exists and is private.

    The operator writes this file over SSH. This script never reads, writes or logs its
    contents; it only creates an empty file on first install and refuses to continue if
    the permissions have been loosened.
    """
    path = root / environment / "secrets.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
        os.chmod(path, 0o600)
    elif path.stat().st_mode & 0o077:
        raise DeploymentError(
            f"{display(path)} must not be group or world readable; run 'chmod 600' on it "
            "and retry"
        )
    return path


def compose(manifest: Manifest, compose_file: Path, secrets_file: Path, *args: str) -> str:
    """Run ``docker compose`` for the deployment ``manifest`` describes.

    The files come from the layout, never from the manifest, which records no paths.
    """
    environment = os.environ.copy()
    environment.update(
        PORTFOLIO_IMAGE=manifest["image"],
        PORTFOLIO_PORT=ENVIRONMENTS[manifest["environment"]],
        PORTFOLIO_ENVIRONMENT=manifest["environment"],
        PORTFOLIO_SECRETS_ENV_FILE=str(secrets_file),
    )
    return run(
        [
            "docker",
            "compose",
            "--project-name",
            f"{PROJECT_PREFIX}-{manifest['environment']}",
            "--file",
            str(compose_file),
            *args,
        ],
        env=environment,
    )


def verify_running(manifest: Manifest, compose_file: Path, secrets_file: Path) -> str:
    container = compose(manifest, compose_file, secrets_file, "ps", "--quiet", "app")
    if not container or "\n" in container:
        raise DeploymentError("Expected exactly one running application container")
    info = json.loads(run(["docker", "inspect", container]))[0]
    if info["Config"]["Image"] != manifest["image"]:
        raise DeploymentError("The running container does not match the requested image")
    if info["State"].get("Health", {}).get("Status") != "healthy":
        raise DeploymentError("The application health check did not pass")
    return str(container)


def backup(
    previous: Manifest, compose_file: Path, secrets_file: Path, destination: Path, tag: str
) -> bool:
    """Copy the live SQLite database to ``destination``; return whether there was one.

    Uses sqlite3's own backup API inside the running container rather than copying the
    file, so the snapshot is consistent even while the application is writing, and
    verifies it with an integrity check before accepting it.
    """
    try:
        container = verify_running(previous, compose_file, secrets_file)
    except DeploymentError:
        return False  # Nothing healthy to back up; this deployment may well be the fix.
    inner_path = f"/app/data/deploy-backup-{tag}.sqlite3"
    script = (
        "import os,sqlite3,sys\n"
        f"if not os.path.exists({DATABASE!r}): print('absent'); sys.exit(0)\n"
        f"source=sqlite3.connect('file:{DATABASE}?mode=ro',uri=True)\n"
        "target=sqlite3.connect(sys.argv[1]); source.backup(target)\n"
        "assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'\n"
        "target.close(); source.close(); print('ok')"
    )
    if run(["docker", "exec", container, "python", "-c", script, inner_path]) == "absent":
        return False
    run(["docker", "cp", f"{container}:{inner_path}", str(destination)])
    run(["docker", "exec", container, "rm", "-f", inner_path])
    return True


def check_run_order(
    previous: Manifest | None, image: str, revision: str, run_number: int
) -> None:
    """Refuse a workflow run older than the one already deployed.

    Re-running an old workflow from the Actions UI would otherwise silently roll
    production back to a previous commit.
    """
    if not previous or "delivery" not in previous:
        return
    last = previous["delivery"]
    same_artifact = (image, revision) == (previous["image"], previous["revision"])
    if run_number < last["run_number"] or (
        run_number == last["run_number"] and not same_artifact
    ):
        raise DeploymentError(
            "This workflow run is older than, or conflicts with, the deployed one; "
            "push a new commit instead of re-running an old workflow"
        )


def legacy_compose_file(prod: Path, previous: Manifest) -> Path:
    """Rebase the compose path an attempts/-layout manifest stores onto today's root.

    Such a manifest names ``<old root>/<env>/attempts/<id>/compose.yml`` as an absolute
    path, under whatever the root was called when it was written. Only the ``<id>`` is
    taken from it, and only in the one format that layout wrote.
    """
    stored = Path(str(previous.get("compose", "")))
    attempt = stored.parent
    if (
        stored.name != "compose.yml"
        or attempt.parent.name != "attempts"
        or attempt.parent.parent.name != prod.name
        or not LEGACY_ATTEMPT.fullmatch(attempt.name)
    ):
        raise DeploymentError(
            "The live deployment's manifest does not name a compose file this script can "
            "locate, so there would be nothing to roll back to. Nothing was changed."
        )
    return prod / "attempts" / attempt.name / "compose.yml"


def live_deployment(prod: Path) -> Live | None:
    """Read the deployment ``prod/`` describes, and find the compose file it runs from.

    Read-only. Refuses if that file is missing: it is the rollback target.
    """
    current = prod / "current.json"
    if not current.exists():
        return None
    manifest = read_json(current)
    if manifest.get("layout") == MANIFEST_LAYOUT:
        compose_file = prod / "compose.yml"
    else:
        compose_file = legacy_compose_file(prod, manifest)
    if not compose_file.is_file():
        raise DeploymentError(
            f"The live deployment's compose file {display(compose_file)} is missing, so "
            "there would be nothing to roll back to. Nothing was changed."
        )
    return Live(manifest, compose_file)


def compose_script(manifest: Manifest) -> str:
    """The text of ``compose.sh``: docker compose against the live deployment.

    It embeds only values ``validate()`` accepted -- a digest, a port and an environment
    name from ``ENVIRONMENTS`` -- and checks them again here, because a value that could
    carry a quote would turn this file into a shell injection. It holds no secret, and
    its paths are relative to the script, so renaming the root does not break it.
    """
    image = manifest["image"]
    environment = manifest["environment"]
    port = ENVIRONMENTS.get(environment, "")
    if not (
        IMAGE.fullmatch(image)
        and re.fullmatch(r"[a-z]+", environment)
        and re.fullmatch(r"[0-9]+", port)
    ):
        raise DeploymentError("Refusing to write compose.sh from an unvalidated manifest")
    return (
        "#!/bin/sh\n"
        "# Written by deploy.py for the live deployment: docker compose against it.\n"
        "#   ./compose.sh up -d --force-recreate app   apply a secrets.env change\n"
        "#   ./compose.sh ps\n"
        "#   ./compose.sh exec app python -m portfolio create-user --username <name>\n"
        "set -eu\n"
        'cd "$(dirname "$0")"\n'
        f"export PORTFOLIO_IMAGE='{image}'\n"
        f"export PORTFOLIO_PORT='{port}'\n"
        f"export PORTFOLIO_ENVIRONMENT='{environment}'\n"
        'export PORTFOLIO_SECRETS_ENV_FILE="$PWD/secrets.env"\n'
        f"exec docker compose --project-name {PROJECT_PREFIX}-{environment} "
        '--file "$PWD/compose.yml" "$@"\n'
    )


def settle_backups(prod: Path) -> None:
    """Finish or undo a backup swap an earlier deployment was interrupted in.

    The swap moves ``backup/`` aside to ``backup.old/`` only once ``backup.new/`` is
    complete, so ``backup.old/`` without ``backup/`` means it stopped halfway: the new one
    is renamed in (or, if it is gone, the old one back). Any other ``backup.new/`` may be
    partial and is deleted. Afterwards only ``backup/`` can exist.
    """
    current, new, old = prod / "backup", prod / "backup.new", prod / "backup.old"
    if os.path.lexists(old):
        if not os.path.lexists(current):
            os.rename(new if os.path.lexists(new) else old, current)
        remove_tree(old)
    remove_tree(new)


def legacy_attempt_with_database(prod: Path) -> Path | None:
    """The newest ``attempts/<id>/`` directory that still holds a database copy, if any.

    Attempt ids start with a fixed-width UTC timestamp, so their names sort in time order.
    """
    attempts = prod / "attempts"
    if not attempts.is_dir():
        return None
    found = [
        path
        for path in attempts.iterdir()
        if LEGACY_ATTEMPT.fullmatch(path.name) and (path / "database.sqlite3").is_file()
    ]
    return max(found, key=lambda path: path.name) if found else None


def legacy_attempt_manifest(attempt: Path) -> bytes | None:
    """The manifest describing a legacy attempt: its result if it recorded a healthy
    deployment, otherwise its request. Returned verbatim, as the record it is."""
    result = attempt / "result.json"
    try:
        if result.is_file() and read_json(result).get("status") == "healthy":
            return result.read_bytes()
    except ValueError:
        pass  # A torn or foreign result.json is no reason to lose the database beside it.
    request = attempt / "request.json"
    return request.read_bytes() if request.is_file() else None


def seed_backup_from_attempts(prod: Path, backup_new: Path) -> bool:
    """Build ``backup.new/`` from the newest legacy attempt holding a database.

    Used only when this deployment took no snapshot and ``backup/`` holds no database,
    just before ``attempts/`` is deleted. Without it, a migrating deployment whose previous
    container was unhealthy would delete every database copy on the host.
    """
    source = legacy_attempt_with_database(prod)
    if source is None:
        return False
    os.mkdir(backup_new)
    if (source / "compose.yml").is_file():
        write_atomic(backup_new / "compose.yml", (source / "compose.yml").read_bytes())
    manifest = legacy_attempt_manifest(source)
    if manifest is not None:
        write_atomic(backup_new / "current.json", manifest)
    os.rename(source / "database.sqlite3", backup_new / "database.sqlite3")
    return True


def rotate(prod: Path, candidate: Manifest, previous: Live | None, snapshot: Path | None) -> None:
    """Make the healthy candidate the live deployment, and the previous one the backup.

    The order bounds what a crash can leave: every file is replaced atomically, and at
    worst ``current.json`` still describes the previous deployment while the candidate
    runs. The next deployment then finds the container does not match that manifest,
    skips its backup, and deploys normally.

    The backup is replaced only when this deployment took a snapshot. One that could not
    -- the previous container was unhealthy, or had no database -- leaves ``backup/`` as
    it was, rather than deleting the only copy of the database for one that has none.
    If ``backup/`` holds no database either and the legacy ``attempts/`` is about to be
    deleted, the newest legacy attempt holding a database becomes the backup instead.
    """
    incoming = prod / "incoming"
    backup_dir, backup_new, backup_old = prod / "backup", prod / "backup.new", prod / "backup.old"

    # 1. Build the new backup beside the old one.
    settle_backups(prod)
    if snapshot is not None:
        os.mkdir(backup_new)
        if previous is not None:
            write_atomic(backup_new / "compose.yml", previous.compose_file.read_bytes())
            write_atomic(backup_new / "current.json", (prod / "current.json").read_bytes())
        os.rename(snapshot, backup_new / "database.sqlite3")
    elif os.path.lexists(prod / "attempts") and not os.path.lexists(
        backup_dir / "database.sqlite3"
    ):
        if not seed_backup_from_attempts(prod, backup_new):
            print(
                "No database backup exists on this host: the previous deployment could not "
                "be snapshotted, and the previous layout held no copy.",
                file=sys.stderr,
            )

    # 2 and 3. The live pair: the compose file first, then the manifest.
    write_atomic(prod / "compose.yml", (incoming / "compose.yml").read_bytes())
    write_atomic(prod / "current.json", json_bytes(candidate))

    # 4. Swap the backup in.
    if os.path.lexists(backup_new):
        if os.path.lexists(backup_dir):
            os.rename(backup_dir, backup_old)
        os.rename(backup_new, backup_dir)
        remove_tree(backup_old)

    # 5 and 6.
    write_atomic(prod / "compose.sh", compose_script(candidate).encode("utf-8"), mode=0o700)
    write_atomic(prod / "last-attempt.json", json_bytes(candidate))

    # 7. A success supersedes a failure's evidence, and the legacy attempts/.
    for leftover in (incoming, prod / "failed", prod / "attempts"):
        remove_tree(leftover)


def record_failure(prod: Path, result: Manifest) -> str:
    """Keep the failed attempt as ``failed/``, and return where, for the error message.

    ``compose.yml``, ``current.json`` and ``backup/`` are not touched: they still describe
    what is running. Recording can itself fail; that is reported rather than allowed to
    hide the failure it was recording.
    """
    incoming, failed = prod / "incoming", prod / "failed"
    try:
        write_atomic(incoming / "result.json", json_bytes(result))
        remove_tree(failed)
        os.rename(incoming, failed)
        write_atomic(prod / "last-attempt.json", json_bytes(result))
    except OSError as error:
        return f"not recorded ({describe(error)})"
    return display(failed)


def deploy(
    environment: str,
    image: str,
    revision: str,
    version: str,
    run_number: int,
    source_run_url: str,
    root: Path,
    legacy_root: Path | None = None,
) -> Manifest:
    """Deploy ``image`` under ``root``, first migrating ``legacy_root`` if it is there.

    Nothing before the lock is taken writes anything but a fresh, empty root.
    """
    validate(environment, image, revision, version, run_number, source_run_url)
    # absolute(), not resolve(): a symlinked legacy root is renamed as the link it is.
    root = Path(root).expanduser().absolute()
    legacy = None if legacy_root is None else Path(legacy_root).expanduser().absolute()
    previous_umask = os.umask(0o077)
    try:
        for _ in range(LOCK_ATTEMPTS):
            directory = lock_directory(root, legacy)
            if directory == root:
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with deployment_lock(directory):
                    settle_root(root, legacy)
                    return deploy_locked(
                        environment, image, revision, version, run_number, source_run_url, root
                    )
            except RootMoved:
                continue
        raise DeploymentError("The deployment root kept moving while waiting for its lock")
    finally:
        os.umask(previous_umask)


def deploy_locked(
    environment: str,
    image: str,
    revision: str,
    version: str,
    run_number: int,
    source_run_url: str,
    root: Path,
) -> Manifest:
    prod = root / environment
    previous = live_deployment(prod)
    check_run_order(
        previous.manifest if previous is not None else None, image, revision, run_number
    )

    # Pull and check provenance before touching the running service.
    run(["docker", "pull", image])
    labels = json.loads(run(["docker", "image", "inspect", image]))[0]["Config"].get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != revision:
        raise DeploymentError("The OCI revision label does not match the requested commit")
    if labels.get("org.opencontainers.image.version") != version:
        raise DeploymentError("The OCI version label does not match the requested version")

    secrets = prepare_secrets_env_file(root, environment)
    attempt = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
    candidate: Manifest = {
        "layout": MANIFEST_LAYOUT,
        "environment": environment,
        "image": image,
        "revision": revision,
        "version": version,
        "status": "pending",
        "attempt": attempt,
        "delivery": {"run_number": run_number, "source_run_url": source_run_url},
    }

    # Stage the candidate, replacing whatever an interrupted deployment left behind.
    incoming = prod / "incoming"
    remove_tree(incoming)
    os.mkdir(incoming)
    candidate_compose = incoming / "compose.yml"
    write_atomic(candidate_compose, kit_compose_file().read_bytes())
    write_atomic(incoming / "request.json", json_bytes(candidate))

    snapshot = incoming / "database.sqlite3"
    candidate["backup"] = False
    if previous is not None:
        try:
            candidate["backup"] = backup(
                previous.manifest, previous.compose_file, secrets, snapshot, attempt
            )
        except Exception as failure:
            evidence = record_failure(
                prod, dict(candidate, status="failed", stage="backup", error=str(failure))
            )
            raise DeploymentError(
                f"The backup failed before the service was replaced: {failure}; "
                f"evidence={evidence}"
            ) from failure

    try:
        compose(
            candidate,
            candidate_compose,
            secrets,
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            WAIT_TIMEOUT_SECONDS,
            "app",
        )
        verify_running(candidate, candidate_compose, secrets)
    except Exception as failure:
        result = dict(candidate, status="failed", error=str(failure))
        try:
            if previous is not None:
                compose(
                    previous.manifest,
                    previous.compose_file,
                    secrets,
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    WAIT_TIMEOUT_SECONDS,
                    "app",
                )
                verify_running(previous.manifest, previous.compose_file, secrets)
                result["rollback"] = "healthy"
            else:
                compose(candidate, candidate_compose, secrets, "down")  # Keeps the data volume.
                result["rollback"] = "no_previous_deployment"
        except Exception as rollback_failure:
            result["rollback"] = "failed"
            result["rollback_error"] = str(rollback_failure)
        evidence = record_failure(prod, result)
        raise DeploymentError(
            f"Deployment failed; rollback={result['rollback']}; evidence={evidence}"
        ) from failure

    candidate.update(status="healthy", deployed_at=datetime.now(UTC).isoformat())
    try:
        rotate(prod, candidate, previous, snapshot if candidate["backup"] else None)
    except OSError as error:
        raise DeploymentError(
            "The new deployment is running and healthy, but recording it failed: "
            f"{describe(error)}. "
            "The next deployment repairs the files under the root."
        ) from error
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=tuple(ENVIRONMENTS), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--source-run-url", required=True)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=f"the deployment root; default ~/{ROOT_NAME}, migrating ~/{LEGACY_ROOT_NAME}. "
        "An explicit root is never migrated into.",
    )
    args = parser.parse_args(argv)
    arguments = vars(args)
    if args.root is None:
        arguments["root"], arguments["legacy_root"] = default_roots()
    try:
        result = deploy(**arguments)
    except (DeploymentError, subprocess.SubprocessError, OSError, ValueError) as error:
        print(describe(error) if isinstance(error, OSError) else str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
