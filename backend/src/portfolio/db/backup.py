"""Copies of the SQLite database: taking one, listing them, and restoring one (spec 029).

Synchronous, with the standard library's `sqlite3`, and run in a worker thread by the
service: the backup API is a C call that holds the thread for as long as the copy takes, and
the event loop is the one thing in the process that must not wait on it. Nothing here goes
through the application's engine, so a copy is taken beside the running application without
borrowing a connection from its pool.

Every connection opened here is closed before the next step starts.

## Taking a copy

1. Delete the temporary files an earlier crash left: `.portfolio-<stamp>.partial` and its
   `-wal`, `-shm` and `-journal`, and **only** regular files whose name is exactly that and
   whose modification time is more than an hour old (ruling R6; see *Two processes copying
   at once*). Then open the live database **read-only** (`mode=ro`), so the code that copies
   the owner's data cannot write to it.
2. Copy it with `Connection.backup` in **one step** (`pages=-1`) into a temporary file in
   the backup directory. In WAL mode a reader does not block a writer, and one step is one
   read transaction, so the copy is one consistent snapshot. Measured on 2026-10-02 (spec
   029): a 65 MB database with a writer committing two-table transactions throughout; each
   one-step copy took under 200 ms while about 8,000 commits landed, and every copy passed
   `integrity_check` with both tables in step and none of the commits made during the step.
3. Set `PRAGMA journal_mode=DELETE` on the copy and close it. **This is not optional.** The
   backup copies page 1 with the rest, and page 1's header records the journal mode, so the
   copy of a WAL database *is* a WAL database: measured, the destination connection reports
   `wal` right after the step and a `-wal` and `-shm` appear beside the temporary file.
   After the pragma the header says `delete` and the closed copy is one file with nothing
   beside it.
4. Reopen the copy read-only and run `PRAGMA integrity_check`. Anything but a single `ok`
   row is `integrity_failed`, and so is an `alembic_version` that is missing or does not
   hold exactly one row. **A copy that cannot be opened is `storage_error`** (R6): SQLite's
   `SQLITE_CANTOPEN` or any of the `SQLITE_IOERR` family says the file the backup step just
   wrote is not there to read, which is a fault of the backup directory and says nothing
   about the database the copy came from. `integrity_failed` is kept for a check that ran.
5. `fsync` the file, rename it to its final name, `fsync` the directory.

On any failure the temporary file is removed and a `BackupError` carries one
`BackupErrorKind`. A copy that is not kept leaves nothing behind.

### Why a copy ends by opening the live database read-write

**A read-only connection that closes last leaves the `-wal` and `-shm` files behind**,
measured with SQLite 3.49 on 2026-10-02 and recorded as ruling R1 of spec 029. SQLite
deletes them when the last connection closes cleanly, but a read-only connection cannot
checkpoint, so it does not. With the application running that changes nothing -- the
application's own connections close later and clean up. With it stopped, `python -m
portfolio backup` would leave a `-wal` file beside a database nothing has open, and
`restore_copy` would read it as "the application is running" and refuse.

So once the read-only connection is closed, `release_wal` opens the live database
read-write, reads `PRAGMA schema_version` and closes it. If that connection is the last one,
SQLite checkpoints and deletes both files; while the application holds a connection it is a
no-op. **Best effort**: the copy has already been taken, so an error here is logged and does
not fail it.

**The copy itself is read-only; the release is the one write, and only after an unclean
stop.** After a clean close there is nothing in the `-wal` to checkpoint. After a crash or a
power cut with the application stopped, the `-wal` still holds committed transactions, and
the release's connection -- the first to open the database since -- recovers them and, as
the last to close, checkpoints them into the live file: what the application's own next
start would have done, done a little earlier. The copy, read before the release, already
holds those transactions, because a reader sees the `-wal`'s committed frames.

### Two processes copying at once

The application's timer and an operator's `backup` can run at the same moment in two
containers. Two things keep them from damaging each other (ruling R6):

* **Step 1 leaves young files alone.** A copy takes well under a second, so a temporary file
  less than an hour old is one a process may still be writing, and only an older one is an
  earlier crash's. Before this rule, one process's clean-up could delete the other's copy in
  flight. A copy that takes longer than an hour could still lose its file to another
  process's step 1, and then fails as `storage_error`.
* **A copy whose file disappeared fails as `storage_error`, not `integrity_failed`**, because
  of step 4's rule. `integrity_failed` tells the operator to suspect the live database,
  which would have sent them after a fault that was not there.

A lock was considered and not taken: `fcntl` does not exist on Windows, where the test suite
also runs, and the two rules above remove the harm a lock would have prevented.

## Restoring

`restore_copy` follows spec 029, *Restoring*:

1. **Refuse while the database is open**: a `-wal` file beside it means the application is
   running, or stopped without closing it cleanly. **This check is the only guard.** Measured:
   `Connection.backup` into a live WAL database succeeds while a second connection holds it
   open, and that connection reads the restored rows at once. Nothing in SQLite stops a
   restore under a running application, so this check has to. **And refuse a leftover
   rollback journal** (ruling R16): when there is no live database file but a non-empty
   `-journal` lies beside its path -- after R15's journal move failed, say -- that journal
   belongs to a database that is gone, and step 5 would make SQLite discard it. Measured
   with SQLite 3.49.1 on 2026-10-02: a hot journal beside a missing file was deleted when the
   backup API wrote the new file, which SQLite does to a journal beside a database of zero
   pages. The restore would succeed, and the damaged file's journal would be lost. When the
   live database exists, a journal beside it is SQLite's to recover, and nothing is refused.
2. Refuse a name that is not a copy's name or is not in the directory, and a copy with a
   `-wal` beside it that holds frames (R13): such a file is not a self-contained copy, and
   step 3 would not see what the `-wal` holds -- see *The chosen copy is read immutable too*.
3. Check the copy: `integrity_check`, one row in `alembic_version`, and a revision this
   image's migrations know. A revision it does not know was written by a newer version,
   which this one cannot migrate, so it is refused with both ids.
4. Take a copy of the live database first, with `take_copy` -- the same code as a scheduled
   copy -- so a restore can be undone by restoring that one. **Skipped when there is no live
   database** (ruling R2): a fresh data volume beside a surviving backups volume is the case
   the second volume exists for, and there is nothing to protect. **When the safety copy
   fails, the live file decides** -- see *A damaged live database* below.
5. Copy the chosen backup **into** the live file with `Connection.backup`, the copy as the
   source and the live file as the destination. No file is renamed or deleted, so no stale
   `-wal` can be replayed over the result. Measured: a `journal_mode=DELETE` source into a
   WAL destination works and the destination stays WAL; the restored pages sit in its `-wal`
   while the connection is open, and are checkpointed and removed when it closes as the
   last connection. Into a missing file it creates a `delete`-mode database, which the
   engine switches to WAL on its first connection. **A different page size fails**: a WAL
   destination cannot change its page size, and SQLite answers "attempt to write a readonly
   database". Every copy is taken from the live database and has its page size, so only a
   file from elsewhere meets this.
6. `integrity_check` on the live database and the rows per table, **over a read-write
   connection**, which closes last and so leaves no `-wal` behind: the application can start
   at once. The counts must equal the copy's, read in step 3.

The row counts are `SELECT COUNT(*)` per table. They count rows, not money, so the rule
against aggregating money in SQL does not apply.

### A damaged live database (ruling R3)

A live database that is damaged is exactly when the owner needs a restore, and it is also
what makes step 4 fail: its copy does not pass the check. Measured with SQLite 3.49 on
2026-10-02, a scribbled index page and a 0-byte file fail as `integrity_failed` (the 0-byte
file is an empty database with no `alembic_version`), and a damaged header fails as
`database_error` (`SQLITE_NOTADB`). Skipping the safety copy is not enough, because
**step 5 cannot write into a file whose header is damaged**: the backup API answers
`SQLITE_NOTADB` for the destination too.

So when step 4 fails with `integrity_failed` or `database_error`, the restore checks the live
file itself: it must open, pass `integrity_check`, and hold exactly one `alembic_version`
row.

* **It passes**: the failure was in writing the copy, not in the database. The restore
  refuses with the original error, and nothing is moved.
* **It cannot be read** -- `SQLITE_CANTOPEN` or the `SQLITE_IOERR` family, the split R6 makes
  for a copy (ruling R12): an I/O fault or a permission says nothing about what the file
  holds, and a healthy file that could not be read at that moment must not be called
  damaged. The restore refuses, and the file stays where it is.
* **It opens and is wrong** -- `SQLITE_NOTADB`, `SQLITE_CORRUPT`, a check that does not
  answer `ok`, an `alembic_version` missing or not holding one row, which is what a 0-byte
  file is: the live file is **moved aside** to `<name>.damaged-<UTC stamp>` beside it --
  `portfolio.db.damaged-20261002T093012345678Z` -- a name nothing lists, rotates or
  restores, and the directory is fsynced. A target that already exists is refused rather
  than overwritten. Moving it is safe because step 1 found no `-wal`, so the file is the
  whole database, and nothing of the owner's is deleted. A `-journal` beside it is moved
  with it, as `<damaged name>-journal`, because a rollback journal belongs to the file it
  was written for, and step 5 would make SQLite discard it if it stayed (R16). **It is often
  gone by then**: step 4 ends, as every copy does, by opening the live file read-write
  (R1), and SQLite recovers a `-journal` on that open, rolling it back into the file or
  removing it. So the journal moves only when SQLite has not already used it. A `-shm`, and a
  `-wal` this restore's own reads left empty, are removed so that they cannot be paired with
  the new file. The restore then goes on as if there were no live database (R2).

A `storage_error` from step 4 -- the backup directory is full or cannot be written -- says
nothing about the live database, so it refuses, and the live file is untouched.

The live file's check opens it with `mode=ro&immutable=1`: measured, a plain read-only
connection leaves a `-wal` and `-shm` beside a WAL database (R1), and an immutable one
leaves neither while reaching the same verdict on all three kinds of damage. Immutable is
truthful here for the reason the move is safe: there is no `-wal` to read, and the
application is stopped.

### The chosen copy is read immutable too (ruling R11)

Steps 3 and 5 both read the chosen copy, and both open it with `mode=ro&immutable=1`. A copy
this module took is in `delete` mode and nothing appears beside it either way; but a copy
whose header says WAL -- one brought from elsewhere -- gained a `-wal` and `-shm` in the
backup directory from each plain read-only open, measured by `tester-22`, and a whole
restore left both behind. Immutable is truthful here as well: nothing writes to a copy, so
the file is the whole of it. **Only the file is read**, so a `-wal` beside a copy from
elsewhere is not -- and a `-wal` that holds frames would be transactions silently left out.
Step 2 therefore refuses such a copy (ruling R13), and says how to make it one file: open it
once with `sqlite3` outside the backup directory, which checkpoints the `-wal` into it, and
copy it back alone. An empty `-wal` holds nothing, and is no reason to refuse.

## Messages

A `BackupError` or `RestoreRefusedError` message names paths, revisions, SQLite's own error
text and what to do. It never holds row data. The service logs only the kind and a class
name, and the command line prints the message to the operator's terminal.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from portfolio.db.alembic_config import build_alembic_config
from portfolio.domain.backups import BackupErrorKind, backup_name, instant_of, utc_stamp

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

__all__ = [
    "TEMPORARY_FILE_MAX_AGE",
    "TEMPORARY_NAME_PATTERN",
    "TEMPORARY_PREFIX",
    "BackupError",
    "BackupFile",
    "RestoreRefusal",
    "RestoreRefusedError",
    "RestoreResult",
    "damaged_path",
    "database_path",
    "known_schema_revisions",
    "list_copies",
    "release_wal",
    "remove_copies",
    "restore_copy",
    "take_copy",
    "wal_path",
]

TEMPORARY_PREFIX: Final = ".portfolio-"
"""How every temporary file this module writes begins. The leading dot keeps it out of `ls`."""

TEMPORARY_NAME_PATTERN: Final = re.compile(
    r"\A\.portfolio-\d{8}T\d{12}Z\.partial(?:-wal|-shm|-journal)?\Z"
)
"""A temporary file's whole name, or one SQLite keeps beside it while it is open: the stamp
`utc_stamp` writes, to the digit, so step 1 never matches a file this module did not name."""

TEMPORARY_FILE_MAX_AGE: Final = timedelta(hours=1)
"""How old a temporary file must be before step 1 deletes it. A copy takes well under a
second, so a younger one may belong to a copy another process is still writing (R6)."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NANOSECONDS_PER_MICROSECOND: Final = 1_000

_SYNC_DIRECTORIES: Final = os.name == "posix"
"""Whether a directory can be opened to `fsync` it. Windows cannot; the tests that run there
do not depend on durability. A module constant so a test can flip it."""

_WRITE_SIDE_ERRORS: Final = frozenset(
    {sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR_WRITE, sqlite3.SQLITE_IOERR_FSYNC}
)
"""SQLite errors from a backup step that can only come from writing the copy.

The source is opened read-only, so it never writes, and these three are a full disk, a
failed write and a failed sync. Anything else from that step is read as the live database's.
"""

_PRIMARY_CODE_MASK: Final = 0xFF
"""An extended result code's low byte is its primary code: `SQLITE_IOERR_READ` is 266, and
`266 & 0xFF` is `SQLITE_IOERR`, 10."""

_CANNOT_OPEN_ERRORS: Final = frozenset({sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_IOERR})
"""Primary codes that say a file could not be opened or read at all, rather than that what
was read is wrong. On the copy just written they are the backup directory's fault (R6)."""

_logger = structlog.get_logger(__name__)


class BackupError(Exception):
    """A copy could not be taken, checked, kept or restored. `error_kind` says which way.

    **It copies and pickles**: `BaseException` rebuilds an instance by calling the class with
    `self.args`, which here is the message alone, so `__reduce__` hands back both arguments.
    """

    def __init__(self, error_kind: BackupErrorKind, message: str) -> None:
        """Keep the kind as an attribute and the message as the text."""
        super().__init__(message)
        self.error_kind = error_kind

    def __reduce__(self) -> tuple[Callable[..., BackupError], tuple[object, ...]]:
        """Rebuild with both arguments. Typed loosely, so a subclass can add its own."""
        return (type(self), (self.error_kind, str(self)))


class RestoreRefusal(StrEnum):
    """Why a restore was refused before it changed anything."""

    DATABASE_OPEN = "database_open"
    """A `-wal` file is beside the live database: the application is running, or stopped
    without closing the database cleanly. Also a `-wal` with content that appeared during the
    restore, before a damaged live database was moved aside."""

    UNKNOWN_BACKUP = "unknown_backup"
    """The name is not a copy's name, or no copy by that name is in the directory."""

    NEWER_SCHEMA = "newer_schema"
    """The copy's schema revision is one this version's migrations do not know."""

    NOT_SELF_CONTAINED = "not_self_contained"
    """A `-wal` beside the copy holds frames, so the file alone is not the whole copy."""

    LEFTOVER_JOURNAL = "leftover_journal"
    """There is no live database file, but a non-empty `-journal` lies beside its path (R16)."""


class RestoreRefusedError(Exception):
    """A restore refused before it wrote or moved anything. Copies and pickles."""

    def __init__(self, reason: RestoreRefusal, message: str) -> None:
        """Keep the reason as an attribute and the message as the text."""
        super().__init__(message)
        self.reason = reason

    def __reduce__(self) -> tuple[type[RestoreRefusedError], tuple[RestoreRefusal, str]]:
        """Rebuild with both arguments."""
        return (type(self), (self.reason, str(self)))


class CheckFailedError(Exception):
    """A copy opened and read, and failed a check. Its message names the check, never a row.

    Internal: it never leaves this module except as the `__cause__` of a `BackupError`, where
    its name is what the service logs as `error_type` for an `integrity_failed` copy.
    """


@dataclass(frozen=True, slots=True)
class BackupFile:
    """One copy in the backup directory: its name, the instant the name records, its size."""

    name: str
    started_at: datetime
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What a restore did.

    `safety_copy` is the name of the copy of the live database taken before it was
    overwritten, or `None` when there was no live database or it was damaged. `row_counts` is
    the number of rows in each table of the live database afterwards, by table name in order.
    `damaged` is where a live database that failed its own check was moved (ruling R3), and
    `None` otherwise; at most one of `safety_copy` and `damaged` is set.
    """

    restored: str
    safety_copy: str | None
    row_counts: Mapping[str, int]
    damaged: Path | None = None


def database_path(database_url: str) -> Path:
    """The file a SQLite database URL names, as an absolute path.

    Raises:
        BackupError: `database_error`, for a URL that names no file -- another dialect, an
            in-memory database, or no database at all. Raised when a copy is attempted rather
            than at startup, because the test suite runs the application on such URLs.
    """
    url = make_url(database_url)
    database = url.database
    if (
        not url.drivername.startswith("sqlite")
        or not database
        or database == ":memory:"
        or url.query.get("mode") == "memory"
    ):
        message = (
            "PORTFOLIO_DATABASE_URL names no database file, so there is nothing to copy. "
            "Backups need a file-backed SQLite database."
        )
        raise BackupError(BackupErrorKind.DATABASE_ERROR, message)
    return Path(database).expanduser().resolve()


def wal_path(database: Path) -> Path:
    """The write-ahead log SQLite keeps beside `database` while a connection is open."""
    return database.with_name(f"{database.name}-wal")


def damaged_path(database: Path, moved_at: datetime) -> Path:
    """Where a restore moves a live database that failed its own check (ruling R3).

    Beside it, in the data directory, as `<name>.damaged-<UTC stamp>`: a name no code here
    lists, rotates or restores, and one the operator can read the time of.

    Raises:
        ValueError: `moved_at` is naive.
    """
    return database.with_name(f"{database.name}.damaged-{utc_stamp(moved_at)}")


def take_copy(database: Path, directory: Path, started_at: datetime) -> BackupFile:
    """Copy `database` into `directory` under the name `started_at` gives it, and check it.

    The five steps of the module docstring, then `release_wal`. `started_at` must be
    timezone-aware; it names the copy, so it should be the instant the caller started, and it
    is the "now" step 1 measures a temporary file's age against.

    Raises:
        BackupError: with `database_error`, `integrity_failed` or `storage_error`. The
            temporary file is gone by then, and nothing else was changed.
    """
    name = backup_name(started_at)
    _prepare_directory(directory, started_at)
    if not database.is_file():
        message = f"There is no database at {database} to copy."
        raise BackupError(BackupErrorKind.DATABASE_ERROR, message)
    # `.portfolio-<stamp>.partial`, the shape `TEMPORARY_NAME_PATTERN` matches. Built from the
    # stamp rather than from `name`, which already begins `portfolio-`.
    temporary = directory / f"{TEMPORARY_PREFIX}{utc_stamp(started_at)}.partial"
    final = directory / name
    kept = False
    try:
        try:
            _copy_into(database, temporary)
        finally:
            # After the read-only connection has closed, whatever became of the copy: it is
            # that connection's leftovers this removes. See the module docstring.
            release_wal(database)
        _check_copy(temporary, database)
        size = _rename_durably(temporary, final)
        kept = True
    finally:
        if not kept:
            _remove_temporary(temporary)
    return BackupFile(name=name, started_at=started_at, size_bytes=size)


def release_wal(database: Path) -> None:
    """Let SQLite remove the `-wal` and `-shm` a read-only connection left. **Never raises.**

    Opens `database` read-write without creating it, reads `PRAGMA schema_version` -- a read
    is what makes the connection open the log at all -- and closes it. If no other connection
    is open, that close is the last one, and SQLite checkpoints and deletes both files; while
    the application holds a connection it changes nothing. Why it is needed is in the module
    docstring, and is ruling R1 of spec 029.

    A failure is logged at warning, with the exception's class name only, and swallowed: the
    copy this follows has already been taken. What it costs is a `-wal` file that makes the
    next restore refuse until the application is started and stopped once.

    After an unclean stop this is a write: it checkpoints the committed transactions the
    `-wal` still holds into the live file, as the application's next start would.
    """
    try:
        with closing(sqlite3.connect(_uri(database, "rw"), uri=True)) as connection:
            connection.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error as exc:
        _logger.warning("backup_wal_release_failed", error_type=type(exc).__name__)


def list_copies(directory: Path) -> tuple[BackupFile, ...]:
    """Every copy in `directory`, newest first. A directory that does not exist holds none.

    Only regular files whose name `instant_of` accepts are copies; everything else is
    ignored. A copy removed while the directory is read is skipped rather than reported.

    Raises:
        BackupError: `storage_error`, when the directory cannot be read.
    """
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return ()
    except OSError as exc:
        message = f"The backup directory {directory} cannot be read: {exc}"
        raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
    copies: list[BackupFile] = []
    for entry in entries:
        started_at = instant_of(entry.name)
        if started_at is None:
            continue
        try:
            if not entry.is_file():
                continue
            size = entry.stat().st_size
        except FileNotFoundError:
            continue
        except OSError as exc:
            message = f"The backup {entry} cannot be read: {exc}"
            raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
        copies.append(BackupFile(name=entry.name, started_at=started_at, size_bytes=size))
    return tuple(sorted(copies, key=lambda copy: copy.started_at, reverse=True))


def remove_copies(directory: Path, names: Iterable[str]) -> tuple[str, ...]:
    """Delete these copies from `directory`, ignoring one already gone. Returns `names`.

    Raises:
        ValueError: a name is not a copy's name. Rotation only ever passes names
            `list_copies` returned, so this is a defect, and it fails before anything is
            deleted rather than after.
        BackupError: `storage_error`, when a file cannot be deleted. The ones before it are
            gone.
    """
    chosen = tuple(names)
    for name in chosen:
        if instant_of(name) is None:
            message = "rotation was asked to delete a file that is not a copy"
            raise ValueError(message)
    for name in chosen:
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError as exc:
            message = f"An old backup, {directory / name}, could not be deleted: {exc}"
            raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
    return chosen


def known_schema_revisions() -> tuple[frozenset[str], str]:
    """Every revision this image's migrations know, and the newest of them.

    Read from the packaged migration scripts, the ones `upgrade_to_head` runs, so the answer
    is the image's and not the database's. The URL `build_alembic_config` takes is not read
    to list scripts, so none is given.
    """
    scripts = ScriptDirectory.from_config(build_alembic_config(""))
    revisions = frozenset(script.revision for script in scripts.walk_revisions())
    head = scripts.get_current_head()
    if head is None:  # pragma: no cover - the package always ships its migrations
        message = "the packaged migrations have no head revision"
        raise RuntimeError(message)
    return revisions, head


def restore_copy(
    database: Path,
    directory: Path,
    name: str,
    *,
    clock: Callable[[], datetime],
) -> RestoreResult:
    """Replace the live database's contents with the copy `name`. Steps 1 to 6 above.

    `clock` names the safety copy of step 4, read when that copy starts, and a damaged live
    database's new name, read when it is moved aside.

    Raises:
        RestoreRefusedError: steps 1 to 3 refused, and nothing was written anywhere; or a
            `-wal` with content appeared beside a damaged live database before it was moved.
        BackupError: `integrity_failed` for a copy that does not pass step 3 (nothing was
            written) or a live database that does not pass step 6; `database_error` when the
            live database's directory is missing, or the live database cannot be written,
            read or moved aside; and, with the kind step 4 failed with, when no safety copy
            could be taken of a live database that passes its own check, or none could be
            written at all (`storage_error`). Nothing was written in either of those. A
            message after step 4 says where the database as it was is.
    """
    _refuse_while_open(database)
    _refuse_leftover_journal(database)
    if not database.parent.is_dir():
        message = (
            f"The database's directory {database.parent} does not exist. Check "
            "PORTFOLIO_DATABASE_URL, and that the data volume is mounted."
        )
        raise BackupError(BackupErrorKind.DATABASE_ERROR, message)
    chosen = _chosen_copy(directory, name)
    _refuse_unless_self_contained(chosen)
    expected = _check_backup(chosen)
    safety: str | None = None
    damaged: Path | None = None
    if database.is_file():
        safety, damaged = _protect_live_database(database, directory, clock)
    before = _before_sentence(safety, damaged)
    _copy_over(chosen, database, before)
    restored = _check_restored(database, before)
    if restored != expected:
        message = (
            f"After the restore, the rows per table of {database} differ from {name}'s. {before}"
        )
        raise BackupError(BackupErrorKind.INTEGRITY_FAILED, message)
    return RestoreResult(restored=name, safety_copy=safety, row_counts=restored, damaged=damaged)


def _uri(path: Path, mode: str) -> str:
    """A `file:` URI for `path` with SQLite's `mode` parameter, escaped by `as_uri`."""
    return f"{path.resolve().as_uri()}?mode={mode}"


def _immutable_uri(path: Path) -> str:
    """A read-only URI that also tells SQLite the file cannot change: no lock, no `-wal` read,
    and no `-wal` or `-shm` created beside it. For a file nothing else writes to while it is
    read -- the live database after step 1, and a chosen copy (R3, R11)."""
    return f"{_uri(path, 'ro')}&immutable=1"


def _prepare_directory(directory: Path, now: datetime) -> None:
    """Create the backup directory if it is missing, and remove an earlier crash's leftovers.

    A leftover is a **regular** file -- not a link, not a directory -- whose name
    `TEMPORARY_NAME_PATTERN` matches whole, last modified more than `TEMPORARY_FILE_MAX_AGE`
    before `now`. A younger one may be a copy another process is writing (R6). The age is
    read from `st_mtime_ns` in whole microseconds, so no `float` is involved.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        entries = list(directory.iterdir())
    except OSError as exc:
        message = f"The backup directory {directory} cannot be prepared: {exc}"
        raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
    for entry in entries:
        if TEMPORARY_NAME_PATTERN.match(entry.name) is None:
            continue
        try:
            status = entry.lstat()
            if not stat.S_ISREG(status.st_mode) or not _older_than_max_age(status, now):
                continue
            entry.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except OSError as exc:
            message = f"The leftover temporary file {entry} cannot be removed: {exc}"
            raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc


def _older_than_max_age(status: os.stat_result, now: datetime) -> bool:
    """Whether a file last modified at `status.st_mtime_ns` is past `TEMPORARY_FILE_MAX_AGE`."""
    microseconds = status.st_mtime_ns // _NANOSECONDS_PER_MICROSECOND
    modified_at = _EPOCH + timedelta(microseconds=microseconds)
    return now - modified_at > TEMPORARY_FILE_MAX_AGE


def _copy_into(database: Path, temporary: Path) -> None:
    """Steps 1 to 3: a read-only one-step backup into `temporary`, made one file."""
    try:
        # Created here rather than by SQLite, so that a directory the process cannot write
        # to fails as the `OSError` it is, rather than as SQLite's "unable to open".
        temporary.touch(exist_ok=False)
    except OSError as exc:
        message = f"The copy {temporary} cannot be created: {exc}"
        raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
    try:
        destination = sqlite3.connect(temporary)
    except sqlite3.Error as exc:
        message = f"The copy {temporary} cannot be opened: {_sqlite_text(exc)}"
        raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
    with closing(destination):
        try:
            source = sqlite3.connect(_uri(database, "ro"), uri=True)
        except sqlite3.Error as exc:
            message = f"The database {database} cannot be opened: {_sqlite_text(exc)}"
            raise BackupError(BackupErrorKind.DATABASE_ERROR, message) from exc
        with closing(source):
            try:
                source.backup(destination, pages=-1)
            except sqlite3.Error as exc:
                kind = _kind_of_copy_failure(exc)
                place = temporary if kind is BackupErrorKind.STORAGE_ERROR else database
                message = f"Copying {database} failed at {place}: {_sqlite_text(exc)}"
                raise BackupError(kind, message) from exc
        try:
            mode = destination.execute("PRAGMA journal_mode=DELETE").fetchone()
        except sqlite3.Error as exc:
            message = f"The copy {temporary} cannot be made one file: {_sqlite_text(exc)}"
            raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
        if mode != ("delete",):
            message = f"The copy {temporary} stayed in journal mode {mode!r}, not delete."
            raise BackupError(BackupErrorKind.STORAGE_ERROR, message)


def _kind_of_copy_failure(exc: sqlite3.Error) -> BackupErrorKind:
    """`storage_error` for an error only writing the copy can cause, else `database_error`."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code in _WRITE_SIDE_ERRORS:
        return BackupErrorKind.STORAGE_ERROR
    return BackupErrorKind.DATABASE_ERROR


def _check_copy(temporary: Path, database: Path) -> None:
    """Step 4: the copy passes `integrity_check` and holds exactly one schema revision.

    A copy that cannot be opened or read at all is `storage_error`; see `_cannot_open`.
    """
    try:
        with closing(sqlite3.connect(_uri(temporary, "ro"), uri=True)) as copy:
            _require_integrity(copy)
            _single_revision(copy)
    except sqlite3.Error as exc:
        if _cannot_open(exc):
            message = f"The copy {temporary} cannot be read back: {_sqlite_text(exc)}"
            raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
        message = (
            f"The copy of {database} did not pass its check, so it was not kept: "
            f"{_sqlite_text(exc)}"
        )
        raise BackupError(BackupErrorKind.INTEGRITY_FAILED, message) from exc
    except CheckFailedError as exc:
        message = f"The copy of {database} did not pass its check, so it was not kept: {exc}"
        raise BackupError(BackupErrorKind.INTEGRITY_FAILED, message) from exc


def _cannot_open(exc: sqlite3.Error) -> bool:
    """Whether SQLite could not open or read the file at all: `SQLITE_CANTOPEN`, `SQLITE_IOERR`.

    Every other error -- `SQLITE_NOTADB`, `SQLITE_CORRUPT`, "no such table" for a missing
    `alembic_version` -- means the file was read and what it holds is wrong, which is what
    `integrity_failed` reports. Matched on the primary code, so every `SQLITE_IOERR_*` counts.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and (code & _PRIMARY_CODE_MASK) in _CANNOT_OPEN_ERRORS


def _require_integrity(connection: sqlite3.Connection) -> None:
    """`PRAGMA integrity_check` answered a single `ok` row, or `CheckFailedError`.

    What the pragma reports otherwise is not quoted: its lines name pages, indexes and rowids,
    and a message is not the place to start deciding which of those is safe to show.
    """
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        message = "PRAGMA integrity_check did not answer ok"
        raise CheckFailedError(message)


def _single_revision(connection: sqlite3.Connection) -> str:
    """The one revision in `alembic_version`, or `CheckFailedError` for none or several."""
    rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if len(rows) != 1:
        message = f"alembic_version holds {len(rows)} rows, not one"
        raise CheckFailedError(message)
    return str(rows[0][0])


def _rename_durably(temporary: Path, final: Path) -> int:
    """Step 5: `fsync` the file, rename it into place, `fsync` the directory. Returns its size.

    The size is read from the open descriptor, before the rename, so it is the size of the
    file that was synced rather than of whatever is at that name afterwards.
    """
    try:
        descriptor = os.open(temporary, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(descriptor)
            size = os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)
        temporary.rename(final)
        _fsync_directory(final.parent)
    except OSError as exc:
        message = f"The copy {final} cannot be stored: {exc}"
        raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from exc
    return size


def _fsync_directory(directory: Path) -> None:
    """Force a directory's entries -- the rename into it -- to disk. POSIX only."""
    if not _SYNC_DIRECTORIES:
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_temporary(temporary: Path) -> None:
    """Remove a copy that is not kept, and anything SQLite left beside it. Never raises.

    A file that cannot be removed now is removed by the next attempt's first step, which is
    the reason that step exists; raising here would replace the error that explains the
    failure with one that does not.
    """
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            temporary.with_name(f"{temporary.name}{suffix}").unlink(missing_ok=True)
        except OSError:
            _logger.warning("backup_temporary_file_not_removed", suffix=suffix or "partial")


def _refuse_while_open(database: Path) -> None:
    """Step 1: a `-wal` beside the live database means it is open, or was not closed cleanly."""
    wal = wal_path(database)
    if wal.exists():
        message = (
            f"Refusing to restore: {wal} exists, so the database is open. Stop the "
            "application first. If it is already stopped, it did not close the database "
            "cleanly: start it and stop it once, which lets SQLite recover the file, then "
            "restore again."
        )
        raise RestoreRefusedError(RestoreRefusal.DATABASE_OPEN, message)


def _refuse_leftover_journal(database: Path) -> None:
    """Step 1, R16: no live database file, and a non-empty `-journal` beside its path.

    A journal of zero bytes holds nothing and is no reason to refuse; SQLite does not treat it
    as hot either. A journal beside a live database that exists is left to SQLite, whose
    recovery of it is correct.
    """
    journal = _journal_path(database)
    if database.is_file() or not journal.is_file() or journal.stat().st_size == 0:
        return
    message = (
        f"Refusing to restore: there is no database at {database}, but {journal.name} lies "
        "beside its path. It is a rollback journal that belongs to the database that was "
        "there, and writing the restored file would make SQLite discard it. Nothing was "
        "changed. Move it first: beside the damaged file it belongs to, as "
        f"{database.name}.damaged-<stamp>-journal, or out of the data directory. Then restore "
        "again."
    )
    raise RestoreRefusedError(RestoreRefusal.LEFTOVER_JOURNAL, message)


def _journal_path(database: Path) -> Path:
    """The rollback journal SQLite keeps beside `database` in `delete` mode."""
    return database.with_name(f"{database.name}-journal")


def _protect_live_database(
    database: Path,
    directory: Path,
    clock: Callable[[], datetime],
) -> tuple[str | None, Path | None]:
    """Step 4: the safety copy's name, or where a damaged live database was moved (R3).

    Returns `(safety copy, None)` or `(None, damaged path)`. The module docstring, *A damaged
    live database*, has the rule and the measurements behind it.

    Raises:
        BackupError: with the kind the safety copy failed with, when it was a
            `storage_error`, or when the live database passes its own check;
            `database_error` when the live database cannot be read at all (R12). Nothing
            was written or moved in any of these.
        RestoreRefusedError: see `_move_aside`.
    """
    try:
        return take_copy(database, directory, clock()).name, None
    except BackupError as exc:
        failure = exc
    if failure.error_kind is BackupErrorKind.STORAGE_ERROR:
        message = (
            f"Refusing to restore: no safety copy of {database} could be taken, so nothing "
            f"was changed. {failure}"
        )
        raise BackupError(failure.error_kind, message) from failure
    try:
        _check_live(database)
    except sqlite3.Error as exc:
        if _cannot_open(exc):
            message = (
                f"Refusing to restore: the live database {database} cannot be read "
                f"({_sqlite_text(exc)}), so no safety copy could be taken, and a file that "
                "cannot be read is not judged damaged. Nothing was changed: it is where it "
                "was. Check the data volume, its permissions and the storage device, then "
                "restore again."
            )
            raise BackupError(BackupErrorKind.DATABASE_ERROR, message) from exc
        return None, _move_aside(database, clock())
    except CheckFailedError:
        return None, _move_aside(database, clock())
    message = (
        f"Refusing to restore: no safety copy of {database} could be taken, and the live "
        f"database passes its own check, so it was not moved aside. Nothing was changed. "
        f"{failure}"
    )
    raise BackupError(failure.error_kind, message) from failure


def _check_live(database: Path) -> None:
    """The live file opens, passes `integrity_check`, and holds one `alembic_version` row.

    `mode=ro&immutable=1`, so that the check leaves no `-wal` or `-shm` behind it: see the
    module docstring for why that is both needed and truthful here.

    Raises:
        sqlite3.Error: the file cannot be opened or read as a database, or has no
            `alembic_version` table.
        CheckFailedError: the check ran and did not answer `ok`, or the revision is not one.
    """
    with closing(sqlite3.connect(_immutable_uri(database), uri=True)) as live:
        _require_integrity(live)
        _single_revision(live)


def _move_aside(database: Path, moved_at: datetime) -> Path:
    """Rename a damaged live database to `damaged_path`, fsync, and clear what was beside it.

    A `-journal` goes with it, renamed after it, because a rollback journal belongs to the
    file it was written for. The `-shm`, and a `-wal` that is empty, are removed after the
    move: both can only be left by this restore's own read-only connections, which never write
    a frame, and neither may be paired with the database step 5 creates at the old name.

    Raises:
        RestoreRefusedError: `database_open`, when a `-wal` with content is beside the live
            database: something opened it read-write since step 1. Nothing was moved.
        BackupError: `database_error`, when the target already exists (nothing was moved),
            when the move failed (the file is where it was), when only the directory's fsync
            after it failed (the file is at the target), or when the clean-up after it failed.
            Every message says where the live database is.
    """
    wal = wal_path(database)
    if wal.exists() and wal.stat().st_size > 0:
        message = (
            f"Refusing to restore: {wal} appeared during the restore, so the database was "
            "opened. Nothing was changed. Stop the application, then restore again."
        )
        raise RestoreRefusedError(RestoreRefusal.DATABASE_OPEN, message)
    target = damaged_path(database, moved_at)
    journal = _journal_path(database)
    moves = [(database, target)]
    if journal.exists():
        moves.append((journal, target.with_name(f"{target.name}-journal")))
    for _, destination in moves:
        if destination.exists():
            message = (
                f"Refusing to restore: the live database {database} did not pass its own "
                f"check, and {destination}, where it would be moved aside, already exists. "
                "Nothing was changed: the live database is where it was."
            )
            raise BackupError(BackupErrorKind.DATABASE_ERROR, message)
    try:
        database.rename(target)
    except OSError as exc:
        message = (
            f"The live database {database} did not pass its own check, and moving it aside to "
            f"{target} failed: {exc}. Nothing was restored, and it is where it was."
        )
        raise BackupError(BackupErrorKind.DATABASE_ERROR, message) from exc
    for source, destination in moves[1:]:
        try:
            source.rename(destination)
        except OSError as exc:
            message = (
                f"The damaged live database was moved aside to {target}, but moving {source} "
                f"to {destination} failed: {exc}. Nothing was restored. Move it there by hand "
                "before restoring again: it belongs to the damaged file."
            )
            raise BackupError(BackupErrorKind.DATABASE_ERROR, message) from exc
    try:
        _fsync_directory(database.parent)
    except OSError as exc:
        message = (
            f"The damaged live database was moved aside to {target}, but the move could not "
            f"be made durable: syncing {database.parent} failed: {exc}. Nothing was restored."
        )
        raise BackupError(BackupErrorKind.DATABASE_ERROR, message) from exc
    for suffix in ("-shm", "-wal"):
        leftover = database.with_name(f"{database.name}{suffix}")
        try:
            leftover.unlink(missing_ok=True)
        except OSError as exc:
            message = (
                f"The damaged live database was moved aside to {target}, but {leftover} "
                f"cannot be removed: {exc}. Nothing was restored."
            )
            raise BackupError(BackupErrorKind.DATABASE_ERROR, message) from exc
    return target


def _chosen_copy(directory: Path, name: str) -> Path:
    """Step 2: the path of the copy `name`, or `RestoreRefusedError`."""
    if instant_of(name) is None:
        message = (
            f"Refusing to restore: {name!r} is not the name of a backup. A backup is named "
            "portfolio-YYYYMMDDTHHMMSSffffffZ.sqlite3, and list-backups shows them."
        )
        raise RestoreRefusedError(RestoreRefusal.UNKNOWN_BACKUP, message)
    chosen = directory / name
    if not chosen.is_file():
        message = (
            f"Refusing to restore: there is no backup named {name} in {directory}. "
            "list-backups shows the ones there are."
        )
        raise RestoreRefusedError(RestoreRefusal.UNKNOWN_BACKUP, message)
    return chosen


def _refuse_unless_self_contained(chosen: Path) -> None:
    """Refuse a copy with a `-wal` beside it that holds frames (R13). Nothing is written."""
    wal = wal_path(chosen)
    if wal.exists() and wal.stat().st_size > 0:
        message = (
            f"Refusing to restore: {chosen.name} is not a self-contained copy: {wal.name} "
            "beside it holds transactions the file itself does not, and the restore reads "
            "the file alone. Nothing was changed. Copy both files out of the backup "
            "directory, open the copy there once with sqlite3, which writes those "
            "transactions into it, and copy it back alone."
        )
        raise RestoreRefusedError(RestoreRefusal.NOT_SELF_CONTAINED, message)


def _check_backup(chosen: Path) -> dict[str, int]:
    """Step 3: check the copy and return its rows per table. Nothing is written.

    Opened immutable, so that nothing is left beside the copy (R11).
    """
    revisions, head = known_schema_revisions()
    try:
        with closing(sqlite3.connect(_immutable_uri(chosen), uri=True)) as copy:
            _require_integrity(copy)
            revision = _single_revision(copy)
            counts = _row_counts(copy)
    except (sqlite3.Error, CheckFailedError) as exc:
        message = (
            f"Refusing to restore: the backup {chosen.name} did not pass its check ({exc}). "
            "Nothing was changed. Choose another backup."
        )
        raise BackupError(BackupErrorKind.INTEGRITY_FAILED, message) from exc
    if revision not in revisions:
        message = (
            f"Refusing to restore: {chosen.name} is at schema revision {revision}, which this "
            f"version does not know; the newest it knows is {head}. The copy was taken by a "
            "newer version of the application, which this one cannot migrate. Restore it "
            "with that version or a newer one."
        )
        raise RestoreRefusedError(RestoreRefusal.NEWER_SCHEMA, message)
    return counts


def _copy_over(chosen: Path, database: Path, before: str) -> None:
    """Step 5: the backup API, the copy as the source and the live file as the destination.

    The source is opened immutable, so that nothing is left beside the copy (R11). The
    destination connection is read-write and closes last, so SQLite checkpoints the restored
    pages out of the `-wal` and deletes it. If the step fails, SQLite rolls the destination's
    write transaction back. `before` is `_before_sentence`'s, for the message.
    """
    try:
        with (
            closing(sqlite3.connect(_immutable_uri(chosen), uri=True)) as source,
            closing(sqlite3.connect(database)) as destination,
        ):
            source.backup(destination, pages=-1)
    except sqlite3.Error as exc:
        message = f"Writing the backup into {database} failed: {_sqlite_text(exc)}. {before}"
        raise BackupError(BackupErrorKind.DATABASE_ERROR, message) from exc


def _check_restored(database: Path, before: str) -> dict[str, int]:
    """Step 6: `integrity_check` and the rows per table, over a read-write connection.

    Read-write and never creating, so that this connection -- the last to close -- removes
    the `-wal` and `-shm` its own read opened, which a read-only one would leave behind.
    """
    try:
        with closing(sqlite3.connect(_uri(database, "rw"), uri=True)) as live:
            _require_integrity(live)
            return _row_counts(live)
    except CheckFailedError as exc:
        message = f"After the restore, {database} did not pass its check ({exc}). {before}"
        raise BackupError(BackupErrorKind.INTEGRITY_FAILED, message) from exc
    except sqlite3.Error as exc:
        message = f"After the restore, {database} cannot be read: {_sqlite_text(exc)}. {before}"
        raise BackupError(BackupErrorKind.DATABASE_ERROR, message) from exc


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """`SELECT COUNT(*)` of every table but SQLite's own, by table name in order.

    The table names come from `sqlite_master`, and each is quoted as an identifier, doubling
    any quote inside it, before it is put in the statement: a name cannot be bound as a
    parameter.
    """
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND substr(name, 1, 7) != 'sqlite_' ORDER BY name"
        )
    ]
    counts: dict[str, int] = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        row = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()  # noqa: S608
        counts[name] = int(row[0])
    return counts


def _before_sentence(safety: str | None, damaged: Path | None) -> str:
    """What an operator needs after a failure past step 4: where the database as it was is."""
    if safety is not None:
        return f"The safety copy {safety} holds the database as it was before the restore."
    if damaged is not None:
        return (
            f"The live database did not pass its own check and was moved aside to {damaged}, "
            "which holds it as it was; there is no safety copy."
        )
    return "There was no database before the restore, so there is no safety copy."


def _sqlite_text(exc: sqlite3.Error) -> str:
    """SQLite's own message and, when it has one, its error name. Never a row's values."""
    name = getattr(exc, "sqlite_errorname", None)
    return f"{exc} ({name})" if name else str(exc)
