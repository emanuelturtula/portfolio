#!/usr/bin/env python3
"""Deploy an immutable portfolio image on the Raspberry Pi with Docker Compose.

Runs on the Pi, standard library only, uploaded fresh for every deployment so the host
keeps no copy of the tooling. State lives under ``~/portfolio-app/``, and the environment
directory holds the live deployment and at most one backup::

    deploy.lock              host-wide lock, so two deployments can never interleave
    prod/
      compose.yml            the live deployment's compose file
      current.json           the live deployment's manifest
      secrets.env            operator-managed secrets (chmod 600, never read by this script)
      compose.sh             docker compose against the live deployment, for a person
      last-attempt.json      the latest attempt's request and outcome
      backup/                the previous deployment, and the database as it was before
                             the live one: compose.yml, current.json, and
                             database.sqlite3 once there has been a database to back up,
                             with snapshot.json naming the attempt that took it
      failed/                only after a failed or interrupted deployment, replaced by
                             the next one: compose.yml, request.json, result.json,
                             database.sqlite3 and snapshot.json when it holds one
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
had. A regular file, the tombstone, is left at the old path: every earlier deploy.py
defaults to it, and fails on a file before running anything, where it would otherwise
recreate an empty root and deploy into it. If both roots exist as directories it refuses
and changes nothing, so a person decides.

Safety properties, in the order they are enforced:

* every argument is validated before any command runs;
* the image must be a digest belonging to this repository, never a mutable tag;
* the live deployment's compose file must exist, or there is nothing to roll back to;
* an older workflow run can never replace a newer deployment (rerun protection);
* the image's OCI labels must match the requested revision and version, so a digest that
  does not correspond to the commit CI claims is refused;
* the live SQLite database is backed up, with an integrity check, before anything is
  replaced;
* a snapshot is never dropped: a deployment that cannot take one carries forward the one
  a failed or interrupted attempt took of the same live deployment, and no older copy is
  deleted before the copy replacing it is fsynced under its final name;
* the new container must report healthy *and* be running the exact digest, otherwise the
  previous deployment is restored;
* before the previous deployment starts again, the candidate is stopped and the live
  database's schema revision is read; if the candidate moved it, the attempt's own
  snapshot is put back with the previous image's own ``restore-backup``, which keeps a
  safety copy of the migrated database, and a restore that fails leaves the previous image
  unstarted rather than starting it on a database it cannot read. A stop or a revision
  read that fails has changed nothing, so it leaves the decision to the rollback as
  before: the previous image is started;
* every file this script writes is written to a temporary file and then renamed over the
  old one, so a crash leaves the old file or the new one, never a torn one. That holds in
  the backups volume too: the snapshot a rollback restores lands there through a
  ``.partial`` file, fsynced and renamed.

A rollback that restores leaves two copies in the backups volume, beside the scheduled
ones and rotated with them: the snapshot it restored, under a copy's name, and the safety
copy ``restore-backup`` took of the migrated database. The snapshot also stays in
``failed/``. ``result.json`` says which happened in ``database``: ``unchanged``,
``restored``, ``not_restored`` (the revision moved, or could not be compared, and the
attempt has no snapshot of its own with a revision to put back), ``restore_failed``, or
``unread`` (stopping the candidate or reading the revision failed). See
``roll_back_database`` (spec 034).
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
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
# The scheduled copies' directory in the container, on the backups volume: the compose
# file's PORTFOLIO_BACKUP_DIR, and where restore-backup looks for the copy it is named.
BACKUP_DIRECTORY = "/app/backups"
WAIT_TIMEOUT_SECONDS = "180"
# An Alembic revision as the application names them (0010_exchange_balances). A value read
# from a database that is not one is never trusted into a record or a comparison.
DATABASE_REVISION = re.compile(r"[A-Za-z0-9_.-]{1,128}")
# A copy's name, as the application's BACKUP_NAME_PATTERN (domain/backups.py) accepts it:
# the UTC instant it was started, to the microsecond.
COPY_NAME = re.compile(r"portfolio-[0-9]{8}T[0-9]{12}Z\.sqlite3")
# What restore-backup prints before the rows per table, which no record may hold (spec 034
# R5). Only ever looked for in a failure's diagnostic; see roll_back_database.
ROWS_REPORT = "Rows per table"
# docker compose run, for a one-off container of the service: removed when it exits, with
# no other service started, no terminal and no published port. The service's volumes,
# user and env_file are kept, which is the point.
ONE_OFF = ("run", "--rm", "--no-deps", "-T", "app")

# The scripts below run in a container, with the image's Python, as ``python -c SCRIPT
# ARGS...``. Each takes its paths as arguments, so the tests run the very same text with the
# host's Python against real SQLite files (spec 034, R9). Standard library only, and no
# ``assert``: a check must hold under ``python -O`` too.

# Copies the live database (argv[1]) to argv[2] with SQLite's backup API, from a read-only
# connection, in one step: one read transaction, so the copy is consistent while the
# application writes. The copy is made one self-contained file (journal_mode=DELETE: the
# backup copies page 1, whose header would otherwise say WAL), checked, and its schema
# revision printed: "ok <revision>", or "ok" when alembic_version does not hold exactly one
# row -- no reason to refuse a deployment. "absent" when there is no database yet.
SNAPSHOT_SCRIPT = """\
import os, pathlib, sqlite3, sys
database, copy = sys.argv[1], sys.argv[2]
if not os.path.exists(database):
    print("absent")
    sys.exit(0)
source = sqlite3.connect(pathlib.Path(database).resolve().as_uri() + "?mode=ro", uri=True)
target = sqlite3.connect(copy)
source.backup(target, pages=-1)
source.close()
if target.execute("PRAGMA journal_mode=DELETE").fetchall() != [("delete",)]:
    sys.exit("the copy could not be made one file")
if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
    sys.exit("the copy did not pass PRAGMA integrity_check")
try:
    rows = target.execute("SELECT version_num FROM alembic_version").fetchall()
except sqlite3.OperationalError:
    rows = []
target.close()
if len(rows) == 1:
    print("ok", rows[0][0])
else:
    print("ok")
"""

# Prints the schema revision of the database at argv[1], or "absent" when there is none.
# Opened read-write, without creating it: after an unclean stop a -wal still holds committed
# transactions, and a read-write connection that closes last recovers them into the file
# and removes the -wal, which restore-backup would otherwise read as an open database and
# refuse (spec 029's release_wal; spec 034 R3). Fails unless alembic_version holds one row.
REVISION_SCRIPT = """\
import os, pathlib, sqlite3, sys
database = sys.argv[1]
if not os.path.exists(database):
    print("absent")
    sys.exit(0)
connection = sqlite3.connect(pathlib.Path(database).resolve().as_uri() + "?mode=rw", uri=True)
rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
connection.close()
if len(rows) != 1:
    sys.exit("alembic_version holds %d rows, not one" % len(rows))
print(rows[0][0])
"""

# Writes stdin into the directory argv[1] under the copy name argv[2], the way the
# application writes a scheduled copy: into ".portfolio-<stamp>.partial", fsynced, renamed,
# and the directory fsynced. The application's own clean-up removes a .partial a crash
# left, once it is an hour old. Refuses a name that is not a copy's, or one already taken.
# Prints "ok <bytes written>", which the caller compares with the file it sent.
STREAM_SCRIPT = """\
import os, re, shutil, sys
directory, name = sys.argv[1], sys.argv[2]
match = re.fullmatch(r"portfolio-([0-9]{8}T[0-9]{12}Z)[.]sqlite3", name)
if match is None:
    sys.exit("not a copy's name")
final = os.path.join(directory, name)
partial = os.path.join(directory, ".portfolio-" + match.group(1) + ".partial")
if os.path.lexists(final):
    sys.exit(name + " already exists")
target = open(partial, "xb")
try:
    with target:
        shutil.copyfileobj(sys.stdin.buffer, target)
        target.flush()
        os.fsync(target.fileno())
        size = target.tell()
    if os.path.lexists(final):
        sys.exit(name + " already exists")
    os.rename(partial, final)
except BaseException:
    if os.path.lexists(partial):
        os.remove(partial)
    raise
if os.name == "posix":
    descriptor = os.open(directory, os.O_RDONLY)
    os.fsync(descriptor)
    os.close(descriptor)
print("ok", size)
"""

ROOT_NAME = "portfolio-app"
LEGACY_ROOT_NAME = "portfolio-app-deploy"
# A manifest carrying this layout was written by this version: its compose file is the one
# beside it. A manifest without it was written by the attempts/ layout.
MANIFEST_LAYOUT = 2
# The only attempt id the attempts/ layout ever wrote. Matching it exactly is also what
# stops a manifest's stored path from pointing anywhere outside prod/attempts/.
LEGACY_ATTEMPT = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
# Any attempt id: the legacy form, or this version's, which adds microseconds. Both start
# with their UTC time, which attempt_time() reads.
ATTEMPT = re.compile(r"([0-9]{8}T[0-9]{6})([0-9]{6})?Z-[0-9a-f]{12}")
# The lock directory can move at most once (the migration), so one retry is enough;
# the third is margin.
LOCK_ATTEMPTS = 3
# Left as a regular file where the legacy root was. Every earlier deploy.py defaults to that
# path and creates it with mkdir(exist_ok=True), which raises on a file before any docker
# command runs; without it, re-running an old delivery would recreate an empty root there,
# deploy into it, and leave two roots behind.
TOMBSTONE = (
    f"This directory moved to ~/{ROOT_NAME} (issue #94).\n"
    "\n"
    "This file stands in its place so that re-running an older delivery, whose deploy.py\n"
    f"defaults to ~/{LEGACY_ROOT_NAME}, fails at once instead of deploying into an empty\n"
    "directory here. Leave it where it is.\n"
)

Manifest = dict[str, Any]


class DeploymentError(RuntimeError):
    pass


class RootMoved(DeploymentError):
    """The directory whose lock this process meant to take was renamed before it opened it."""


class Live(NamedTuple):
    """The deployment ``prod/`` describes: its manifest and the compose file it runs from."""

    manifest: Manifest
    compose_file: Path


COMMAND_TIMEOUT_SECONDS = 900


def command_name(args: list[str]) -> str:
    """A command for a message, without its arguments, which name absolute paths."""
    if args[:2] == ["docker", "compose"] and len(args) > 6:
        return f"docker compose {args[6]}"
    return " ".join(args[:3] if args[1:2] == ["image"] else args[:2])


def run(
    args: list[str], *, env: dict[str, str] | None = None, input_file: Path | None = None
) -> str:
    """Run a command and return its output. Every docker call goes through here.

    Its failures are reported without the command's arguments, and docker's own output
    is redacted: both can spell out absolute paths under the home directory.

    ``input_file``, when given, is opened in binary and becomes the command's stdin. It is
    how a rollback hands a database copy to a one-off container: a bind mount of a 0600 file
    the deploy user owns cannot be read by the container's user, and ``docker cp`` would
    leave the copy owned by root (spec 034).
    """
    stdin = None if input_file is None else open(input_file, "rb")
    try:
        return subprocess.run(
            args, env=env, stdin=stdin, check=True, text=True, capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "No diagnostic output").strip()
        detail = "".join(c for c in detail if c.isprintable() or c == "\n")
        raise DeploymentError(
            f"Command failed ({error.returncode}): {redact(detail[-4000:])}"
        ) from error
    except subprocess.TimeoutExpired:
        raise DeploymentError(
            f"{command_name(args)} did not finish within {COMMAND_TIMEOUT_SECONDS} seconds"
        ) from None
    finally:
        if stdin is not None:
            stdin.close()


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


def fsync_file(path: Path) -> None:
    """Force a file's data to disk.

    A new file renamed into a new name is not covered by ext4's rename heuristics, so its
    data can reach the disk well after the unlinks of the older copies it replaces. Every
    database copy is fsynced before an older one is removed.
    """
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    """Force a directory's entries (a rename into it, a new file) to disk.

    POSIX only: Windows cannot open a directory this way, and the tests that run there
    do not depend on durability. Always called, so it can be observed on every platform.
    """
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    """``path`` for a message, never as an absolute path.

    Everything this script writes to stderr reaches the public Actions log through ssh,
    and an absolute path under the home directory names the host user. That user is a
    repository secret GitHub masks; this does not rely on the mask. So ``~`` stands for
    the directory holding the deployment root (the home directory, unless ``--root``
    says otherwise), then for the home directory itself, and a path under neither is
    shown by its name alone.
    """
    path = Path(path)
    if not path.anchor:  # Relative: it names nothing outside the deployment.
        return path.as_posix()
    for candidate in (path, *path.parents):
        if candidate.name in (ROOT_NAME, LEGACY_ROOT_NAME):
            return "~/" + path.relative_to(candidate.parent).as_posix()
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except (ValueError, RuntimeError):
        return ".../" + path.name


def describe(error: OSError) -> str:
    """An ``OSError`` for a message: its reason and its paths as ``display`` writes them.

    Never ``str(error)``, which on Linux spells out both absolute paths of a failed
    rename.
    """
    names = [
        display(Path(os.fsdecode(name)))
        for name in (error.filename, error.filename2)
        if name is not None and not isinstance(name, int)
    ]
    reason = error.strerror or type(error).__name__
    return f"{reason}: {' -> '.join(names)}" if names else reason


# An absolute path up to the deployment root it leads to, on POSIX or Windows, in any
# text: docker's own diagnostics, a traceback, an exception from a library. Not one
# already written as ~/ by display().
ROOT_PREFIX = re.compile(
    r"(?<![\w~.])(?:[A-Za-z]:)?(?:[\\/][^\\/\s'\"]+)*?[\\/]"
    rf"(?=(?:{re.escape(LEGACY_ROOT_NAME)}|{re.escape(ROOT_NAME)})(?:[\\/\s'\":,)]|$))"
)


def redact(text: str) -> str:
    """``text`` with every absolute path to a deployment root, and the home directory
    itself, written as ``~``. The last line of defence before stderr, for text this
    script did not compose: docker's output, a traceback, a library's exception."""
    text = ROOT_PREFIX.sub("~/", text)
    try:
        home = Path.home()
    except RuntimeError:
        return text
    for spelling in {str(home), home.as_posix()}:
        if len(spelling) > 1:
            text = text.replace(spelling, "~")
    return text


def reason(error: BaseException) -> str:
    """An exception's text for a message: ``describe`` for an ``OSError``, never its
    ``str``, and redacted either way."""
    if isinstance(error, OSError):
        return describe(error)
    return redact(str(error))


def warn(message: str) -> None:
    """Every line this script writes to stderr goes through here, redacted."""
    print(redact(message), file=sys.stderr)


def both_roots_exist(root: Path, legacy_root: Path) -> DeploymentError:
    return DeploymentError(
        f"Both {display(legacy_root)} and {display(root)} exist, so this host cannot be "
        "migrated safely. Nothing was changed. Keep the one holding the live deployment, "
        "move the other away, and retry."
    )


def legacy_root_present(root: Path, legacy_root: Path | None) -> bool:
    """Whether a legacy root still waits to be migrated.

    Only a directory counts (a symlink to one included): the file a migration leaves at
    that path is the tombstone, not a root.
    """
    return legacy_root is not None and legacy_root != root and os.path.isdir(legacy_root)


def lock_directory(root: Path, legacy_root: Path | None) -> Path:
    """The directory whose ``deploy.lock`` serialises deployments right now.

    A host still on the legacy root is locked there, so the migration itself happens
    under the lock. Read-only: it creates, renames and runs nothing.
    """
    if not legacy_root_present(root, legacy_root):
        return root
    assert legacy_root is not None
    if os.path.lexists(root):
        # Looked again: a concurrent deployment may have migrated it between the checks.
        if not legacy_root_present(root, legacy_root):
            return root
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
    file only when it is created. The tombstone then takes the old path. A process that
    waited on the legacy root's lock may find another process migrated it in the
    meantime; it then renames nothing and carries on in the new root, holding the same
    lock file.
    """
    migrated = False
    if legacy_root_present(root, legacy_root):
        assert legacy_root is not None
        if os.path.lexists(root):
            raise both_roots_exist(root, legacy_root)
        os.rename(legacy_root, root)
        fsync_directory(root.parent)
        migrated = True
        warn(
            f"Migrated the deployment root from {display(legacy_root)} to {display(root)}."
        )
    if not root.is_dir():
        raise DeploymentError(f"The deployment root {display(root)} is not a directory")
    root.chmod(0o700)
    if (
        legacy_root is not None
        and legacy_root != root
        and not os.path.lexists(legacy_root)
        # Just migrated, or migrated by a run that stopped before it wrote the tombstone,
        # which the legacy attempts/ still shows until a deployment succeeds.
        and (migrated or any((root / env / "attempts").is_dir() for env in ENVIRONMENTS))
    ):
        write_atomic(legacy_root, TOMBSTONE.encode("utf-8"), mode=0o644)
        fsync_directory(legacy_root.parent)
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


def compose(
    manifest: Manifest,
    compose_file: Path,
    secrets_file: Path,
    *args: str,
    input_file: Path | None = None,
) -> str:
    """Run ``docker compose`` for the deployment ``manifest`` describes.

    The files come from the layout, never from the manifest, which records no paths.
    ``input_file`` becomes the command's stdin, as ``run`` describes.
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
        input_file=input_file,
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


class Snapshot(NamedTuple):
    """A copy of the live database ``backup`` took.

    ``revision`` is the Alembic revision the copy holds, read from the copy itself, or None
    when its ``alembic_version`` does not hold exactly one revision. A rollback compares it
    with the live database's to tell whether the candidate migrated (spec 034, R1).
    """

    revision: str | None


def backup(
    previous: Manifest, compose_file: Path, secrets_file: Path, destination: Path, tag: str
) -> Snapshot | None:
    """Copy the live SQLite database to ``destination``; None when there was nothing to copy.

    Uses sqlite3's own backup API inside the running container rather than copying the
    file, so the snapshot is consistent even while the application is writing, and
    verifies it with an integrity check before accepting it. ``SNAPSHOT_SCRIPT`` has the
    steps. The copy is written into the data volume, beside the database, and removed once
    ``docker cp`` has brought it out.
    """
    try:
        container = verify_running(previous, compose_file, secrets_file)
    except DeploymentError:
        return None  # Nothing healthy to back up; this deployment may well be the fix.
    inner_path = f"/app/data/deploy-backup-{tag}.sqlite3"
    output = run(
        ["docker", "exec", container, "python", "-c", SNAPSHOT_SCRIPT, DATABASE, inner_path]
    )
    if output == "absent":
        return None
    status, _, revision = output.partition(" ")
    if status != "ok":
        raise DeploymentError("The database snapshot did not report that it succeeded")
    run(["docker", "cp", f"{container}:{inner_path}", str(destination)])
    run(["docker", "exec", container, "rm", "-f", inner_path])
    return Snapshot(revision if DATABASE_REVISION.fullmatch(revision) else None)


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
            "locate, so there would be nothing to roll back to. The live deployment was not "
            "touched."
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
            "there would be nothing to roll back to. The live deployment was not touched."
        )
    return Live(manifest, compose_file)


# The compose file compose.sh may name, relative to the environment directory: the live
# one, or a legacy attempt's while a migrated host's live deployment still runs from it.
SCRIPT_COMPOSE_FILE = re.compile(rf"compose\.yml|attempts/{LEGACY_ATTEMPT.pattern}/compose\.yml")


def compose_script(manifest: Manifest, compose_file: str = "compose.yml") -> str:
    """The text of ``compose.sh``: docker compose against the live deployment.

    It embeds only values ``validate()`` accepted -- a digest, a port and an environment
    name from ``ENVIRONMENTS`` -- plus ``compose_file``, the live compose file relative to
    the script, which must be ``compose.yml`` or a legacy attempt's. It checks them all
    again here, because a value that could carry a quote would turn this file into a
    shell injection. It holds no secret. It finds its own directory through any symlink
    and ignoring ``CDPATH``, and names everything relative to it, so renaming the root
    does not break it.
    """
    image = manifest["image"]
    environment = manifest["environment"]
    port = ENVIRONMENTS.get(environment, "")
    if not (
        IMAGE.fullmatch(image)
        and re.fullmatch(r"[a-z]+", environment)
        and re.fullmatch(r"[0-9]+", port)
        and SCRIPT_COMPOSE_FILE.fullmatch(compose_file)
    ):
        raise DeploymentError("Refusing to write compose.sh from an unvalidated manifest")
    return (
        "#!/bin/sh\n"
        "# Written by deploy.py for the live deployment: docker compose against it.\n"
        "#   ./compose.sh up -d --force-recreate app   apply a secrets.env change\n"
        "#   ./compose.sh ps\n"
        "#   ./compose.sh exec app python -m portfolio create-user --username <name>\n"
        "set -eu\n"
        'CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")"\n'
        f"export PORTFOLIO_IMAGE='{image}'\n"
        f"export PORTFOLIO_PORT='{port}'\n"
        f"export PORTFOLIO_ENVIRONMENT='{environment}'\n"
        'export PORTFOLIO_SECRETS_ENV_FILE="$PWD/secrets.env"\n'
        f"exec docker compose --project-name {PROJECT_PREFIX}-{environment} "
        f'--file "$PWD/{compose_file}" "$@"\n'
    )


def write_live_compose_script(prod: Path, live: Live) -> None:
    """Make ``compose.sh`` address the live deployment, before anything can refuse.

    Rotation rewrites it after every success, but a host whose live deployment has none
    -- one just migrated from the attempts/ layout, or one a crash left mid-rotation --
    would otherwise have no working ``compose.sh`` until a deployment succeeds, and a
    failed deployment would leave it that way. It is rewritten only if missing or
    different. A live manifest that does not validate is reported and skipped: this must
    never be what stops a deployment.
    """
    try:
        script = compose_script(live.manifest, live.compose_file.relative_to(prod).as_posix())
    except (DeploymentError, KeyError, TypeError, ValueError):
        warn(
            "compose.sh was not written for the live deployment: its manifest does not "
            "validate."
        )
        return
    target = prod / "compose.sh"
    data = script.encode("utf-8")
    try:
        if not target.is_file() or target.read_bytes() != data:
            write_atomic(target, data, mode=0o700)
    except OSError as error:
        warn(
            f"compose.sh was not written for the live deployment: {describe(error)}"
        )


def deployment_id(manifest: Manifest) -> str | None:
    """The id of the attempt that made a deployment. Layout 2 stores the id itself; the
    attempts/ layout stored that attempt's directory, whose name is the id."""
    return Path(str(manifest.get("attempt") or "")).name or None


def attempt_time(attempt_id: object) -> datetime | None:
    """When an attempt started, read from its id; None for anything that is not one."""
    match = ATTEMPT.fullmatch(attempt_id) if isinstance(attempt_id, str) else None
    if match is None:
        return None
    seconds, microseconds = match.groups()
    try:
        started = datetime.strptime(seconds, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None  # The right shape but no real time: a damaged record, not an attempt.
    return started.replace(microsecond=int(microseconds or 0))


def recorded_attempt_times(prod: Path) -> list[datetime]:
    """Every attempt time the environment directory records, wherever it records one."""
    sources = [
        ("current.json", "attempt"),
        ("last-attempt.json", "attempt"),
        ("incoming/request.json", "attempt"),
        ("incoming/snapshot.json", "taken_by"),
        ("failed/request.json", "attempt"),
        ("failed/snapshot.json", "taken_by"),
        ("backup/snapshot.json", "taken_by"),
        ("backup.new/snapshot.json", "taken_by"),
    ]
    times = []
    for name, key in sources:
        try:
            value = read_json(prod / name).get(key)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, str):
            value = Path(value).name  # a legacy manifest stores its attempt's directory
        found = attempt_time(value)
        if found is not None:
            times.append(found)
    return times


def new_attempt_id(prod: Path) -> str:
    """An id for this attempt: its UTC start to the microsecond, then 12 random hex digits.

    The carry-forward decides which database copy is newer by comparing the times in
    attempt ids, so they must never go backwards on a host. A Raspberry Pi without an RTC
    battery can boot with its clock behind until NTP catches up, and two quick attempts
    can land in the same clock tick. So the time is never earlier than one microsecond
    after the latest attempt the host already records.
    """
    started = datetime.now(UTC)
    latest = max(recorded_attempt_times(prod), default=None)
    if latest is not None and started <= latest:
        try:
            started = latest + timedelta(microseconds=1)
        except OverflowError:
            pass  # A record dated at the end of year 9999 is damaged; the clock is better.
    return f"{started:%Y%m%dT%H%M%S%f}Z-{uuid.uuid4().hex[:12]}"


def snapshot_record(taken_by: str | None) -> bytes:
    """``snapshot.json``: which attempt took the database beside it.

    It travels with every database copy -- in ``incoming/``, ``failed/`` and ``backup/``
    -- and is written before the database, so a complete directory always has it.
    Attempt ids carry their start time and never go backwards on a host
    (``new_attempt_id``), so comparing two says which copy is newer, whatever deployment
    each was taken from.
    """
    return json_bytes({"taken_by": taken_by})


def snapshot_taken_by(directory: Path, *, request_fallback: bool = False) -> str | None:
    """The attempt that took the database in ``directory``, if it can be known.

    ``snapshot.json`` says; failing that, and only where the directory is an attempt's own
    (``failed/``), its ``request.json`` names the attempt, which took any snapshot it held.
    """
    sources = [("snapshot.json", "taken_by")]
    if request_fallback:
        sources.append(("request.json", "attempt"))
    for name, key in sources:
        try:
            value = read_json(directory / name).get(key)
        except (OSError, ValueError, AttributeError):
            continue
        if attempt_time(value) is not None:
            return str(value)
    return None


def carried_snapshot(prod: Path, live: Live | None) -> Path | None:
    """``failed/database.sqlite3``, when a deployment that took no snapshot must keep it.

    Decided by time, not by deployment: the copy in ``failed/`` is carried unless the
    backup's is known to be at least as new. Both record the attempt that took them
    (``snapshot_taken_by``), and attempt ids carry times that never go backwards on a
    host (``attempt_time``, ``new_attempt_id``). Whichever deployment
    each came from, the newer copy is the one worth keeping. An unknown time on either
    side carries, because a failed or interrupted attempt normally postdates the backup,
    and with no database in the backup at all an older copy still beats none. Call this
    only once ``settle_backups`` has run.

    The ``"replaces"`` each request records is no longer consulted: a ``failed/`` that
    survived a crashed rotation holds a snapshot older than the one that rotation put in
    the backup, which the time rule already sees.
    """
    database = prod / "failed" / "database.sqlite3"
    if live is None or not database.is_file():
        return None
    backup = prod / "backup"
    if not os.path.lexists(backup / "database.sqlite3"):
        return database
    failed_taken = attempt_time(snapshot_taken_by(prod / "failed", request_fallback=True))
    backup_taken = attempt_time(snapshot_taken_by(backup))
    if failed_taken is not None and backup_taken is not None and failed_taken <= backup_taken:
        return None
    return database


def retire_incoming(prod: Path) -> None:
    """Clear what an earlier deployment left in ``incoming/`` before staging a new one.

    One holding ``database.sqlite3`` was interrupted after its snapshot, perhaps with its
    candidate already running, and that snapshot may be the only copy of the database
    from before it. So it becomes ``failed/``, like any failed attempt, and the
    carry-forward keeps its database. It is newer than any ``failed/`` already there,
    which it replaces. Anything else is deleted.
    """
    incoming, failed = prod / "incoming", prod / "failed"
    database = incoming / "database.sqlite3"
    if not database.is_file():
        remove_tree(incoming)
        return
    fsync_file(database)
    fsync_directory(incoming)
    if not (incoming / "result.json").exists():
        try:
            request = read_json(incoming / "request.json")
        except (OSError, ValueError):
            request = {}
        interrupted = dict(
            request,
            status="interrupted",
            error="The deployment stopped before it finished; its snapshot was kept.",
        )
        write_atomic(incoming / "result.json", json_bytes(interrupted))
    remove_tree(failed)
    os.rename(incoming, failed)
    fsync_directory(prod)


def settle_database(prod: Path) -> None:
    """Make the database just placed in ``backup.new/`` durable, name and all, before any
    older copy can be removed."""
    new = prod / "backup.new"
    fsync_file(new / "database.sqlite3")
    fsync_directory(new)
    fsync_directory(prod)


def swap_backup(prod: Path) -> None:
    """Rename ``backup.new/`` in for ``backup/``: the old one aside, the new one in, then
    the old one deleted, so no moment passes without a complete backup directory."""
    current, new, old = prod / "backup", prod / "backup.new", prod / "backup.old"
    if os.path.lexists(current):
        if os.path.lexists(old):
            fsync_directory(prod)
            remove_tree(old)  # An aside left by an earlier crash; backup/ supersedes it.
        os.rename(current, old)
    os.rename(new, current)
    fsync_directory(prod)  # The new backup's name is on disk before the old one goes.
    remove_tree(old)


def settle_backups(prod: Path) -> None:
    """Finish or undo a backup swap an earlier deployment was interrupted in.

    ``backup.new/`` receives its database last, and atomically, so a ``backup.new/``
    holding ``database.sqlite3`` is complete. It is promoted, exactly as rotation would
    have: its database is the one from before the deployment that crashed, which is the
    copy that matters if that deployment's migration damaged the data, and the next
    deployment may well be unable to take a snapshot of its own. Only a ``backup.new/``
    without a database is incomplete and discarded; then a ``backup.old/`` is renamed back
    if ``backup/`` is missing, and deleted otherwise. Afterwards only ``backup/`` can exist.
    """
    current, new, old = prod / "backup", prod / "backup.new", prod / "backup.old"
    if os.path.lexists(new / "database.sqlite3"):
        # Durable on any POSIX filesystem, not only ext4 in data=ordered mode, before the
        # swap removes anything.
        settle_database(prod)
        swap_backup(prod)
        return
    remove_tree(new)
    if os.path.lexists(old):
        if os.path.lexists(current):
            fsync_directory(prod)
            remove_tree(old)
        else:
            os.rename(old, current)
            fsync_directory(prod)


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


SEEDED_WITHOUT_COMPOSE = (
    "The compose file of the attempt this deployment was made in no longer exists, so this "
    "backup has none."
)
SEEDED_WITHOUT_MANIFEST = (
    "The attempt that took this database left no readable previous.json, so the deployment "
    "it was taken from is unknown."
)


def legacy_backup_record(prod: Path, attempt: Path) -> tuple[bytes | None, bytes]:
    """The compose file and ``current.json`` to keep beside a legacy attempt's database.

    That database was taken, before the attempt, from the deployment its
    ``previous.json`` names -- not from the attempt itself. So the manifest is
    ``previous.json``, verbatim, and the compose file is the one that deployment ran from,
    in the attempt it was made in. If that attempt has been pruned, there is no compose
    file, and the manifest says why in ``backup_note``. An unreadable ``previous.json``
    leaves only a note: the database is kept either way.
    """
    record = attempt / "previous.json"
    try:
        manifest = read_json(record)
        if not isinstance(manifest, dict):
            raise ValueError("previous.json is not an object")
    except (OSError, ValueError):
        return None, json_bytes({"backup_note": SEEDED_WITHOUT_MANIFEST})
    try:
        compose_file: Path | None = legacy_compose_file(prod, manifest)
    except DeploymentError:
        compose_file = None
    if compose_file is None or not compose_file.is_file():
        return None, json_bytes(dict(manifest, backup_note=SEEDED_WITHOUT_COMPOSE))
    return compose_file.read_bytes(), record.read_bytes()


def seed_backup_from_attempts(prod: Path, backup_new: Path) -> bool:
    """Build ``backup.new/`` from the newest legacy attempt holding a database.

    Used only when this deployment took no snapshot and ``backup/`` holds no database,
    just before ``attempts/`` is deleted. Without it, a migrating deployment whose previous
    container was unhealthy would delete every database copy on the host.

    The database is copied, not moved: it may be the only copy on the host, and a crash
    before ``backup.new/`` is swapped in leaves a directory the next deployment discards as
    incomplete. The original stays in ``attempts/`` until the swap is done.
    """
    source = legacy_attempt_with_database(prod)
    if source is None:
        return False
    compose_bytes, manifest = legacy_backup_record(prod, source)
    os.mkdir(backup_new)
    if compose_bytes is not None:
        write_atomic(backup_new / "compose.yml", compose_bytes)
    write_atomic(backup_new / "current.json", manifest)
    write_atomic(backup_new / "snapshot.json", snapshot_record(source.name))
    # The database comes last, and appears whole under its name through a rename: its
    # presence is what tells settle_backups this directory is complete. Its data is on
    # disk before it takes that name.
    partial = backup_new / ".database.sqlite3.tmp"
    shutil.copyfile(source / "database.sqlite3", partial)
    fsync_file(partial)
    os.replace(partial, backup_new / "database.sqlite3")
    settle_database(prod)
    return True


def rotate(prod: Path, candidate: Manifest, previous: Live | None, snapshot: Path | None) -> None:
    """Make the healthy candidate the live deployment, and the previous one the backup.

    The order bounds what a crash can leave: every file is replaced atomically, and at
    worst ``current.json`` still describes the previous deployment while the candidate
    runs. The next deployment then finds the container does not match that manifest,
    skips its backup, and deploys normally.

    The backup is replaced only when there is a database to replace it with: this
    deployment's snapshot, or one carried forward from ``failed/`` (``carried_snapshot``).
    One without either -- the previous container was unhealthy, or had no database --
    leaves ``backup/`` as it was, rather than deleting the only copy of the database for
    one that has none. If ``backup/`` holds no database either and the legacy
    ``attempts/`` is about to be deleted, the newest legacy attempt holding a database
    becomes the backup instead.

    No older copy -- ``backup.old/``, ``failed/``, ``attempts/`` -- is removed before the
    database replacing it is fsynced under its final name.
    """
    incoming = prod / "incoming"
    backup_dir, backup_new = prod / "backup", prod / "backup.new"

    # 1. Build the new backup beside the old one. The database goes in LAST, by a rename:
    # settle_backups treats a backup.new/ holding database.sqlite3 as complete and
    # promotes it after a crash, so nothing may follow the database into it.
    settle_backups(prod)
    if snapshot is not None:
        os.mkdir(backup_new)
        if previous is not None:
            write_atomic(backup_new / "compose.yml", previous.compose_file.read_bytes())
            write_atomic(backup_new / "current.json", (prod / "current.json").read_bytes())
        # This deployment's snapshot, or one carried from failed/: either way it says
        # which attempt took it, and the record goes in before the database does.
        taken_by = snapshot_taken_by(snapshot.parent, request_fallback=True)
        write_atomic(backup_new / "snapshot.json", snapshot_record(taken_by))
        os.rename(snapshot, backup_new / "database.sqlite3")
        settle_database(prod)
    elif os.path.lexists(prod / "attempts") and not os.path.lexists(
        backup_dir / "database.sqlite3"
    ):
        if not seed_backup_from_attempts(prod, backup_new):
            warn(
                "No database backup exists on this host: the previous deployment could not "
                "be snapshotted, and the previous layout held no copy."
            )

    # 2 and 3. The live pair: the compose file first, then the manifest.
    write_atomic(prod / "compose.yml", (incoming / "compose.yml").read_bytes())
    write_atomic(prod / "current.json", json_bytes(candidate))

    # 4. Swap the backup in.
    if os.path.lexists(backup_new):
        swap_backup(prod)

    # 5 and 6.
    write_atomic(prod / "compose.sh", compose_script(candidate).encode("utf-8"), mode=0o700)
    write_atomic(prod / "last-attempt.json", json_bytes(candidate))

    # 7. A success supersedes a failure's evidence, and the legacy attempts/.
    for leftover in (incoming, prod / "failed", prod / "attempts"):
        remove_tree(leftover)


def record_failure(prod: Path, result: Manifest, carry: Path | None = None) -> str:
    """Keep the failed attempt as ``failed/``, and return where, for the error message.

    ``compose.yml``, ``current.json`` and ``backup/`` are not touched: they still describe
    what is running. An attempt that took no snapshot moves ``carry`` -- the previous
    ``failed/`` database, see ``carried_snapshot`` -- into its own evidence first, so
    replacing ``failed/`` never deletes it. Recording can itself fail; that is reported
    rather than allowed to hide the failure it was recording.
    """
    incoming, failed = prod / "incoming", prod / "failed"
    database = incoming / "database.sqlite3"
    try:
        if carry is not None and not database.exists():
            taken_by = snapshot_taken_by(carry.parent, request_fallback=True)
            write_atomic(incoming / "snapshot.json", snapshot_record(taken_by))
            os.rename(carry, database)
            fsync_file(database)
            fsync_directory(incoming)
        write_atomic(incoming / "result.json", json_bytes(result))
        remove_tree(failed)
        os.rename(incoming, failed)
        fsync_directory(prod)
        write_atomic(prod / "last-attempt.json", json_bytes(result))
    except OSError as error:
        return f"not recorded ({describe(error)})"
    return display(failed)


def copy_name(instant: datetime) -> str:
    """The name the application gives a copy started at ``instant``, which must be aware.

    ``portfolio-YYYYMMDDTHHMMSSffffffZ.sqlite3`` in UTC: the only shape its
    ``BACKUP_NAME_PATTERN`` accepts, and so the only one ``restore-backup`` restores.
    """
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("A copy's instant must be timezone-aware")
    return f"portfolio-{instant.astimezone(UTC):%Y%m%dT%H%M%S%f}Z.sqlite3"


def safety_copy_name(output: str) -> str | None:
    """The safety copy ``restore-backup`` reports in ``output``, or None.

    Read from the one line that mentions a safety copy, and only as a copy's name: nothing
    else of that output may be recorded (spec 034, R5). None when no line, or more than one,
    mentions it, or the line names no single copy -- there was no live database to copy, or
    it was too damaged to copy and was moved aside instead.
    """
    lines = [line for line in output.splitlines() if "safety copy" in line]
    if len(lines) != 1:
        return None
    names = re.findall(rf"\b{COPY_NAME.pattern}\b", lines[0])
    return names[0] if len(names) == 1 else None


def roll_back_database(
    prod: Path,
    candidate: Manifest,
    candidate_compose: Path,
    previous: Live,
    secrets_file: Path,
    result: Manifest,
) -> None:
    """Put the database back as the previous deployment left it, if the candidate moved it.

    The application migrates its database forward when it starts, never backwards, and an
    image that does not know the database's revision refuses to start. So a candidate that
    migrated and then failed would leave the previous image a database it cannot start on.
    Spec 034:

    1. Stop the candidate, so that nothing writes while the revision is read or the
       database replaced. Compose does not restart a container it stopped, and the ``up``
       that follows replaces it.
    2. Read the live revision in a one-off container of the previous image (the same image,
       volumes and user the rollback starts), with ``REVISION_SCRIPT``.
    3. Compare it with the revision of the attempt's own snapshot:
       - equal: ``unchanged``, and nothing is touched;
       - different, with no snapshot of the attempt's own, or one holding no single
         revision: ``not_restored``. A carried snapshot can be older than writes made
         since, and replacing them is a person's call (R6); a snapshot without one
         revision is one ``restore-backup`` refuses (R11);
       - different, with one: stream the snapshot into the backups volume under a copy's
         name (``STREAM_SCRIPT``), then restore it with the previous image's own
         ``restore-backup`` (spec 029), which checks the copy and that the image knows its
         revision, takes a safety copy of the migrated database first, and writes through
         the backup API. ``restored``.

    Only a failed restore raises (R12). A stream or restore that fails is
    ``restore_failed``: the revision is known to have moved, so the previous image is not
    started on a database it cannot read. A stop or revision read that fails has changed
    nothing, so it is ``unread``, with ``database_error``, and returns: the rollback starts
    the previous image as it did before this step existed. Every field goes into ``result``
    as soon as it is known, so a failure part-way still records how far this got.

    ``restore-backup`` prints the rows per table, which spec 029 keeps out of every log, and
    the error message reaches a public Actions log: nothing of its output but the safety
    copy's name ever enters ``result`` (R5).

    Needs a previous image that has ``restore-backup``: v0.29.0 or later (R10). A rollback
    target is always the live image, and every one from now on is newer than that, so this
    is stated here rather than checked.

    The ``run`` options are Docker Compose's documented ones (``docker compose run``
    reference, read 2026-10-05): ``--rm`` removes the container and overrides its restart
    policy, ``--no-deps`` starts no linked service, ``-T`` allocates no terminal, stdin is
    attached by default (``--interactive``), no port of the service is published, and the
    service's volumes and environment apply. Assumed, not documented: ``stop`` of a service
    with no container succeeds and changes nothing.
    """
    try:
        compose(candidate, candidate_compose, secrets_file, "stop", "app")
        output = compose(
            previous.manifest, previous.compose_file, secrets_file,
            *ONE_OFF, "python", "-c", REVISION_SCRIPT, DATABASE,
        )
        live = None if output == "absent" else output
        if live is not None and not DATABASE_REVISION.fullmatch(live):
            raise DeploymentError("The live database's schema revision could not be read")
    except Exception as error:
        # Nothing has been restored or changed, so this decides nothing: the rollback goes
        # on as it did before this step existed (R12). A compose file compose rejects, for
        # one, fails here although the live container was never touched.
        result["database"] = "unread"
        result["database_error"] = reason(error)
        return
    result["database_revision_live"] = live
    if live == candidate.get("database_revision"):
        result["database"] = "unchanged"
        return
    if not candidate.get("backup") or candidate.get("database_revision") is None:
        # No snapshot of its own, or one holding no single revision, which restore-backup
        # would refuse: nothing this attempt can put back (R6, R11).
        result["database"] = "not_restored"
        return
    snapshot = prod / "incoming" / "database.sqlite3"
    # Stamped when the restore starts, as the copy's name records when it was taken.
    name = copy_name(datetime.now(UTC))
    try:
        streamed = compose(
            previous.manifest, previous.compose_file, secrets_file,
            *ONE_OFF, "python", "-c", STREAM_SCRIPT, BACKUP_DIRECTORY, name,
            input_file=snapshot,
        )
        if streamed != f"ok {snapshot.stat().st_size}":
            raise DeploymentError("The snapshot did not reach the backups volume whole")
        result["database_restored_from"] = name
        restored = compose(
            previous.manifest, previous.compose_file, secrets_file,
            *ONE_OFF, "python", "-m", "portfolio", "restore-backup", name,
        )
    except Exception as failure:
        result["database"] = "restore_failed"
        # The command prints its report on stdout, and run() quotes stdout when stderr is
        # empty: a restore that reported and was then killed would put the rows per table
        # into rollback_error. Of that report only the safety copy's name is kept, which is
        # where the migrated database now is. Refusals and errors go to stderr, and are kept.
        if ROWS_REPORT in str(failure):
            safety = safety_copy_name(str(failure))
            if safety is not None:
                result["database_safety_copy"] = safety
            raise DeploymentError(
                "restore-backup failed after it reported a result; its output is not recorded"
            ) from None
        raise
    safety = safety_copy_name(restored)
    if safety is not None:
        result["database_safety_copy"] = safety
    result["database"] = "restored"


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
                    try:
                        return deploy_locked(
                            environment, image, revision, version, run_number, source_run_url, root
                        )
                    except DeploymentError as error:
                        if directory == root:
                            raise
                        # Locked in the legacy root, so it has been migrated, and stays so.
                        raise DeploymentError(
                            f"{error} The deployment root was migrated to {display(root)} "
                            "first, and that stands."
                        ) from error
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
    if previous is not None:
        write_live_compose_script(prod, previous)
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
    attempt = new_attempt_id(prod)
    candidate: Manifest = {
        "layout": MANIFEST_LAYOUT,
        "environment": environment,
        "image": image,
        "revision": revision,
        "version": version,
        "status": "pending",
        "attempt": attempt,
        # The deployment this attempt replaces, and so the one its snapshot comes from.
        "replaces": deployment_id(previous.manifest) if previous is not None else None,
        "delivery": {"run_number": run_number, "source_run_url": source_run_url},
    }

    # Settle what a crashed rotation left of the backup first: the carry-forward below
    # must compare against the backup as it really is.
    settle_backups(prod)

    # Stage the candidate. What an interrupted deployment left behind is kept as failed/
    # if it holds a snapshot, and deleted otherwise.
    incoming = prod / "incoming"
    retire_incoming(prod)
    os.mkdir(incoming)
    candidate_compose = incoming / "compose.yml"
    write_atomic(candidate_compose, kit_compose_file().read_bytes())
    write_atomic(incoming / "request.json", json_bytes(candidate))

    snapshot = incoming / "database.sqlite3"
    candidate["backup"] = False
    # The schema revision this attempt's snapshot holds: the one the live deployment ran
    # on. A rollback compares the database with it to tell whether this attempt migrated.
    candidate["database_revision"] = None
    if previous is not None:
        try:
            # docker cp writes in place, so it writes a temporary name: only a finished,
            # fsynced copy is ever called database.sqlite3.
            partial = incoming / ".database.sqlite3.tmp"
            taken = backup(previous.manifest, previous.compose_file, secrets, partial, attempt)
            if taken is not None:
                fsync_file(partial)
                write_atomic(incoming / "snapshot.json", snapshot_record(attempt))
                os.replace(partial, snapshot)
                fsync_directory(incoming)
                candidate["backup"] = True
                candidate["database_revision"] = taken.revision
        except Exception as failure:
            evidence = record_failure(
                prod,
                dict(candidate, status="failed", stage="backup", error=reason(failure)),
                carried_snapshot(prod, previous),
            )
            raise DeploymentError(
                f"The backup failed before the service was replaced: {reason(failure)}; "
                f"evidence={evidence}"
            ) from failure

    # With no snapshot of its own, this attempt keeps the one a failed attempt took of the
    # same live deployment: on success as the backup, on failure in its own failed/.
    carry = None if candidate["backup"] else carried_snapshot(prod, previous)
    if carry is not None:
        try:
            carried_from = read_json(carry.parent / "request.json").get("attempt")
        except (OSError, ValueError):
            carried_from = None
        candidate["backup_carried_from"] = carried_from

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
        result = dict(candidate, status="failed", error=reason(failure))
        try:
            if previous is not None:
                # The candidate may have migrated the database, which the previous image
                # could not start on; this stops it and, if so, puts the snapshot back.
                roll_back_database(prod, candidate, candidate_compose, previous, secrets, result)
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
            result["rollback_error"] = reason(rollback_failure)
        evidence = record_failure(prod, result, carry)
        # The state only, never a revision, a copy's name or an error's text: this line
        # reaches the public Actions log, and failed/result.json on the host has the rest.
        database = f"; database={result['database']}" if "database" in result else ""
        raise DeploymentError(
            f"Deployment failed; rollback={result['rollback']}{database}; evidence={evidence}"
        ) from failure

    candidate.update(status="healthy", deployed_at=datetime.now(UTC).isoformat())
    try:
        rotate(prod, candidate, previous, snapshot if candidate["backup"] else carry)
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
        warn(reason(error))
        return 1
    except Exception:  # noqa: BLE001 - a bug: report it, but never with an absolute path
        warn("deploy.py stopped on an unexpected error:\n" + traceback.format_exc())
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
