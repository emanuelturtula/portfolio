"""Spec 029 (#22): taking a copy of the live database, and the files around it.

Criterion 1 is the first block: a copy taken while another connection commits, that is one
consistent snapshot, passes its check, and needs nothing beside it to be read. Criterion 2's
"a copy that fails the check is not kept" is the second. Ruling R1 is the third: a copy taken
with nothing else open leaves no `-wal` beside the live database, so a restore right after it
is not refused.

The rest pins each failure to its `error_kind`, and that a failure leaves nothing behind.
Where SQLite or the operating system cannot be made to fail on cue -- a full disk, a
directory that cannot be opened -- the call is replaced at `sqlite3.connect`, `Path` or `os`
for the one path under test, and everything else is real.
"""

from __future__ import annotations

import copy
import os
import shutil
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from structlog.testing import capture_logs

from portfolio.db import backup
from portfolio.db.backup import (
    TEMPORARY_FILE_MAX_AGE,
    TEMPORARY_NAME_PATTERN,
    TEMPORARY_PREFIX,
    BackupError,
    BackupFile,
    CheckFailedError,
    RestoreRefusal,
    RestoreRefusedError,
    database_path,
    known_schema_revisions,
    list_copies,
    release_wal,
    remove_copies,
    restore_copy,
    take_copy,
    wal_path,
)
from portfolio.domain.backups import BackupErrorKind, backup_name
from tests.backup_harness import (
    T0,
    add_notes,
    copy_names,
    corrupt_with_orphan_pages,
    execute,
    header_versions,
    integrity,
    plant_copies,
    read_only,
    reader,
    sidecars_of,
    table_contents,
)

if TYPE_CHECKING:
    from collections.abc import Callable

NAME: Final = backup_name(T0)
#: The temporary file a copy started at `T0` is written to, spelled out rather than built.
TEMPORARY: Final = ".portfolio-20261002T030000123456Z.partial"
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def take(database: Path, directory: Path, started_at: datetime = T0) -> BackupFile:
    return take_copy(database, directory, started_at)


# --------------------------------------------------------------------------------------
# A stand-in for `sqlite3.connect`, for the failures SQLite cannot be made to produce
# --------------------------------------------------------------------------------------


class Connection:
    """Wraps a real connection. `backup` and `execute` can be replaced, the rest passes on."""

    def __init__(
        self,
        real: sqlite3.Connection,
        *,
        backup: Callable[..., None] | None = None,
        execute: Callable[..., Any] | None = None,
    ) -> None:
        self.real = real
        self._backup = backup
        self._execute = execute

    def backup(self, target: Any, **keywords: Any) -> None:
        if self._backup is not None:
            self._backup(self.real, target, **keywords)
            return
        self.real.backup(target.real if isinstance(target, Connection) else target, **keywords)

    def execute(self, *arguments: Any) -> Any:
        if self._execute is not None:
            return self._execute(self.real, *arguments)
        return self.real.execute(*arguments)

    def close(self) -> None:
        self.real.close()


def replace_connect(
    monkeypatch: pytest.MonkeyPatch,
    choose: Callable[[str, sqlite3.Connection], Any],
) -> None:
    """Route every `sqlite3.connect` through `choose(target, real)` for this test.

    Whatever `choose` hands back as the real connection is wrapped anyway, so that a
    replaced destination can still be the target of a real source's `backup`.
    """
    real_connect = sqlite3.connect

    def connect(target: Any, *arguments: Any, **keywords: Any) -> Any:
        chosen = choose(str(target), real_connect(target, *arguments, **keywords))
        return chosen if isinstance(chosen, Connection) else Connection(chosen)

    monkeypatch.setattr(sqlite3, "connect", connect)


def sqlite_error(code: int, name: str, message: str) -> sqlite3.OperationalError:
    """An `OperationalError` carrying SQLite's own code, as the C module raises one."""
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    error.sqlite_errorname = name
    return error


# --------------------------------------------------------------------------------------
# Criterion 1: a copy taken while another connection writes
# --------------------------------------------------------------------------------------


class Writer(threading.Thread):
    """Commits a pair of rows, one in each of two tables, in one transaction, until stopped.

    Each pair carries its own number, so a copy can be checked against the exact sequence
    of commits: a consistent snapshot holds pairs 1 to n in both tables and nothing else.
    """

    def __init__(self, database: Path) -> None:
        super().__init__(daemon=True)
        self.database = database
        self.committed = 0
        self.failure: Exception | None = None
        self.stop = threading.Event()
        self.started_writing = threading.Event()

    def run(self) -> None:
        try:
            with closing(sqlite3.connect(self.database, timeout=10)) as connection:
                while not self.stop.is_set():
                    number = self.committed + 1
                    with connection:
                        connection.execute(
                            "INSERT INTO ledger_a (id, body) VALUES (?, ?)", (number, f"a-{number}")
                        )
                        connection.execute(
                            "INSERT INTO ledger_b (id, body) VALUES (?, ?)", (number, f"b-{number}")
                        )
                    self.committed = number
                    if number >= 20:
                        self.started_writing.set()
        except Exception as exc:  # reported by the test, which reads `failure`
            self.failure = exc
            self.started_writing.set()


@pytest.fixture
def busy_database(live_database: Path) -> Path:
    """The live database with two ledgers and a few megabytes of padding to copy."""
    execute(
        live_database,
        "CREATE TABLE ledger_a (id INTEGER PRIMARY KEY, body TEXT NOT NULL)",
        "CREATE TABLE ledger_b (id INTEGER PRIMARY KEY, body TEXT NOT NULL)",
        "CREATE TABLE padding (body TEXT NOT NULL)",
    )
    with closing(sqlite3.connect(live_database)) as connection:
        connection.executemany(
            "INSERT INTO padding (body) VALUES (?)",
            [(f"{index:08d}" * 40,) for index in range(12_000)],
        )
        connection.commit()
    return live_database


def test_a_copy_taken_while_another_connection_commits_is_one_consistent_snapshot(
    busy_database: Path, backup_directory: Path
) -> None:
    """Criterion 1, end to end on real files.

    A writer commits pairs throughout. The copy must hold pairs 1 to n in both ledgers --
    never a half-committed pair, never a gap -- with n between what was committed before the
    copy started and after it finished, and the writer must have committed during the copy,
    or the test proved nothing about concurrency.
    """
    assert header_versions(busy_database) == (2, 2), "the live database is not in WAL mode"
    writer = Writer(busy_database)
    writer.start()
    assert writer.started_writing.wait(timeout=10)
    try:
        before = writer.committed
        taken = take(busy_database, backup_directory)
        after = writer.committed
    finally:
        writer.stop.set()
        writer.join(timeout=10)
    assert writer.failure is None, f"the writer failed: {writer.failure!r}"

    copied = backup_directory / taken.name
    contents = table_contents(copied)
    count = len(contents["ledger_a"])
    assert before <= count <= after
    assert after > before, "no commit landed while the copy was taken"
    with closing(read_only(copied)) as connection:
        ledger_a = list(connection.execute("SELECT id, body FROM ledger_a ORDER BY id"))
        ledger_b = list(connection.execute("SELECT id, body FROM ledger_b ORDER BY id"))
    assert ledger_a == [(number, f"a-{number}") for number in range(1, count + 1)]
    assert ledger_b == [(number, f"b-{number}") for number in range(1, count + 1)]
    assert integrity(copied) == [("ok",)]


def test_a_copy_is_one_self_contained_file(live_database: Path, backup_directory: Path) -> None:
    """Header 1/1, nothing beside it, and readable on its own somewhere else entirely."""
    add_notes(live_database, "first", "second")
    assert header_versions(live_database) == (2, 2)

    taken = take(live_database, backup_directory)

    copied = backup_directory / taken.name
    assert header_versions(copied) == (1, 1)
    assert copy_names(backup_directory) == {taken.name}
    elsewhere = backup_directory.parent / "elsewhere" / "alone.sqlite3"
    elsewhere.parent.mkdir()
    shutil.copyfile(copied, elsewhere)
    assert integrity(elsewhere) == [("ok",)]
    assert table_contents(elsewhere) == table_contents(live_database)
    assert sidecars_of(elsewhere) == []


def test_a_copy_holds_every_row_of_the_live_database(
    live_database: Path, backup_directory: Path
) -> None:
    add_notes(live_database, "a note", "another")

    taken = take(live_database, backup_directory)

    assert table_contents(backup_directory / taken.name) == table_contents(live_database)


def test_the_copy_is_named_after_its_start_and_reports_its_own_size(
    live_database: Path, backup_directory: Path
) -> None:
    started_at = T0.astimezone(timezone(timedelta(hours=-3)))

    taken = take(live_database, backup_directory, started_at)

    assert taken == BackupFile(
        name=NAME,
        started_at=started_at,
        size_bytes=(backup_directory / NAME).stat().st_size,
    )
    assert taken.size_bytes > 0


def test_a_missing_backup_directory_is_created(live_database: Path, tmp_path: Path) -> None:
    directory = tmp_path / "deep" / "er" / "backups"

    take(live_database, directory)

    assert copy_names(directory) == {NAME}


def test_the_live_database_is_never_written(live_database: Path, backup_directory: Path) -> None:
    """Read-only towards the owner's data: the file's bytes are the same before and after."""
    before = live_database.read_bytes()

    take(live_database, backup_directory)

    assert live_database.read_bytes() == before


# --------------------------------------------------------------------------------------
# Criterion 2: a copy that fails its check is not kept
# --------------------------------------------------------------------------------------


def assert_nothing_kept(directory: Path, before: set[str] | None = None) -> None:
    assert copy_names(directory) == (before or set())


def test_a_copy_that_fails_integrity_check_is_not_kept(
    live_database: Path, backup_directory: Path
) -> None:
    corrupt_with_orphan_pages(live_database)
    assert integrity(live_database) != [("ok",)]

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert isinstance(caught.value.__cause__, CheckFailedError)
    assert "did not pass its check, so it was not kept" in str(caught.value)
    # The pragma's own lines name pages; the message names the check and nothing it found.
    assert "never used" not in str(caught.value)
    assert_nothing_kept(backup_directory)


@pytest.mark.parametrize(
    ("statements", "rows"),
    [
        (["DELETE FROM alembic_version"], 0),
        (["INSERT INTO alembic_version (version_num) VALUES ('0000_not_a_revision')"], 2),
    ],
    ids=["no revision", "two revisions"],
)
def test_a_copy_without_exactly_one_schema_revision_is_not_kept(
    live_database: Path, backup_directory: Path, statements: list[str], rows: int
) -> None:
    execute(live_database, *statements)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert f"alembic_version holds {rows} rows, not one" in str(caught.value)
    assert_nothing_kept(backup_directory)


def test_a_database_with_no_schema_revision_table_fails_the_check(
    tmp_path: Path, backup_directory: Path
) -> None:
    """A SQLite file that is not this application's: the check's query fails, and it is not kept."""
    stranger = tmp_path / "stranger.db"
    add_notes(stranger, "not ours")

    with pytest.raises(BackupError) as caught:
        take(stranger, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert_nothing_kept(backup_directory)


# --------------------------------------------------------------------------------------
# Ruling R1: no `-wal` is left beside the live database
# --------------------------------------------------------------------------------------


def test_the_premise_a_read_only_connection_closing_last_leaves_the_wal_behind(
    live_database: Path,
) -> None:
    """Why `release_wal` exists, measured rather than assumed (spec 029, R1).

    If this stops holding on some SQLite, R1's clean-up is merely redundant, and the test
    below it no longer proves the clean-up runs. Either way, this is where to look.
    """
    with closing(read_only(live_database)) as connection:
        connection.execute("PRAGMA schema_version").fetchone()

    assert wal_path(live_database).exists()


def test_a_copy_with_nothing_else_open_leaves_no_wal_and_a_restore_is_not_refused(
    live_database: Path, backup_directory: Path
) -> None:
    """R1: `backup` with the application stopped, then `restore-backup` straight after."""
    add_notes(live_database, "before")
    assert sidecars_of(live_database) == []

    taken = take(live_database, backup_directory)

    assert sidecars_of(live_database) == []
    restored = restore_copy(
        live_database, backup_directory, taken.name, clock=lambda: T0 + timedelta(seconds=1)
    )
    assert restored.restored == taken.name


def test_a_copy_beside_a_running_application_leaves_its_wal_alone(
    live_database: Path, backup_directory: Path
) -> None:
    """While another connection holds the database, the clean-up is a no-op."""
    with closing(sqlite3.connect(live_database)) as application:
        application.execute("PRAGMA schema_version").fetchone()
        assert wal_path(live_database).exists()

        take(live_database, backup_directory)

        assert wal_path(live_database).exists()
        add = "CREATE TABLE written_after (id INTEGER PRIMARY KEY)"
        application.execute(add)
        application.commit()
    assert "written_after" in table_contents(live_database)


def test_the_clean_up_runs_even_when_the_copy_fails(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-only connection read the live file, then the write of the copy failed."""

    def read_then_fill_the_disk(real: sqlite3.Connection, target: Any, **keywords: Any) -> None:
        del target, keywords
        real.execute("PRAGMA schema_version").fetchone()
        raise sqlite_error(sqlite3.SQLITE_FULL, "SQLITE_FULL", "database or disk is full")

    def choose(target: str, real: sqlite3.Connection) -> Any:
        if target.endswith("mode=ro") and "portfolio.db" in target:
            return Connection(real, backup=read_then_fill_the_disk)
        return real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert sidecars_of(live_database) == []
    assert_nothing_kept(backup_directory)


def test_release_wal_never_raises_and_logs_only_the_class_name(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "portfolio.db"

    with capture_logs() as captured:
        release_wal(missing)

    assert captured == [
        {
            "event": "backup_wal_release_failed",
            "error_type": "OperationalError",
            "log_level": "warning",
        }
    ]
    assert not missing.exists(), "the clean-up created a database"


def test_release_wal_removes_both_files_a_read_only_connection_left(live_database: Path) -> None:
    with closing(read_only(live_database)) as connection:
        connection.execute("PRAGMA schema_version").fetchone()
    assert sidecars_of(live_database) == [f"{live_database.name}-wal", f"{live_database.name}-shm"]

    with capture_logs() as captured:
        release_wal(live_database)

    assert sidecars_of(live_database) == []
    assert captured == []


# --------------------------------------------------------------------------------------
# Temporary files: an earlier crash's are removed, nobody else's are
# --------------------------------------------------------------------------------------


#: An earlier attempt's temporary file, by a stamp that is not this test's.
EARLIER: Final = f"{TEMPORARY_PREFIX}20260101T000000000000Z.partial"


def modified_at(path: Path, instant: datetime) -> None:
    """Set `path`'s modification time to `instant`, to the microsecond, without a float."""
    nanoseconds = (instant - EPOCH) // timedelta(microseconds=1) * 1_000
    os.utime(path, ns=(nanoseconds, nanoseconds))


def leave(directory: Path, name: str, at: datetime, content: bytes = b"leftover") -> Path:
    path = directory / name
    path.write_bytes(content)
    modified_at(path, at)
    return path


def test_an_earlier_crashs_temporary_files_are_removed_when_older_than_an_hour(
    live_database: Path, backup_directory: Path
) -> None:
    """R6: the temporary file and the three SQLite keeps beside it, an hour and 1 us old."""
    old = T0 - TEMPORARY_FILE_MAX_AGE - timedelta(microseconds=1)
    backup_directory.mkdir()
    for suffix in ("", "-wal", "-shm", "-journal"):
        leave(backup_directory, f"{EARLIER}{suffix}", old)

    take(live_database, backup_directory)

    assert copy_names(backup_directory) == {NAME}


def test_the_age_limit_is_an_hour() -> None:
    assert timedelta(hours=1) == TEMPORARY_FILE_MAX_AGE


@pytest.mark.parametrize(
    "age",
    [
        TEMPORARY_FILE_MAX_AGE,
        TEMPORARY_FILE_MAX_AGE - timedelta(minutes=1),
        timedelta(0),
        -timedelta(hours=3),
    ],
    ids=["exactly an hour", "59 minutes", "written now", "in the future"],
)
def test_a_temporary_file_an_hour_old_or_younger_is_left_for_the_process_writing_it(
    live_database: Path, backup_directory: Path, age: timedelta
) -> None:
    """R6: another process may be writing it, and deleting it would fail that copy.

    "In the future" is a file whose clock ran ahead of this one: younger than any age.
    """
    backup_directory.mkdir()
    in_flight = [
        leave(backup_directory, f"{EARLIER}{suffix}", T0 - age, b"another copy in flight")
        for suffix in ("", "-journal")
    ]

    take(live_database, backup_directory)

    assert copy_names(backup_directory) == {NAME, *(path.name for path in in_flight)}
    assert all(path.read_bytes() == b"another copy in flight" for path in in_flight)


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "portfolio-20260101T000000000000Z.partial",
        f"x{EARLIER}",
        f"{EARLIER}.bak",
        f"{EARLIER}-wal2",
        f"{EARLIER}-journal.old",
        f"{TEMPORARY_PREFIX}20260101T000000000000Z.keep",
        f"{TEMPORARY_PREFIX}20260101T00000000000Z.partial",
        f"{TEMPORARY_PREFIX}20260101T0000000000000Z.partial",
        f"{TEMPORARY_PREFIX}2026010aT000000000000Z.partial",
        f"{TEMPORARY_PREFIX}20260101T000000000000.partial",
        f"{TEMPORARY_PREFIX.upper()}20260101T000000000000Z.partial",
    ],
)
def test_an_old_file_whose_name_is_not_exactly_a_temporary_files_is_never_deleted(
    live_database: Path, backup_directory: Path, name: str
) -> None:
    """R6: the whole name must match the stamp `utc_stamp` writes, to the digit."""
    backup_directory.mkdir()
    leave(backup_directory, name, T0 - timedelta(days=30))
    assert TEMPORARY_NAME_PATTERN.match(name) is None

    take(live_database, backup_directory)

    assert copy_names(backup_directory) == {NAME, name}


def test_an_old_directory_named_like_a_temporary_file_is_left_alone(
    live_database: Path, backup_directory: Path
) -> None:
    """R6: regular files only. Unlinking a directory would fail, and fail the copy with it."""
    backup_directory.mkdir()
    named_like_one = backup_directory / EARLIER
    named_like_one.mkdir()
    (named_like_one / "inside").write_bytes(b"the operator's")
    modified_at(named_like_one, T0 - timedelta(days=30))

    take(live_database, backup_directory)

    assert named_like_one.is_dir()
    assert (named_like_one / "inside").read_bytes() == b"the operator's"
    assert copy_names(backup_directory) == {NAME, EARLIER}


def test_the_pattern_matches_what_take_copy_names_its_temporary_file() -> None:
    """The clean-up and the writer agree on the name, or the clean-up never runs."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert TEMPORARY_NAME_PATTERN.match(f"{TEMPORARY}{suffix}") is not None
    assert TEMPORARY_NAME_PATTERN.match(f"{TEMPORARY}\n") is None


def test_a_temporary_file_that_vanishes_before_it_is_read_is_skipped(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process removed it between the listing and the look: not an error."""
    backup_directory.mkdir()
    leave(backup_directory, EARLIER, T0 - timedelta(days=1))
    real_lstat = Path.lstat

    def gone(self: Path) -> os.stat_result:
        if self.name == EARLIER:
            self.unlink()
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", gone)

    taken = take(live_database, backup_directory)

    assert taken.name == NAME
    assert copy_names(backup_directory) == {NAME}


def test_a_leftover_that_cannot_be_removed_is_a_storage_error_before_anything_is_copied(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_directory.mkdir()
    leftover = leave(backup_directory, EARLIER, T0 - timedelta(days=1))
    real_unlink = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self.name == EARLIER:
            raise PermissionError(13, "Permission denied", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert str(caught.value).startswith(
        f"The leftover temporary file {leftover} cannot be removed: "
    )
    assert isinstance(caught.value.__cause__, PermissionError)
    assert copy_names(backup_directory) == {EARLIER}
    assert sidecars_of(live_database) == []


def test_a_temporary_file_that_cannot_be_removed_is_logged_and_the_real_error_kept(
    tmp_path: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a database at all, and is long enough to read" * 100)
    real_unlink = Path.unlink

    def refuse_the_temporary(self: Path, missing_ok: bool = False) -> None:
        if self.name == TEMPORARY:
            raise PermissionError(13, "in use", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_the_temporary)

    with capture_logs() as captured, pytest.raises(BackupError) as caught:
        take(garbage, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert {
        "event": "backup_temporary_file_not_removed",
        "suffix": "partial",
        "log_level": "warning",
    } in captured


def test_a_copy_that_is_not_kept_takes_what_sqlite_left_beside_it_too(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed attempt removes the temporary file and its `-wal`, `-shm` and `-journal`.

    SQLite leaves those beside a file it had open when a write fails part of the way. They
    are made here by the failing check itself, the last step before the clean-up, so that
    what is asserted is the clean-up and nothing SQLite decides on its own.
    """

    def leave_sidecars_and_fail(temporary: Path, database: Path) -> None:
        del database
        for suffix in ("-wal", "-shm", "-journal"):
            temporary.with_name(f"{temporary.name}{suffix}").write_bytes(b"left by SQLite")
        raise BackupError(BackupErrorKind.INTEGRITY_FAILED, "the check failed")

    monkeypatch.setattr(backup, "_check_copy", leave_sidecars_and_fail)

    with pytest.raises(BackupError, match="the check failed"):
        take(live_database, backup_directory)

    assert sorted(path.name for path in backup_directory.iterdir()) == []


# --------------------------------------------------------------------------------------
# Every other failure, by kind, and that each leaves nothing behind
# --------------------------------------------------------------------------------------


def test_a_missing_database_is_a_database_error(tmp_path: Path, backup_directory: Path) -> None:
    with pytest.raises(BackupError) as caught:
        take(tmp_path / "data" / "portfolio.db", backup_directory)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert "There is no database at" in str(caught.value)
    assert caught.value.__cause__ is None
    assert_nothing_kept(backup_directory)


def test_a_file_that_is_not_a_database_is_a_database_error(
    tmp_path: Path, backup_directory: Path
) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a database at all, and is long enough to read" * 100)

    with pytest.raises(BackupError) as caught:
        take(garbage, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert f"failed at {garbage}" in str(caught.value)
    assert "SQLITE_NOTADB" in str(caught.value)
    assert_nothing_kept(backup_directory)


def test_a_backup_directory_that_is_a_file_is_a_storage_error(
    live_database: Path, tmp_path: Path
) -> None:
    in_the_way = tmp_path / "backups"
    in_the_way.write_bytes(b"a file where the directory should be")

    with pytest.raises(BackupError) as caught:
        take(live_database, in_the_way)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert "cannot be prepared" in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)


def test_a_temporary_file_that_cannot_be_created_is_a_storage_error(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_touch = Path.touch

    def refuse(self: Path, mode: int = 0o666, exist_ok: bool = True) -> None:
        if self.name == TEMPORARY:
            raise PermissionError(13, "Permission denied", str(self))
        real_touch(self, mode=mode, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "touch", refuse)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert "cannot be created" in str(caught.value)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert_nothing_kept(backup_directory)


def test_a_temporary_file_that_already_exists_is_not_overwritten_silently(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`touch(exist_ok=False)`: a second writer of the same name fails rather than sharing it."""
    real_prepare = backup._prepare_directory

    def prepare_then_race(directory: Path, now: datetime) -> None:
        real_prepare(directory, now)
        (directory / TEMPORARY).write_bytes(b"another process got here first")

    monkeypatch.setattr(backup, "_prepare_directory", prepare_then_race)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert isinstance(caught.value.__cause__, FileExistsError)


def test_a_temporary_file_sqlite_cannot_open_is_a_storage_error(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def choose(target: str, real: sqlite3.Connection) -> Any:
        if target.endswith(".partial"):
            real.close()
            raise sqlite_error(sqlite3.SQLITE_CANTOPEN, "SQLITE_CANTOPEN", "unable to open")
        return real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert "cannot be opened: unable to open (SQLITE_CANTOPEN)" in str(caught.value)
    assert_nothing_kept(backup_directory)


def test_a_live_database_sqlite_cannot_open_is_a_database_error(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def choose(target: str, real: sqlite3.Connection) -> Any:
        if target.endswith("mode=ro") and "portfolio.db" in target:
            real.close()
            raise sqlite3.OperationalError("unable to open database file")
        return real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert f"The database {live_database} cannot be opened" in str(caught.value)
    assert_nothing_kept(backup_directory)


@pytest.mark.parametrize(
    ("code", "name", "kind"),
    [
        (sqlite3.SQLITE_FULL, "SQLITE_FULL", BackupErrorKind.STORAGE_ERROR),
        (sqlite3.SQLITE_IOERR_WRITE, "SQLITE_IOERR_WRITE", BackupErrorKind.STORAGE_ERROR),
        (sqlite3.SQLITE_IOERR_FSYNC, "SQLITE_IOERR_FSYNC", BackupErrorKind.STORAGE_ERROR),
        (sqlite3.SQLITE_IOERR_READ, "SQLITE_IOERR_READ", BackupErrorKind.DATABASE_ERROR),
        (sqlite3.SQLITE_BUSY, "SQLITE_BUSY", BackupErrorKind.DATABASE_ERROR),
        (sqlite3.SQLITE_CORRUPT, "SQLITE_CORRUPT", BackupErrorKind.DATABASE_ERROR),
    ],
)
def test_a_failed_backup_step_is_storage_only_when_writing_the_copy_caused_it(
    live_database: Path,
    backup_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    name: str,
    kind: BackupErrorKind,
) -> None:
    def fail(real: sqlite3.Connection, target: Any, **keywords: Any) -> None:
        del real, target, keywords
        raise sqlite_error(code, name, "the step failed")

    def choose(target: str, real: sqlite3.Connection) -> Any:
        if target.endswith("mode=ro") and "portfolio.db" in target:
            return Connection(real, backup=fail)
        return real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is kind
    place = backup_directory / TEMPORARY if kind is BackupErrorKind.STORAGE_ERROR else live_database
    assert f"failed at {place}: the step failed ({name})" in str(caught.value)
    assert_nothing_kept(backup_directory)


def test_an_error_with_no_sqlite_code_is_the_databases(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(real: sqlite3.Connection, target: Any, **keywords: Any) -> None:
        del real, target, keywords
        raise sqlite3.OperationalError("no code at all")

    def choose(target: str, real: sqlite3.Connection) -> Any:
        if target.endswith("mode=ro") and "portfolio.db" in target:
            return Connection(real, backup=fail)
        return real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value).endswith("failed at " + str(live_database) + ": no code at all")


def test_a_copy_removed_by_another_process_before_its_check_is_a_storage_error(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6, the reviewer's race: another process's clean-up deleted the copy in flight.

    Before R6 this was `integrity_failed`, which sends the operator after the live database.
    The real `sqlite3` answers the missing file; nothing is faked but the deletion.
    """
    real_copy_into = backup._copy_into

    def copy_then_lose_it(database: Path, temporary: Path) -> None:
        real_copy_into(database, temporary)
        temporary.unlink()

    monkeypatch.setattr(backup, "_copy_into", copy_then_lose_it)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert str(caught.value).startswith(
        f"The copy {backup_directory / TEMPORARY} cannot be read back: "
    )
    assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert caught.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN
    assert_nothing_kept(backup_directory)


@pytest.mark.parametrize(
    ("code", "name", "kind"),
    [
        (sqlite3.SQLITE_CANTOPEN, "SQLITE_CANTOPEN", BackupErrorKind.STORAGE_ERROR),
        (sqlite3.SQLITE_IOERR, "SQLITE_IOERR", BackupErrorKind.STORAGE_ERROR),
        (sqlite3.SQLITE_IOERR_READ, "SQLITE_IOERR_READ", BackupErrorKind.STORAGE_ERROR),
        (sqlite3.SQLITE_IOERR_SHORT_READ, "SQLITE_IOERR_SHORT_READ", BackupErrorKind.STORAGE_ERROR),
        (sqlite3.SQLITE_CANTOPEN_ISDIR, "SQLITE_CANTOPEN_ISDIR", BackupErrorKind.STORAGE_ERROR),
        (sqlite3.SQLITE_NOTADB, "SQLITE_NOTADB", BackupErrorKind.INTEGRITY_FAILED),
        (sqlite3.SQLITE_CORRUPT, "SQLITE_CORRUPT", BackupErrorKind.INTEGRITY_FAILED),
        (sqlite3.SQLITE_ERROR, "SQLITE_ERROR", BackupErrorKind.INTEGRITY_FAILED),
        (sqlite3.SQLITE_PERM, "SQLITE_PERM", BackupErrorKind.INTEGRITY_FAILED),
    ],
)
def test_a_copy_that_cannot_be_opened_is_storage_and_one_read_wrongly_is_integrity(
    live_database: Path,
    backup_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    name: str,
    kind: BackupErrorKind,
) -> None:
    """R6: the primary code decides, so every `SQLITE_IOERR_*` and `SQLITE_CANTOPEN_*` counts."""

    def choose(target: str, real: sqlite3.Connection) -> Any:
        if target.endswith(".partial?mode=ro"):
            real.close()
            raise sqlite_error(code, name, "the check could not run")
        return real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is kind
    text = f"the check could not run ({name})"
    if kind is BackupErrorKind.STORAGE_ERROR:
        assert str(caught.value) == (
            f"The copy {backup_directory / TEMPORARY} cannot be read back: {text}"
        )
    else:
        assert str(caught.value) == (
            f"The copy of {live_database} did not pass its check, so it was not kept: {text}"
        )
    assert_nothing_kept(backup_directory)


def test_a_check_error_with_no_sqlite_code_is_integrity_failed(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def choose(target: str, real: sqlite3.Connection) -> Any:
        if target.endswith(".partial?mode=ro"):
            real.close()
            raise sqlite3.DatabaseError("no code at all")
        return real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert_nothing_kept(backup_directory)


def test_a_copy_that_cannot_leave_wal_mode_is_a_storage_error(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stays_wal(real: sqlite3.Connection, *arguments: Any) -> Any:
        if arguments[0] == "PRAGMA journal_mode=DELETE":
            return real.execute("PRAGMA journal_mode")
        return real.execute(*arguments)

    def choose(target: str, real: sqlite3.Connection) -> Any:
        return Connection(real, execute=stays_wal) if target.endswith(".partial") else real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert "stayed in journal mode ('wal',), not delete" in str(caught.value)
    assert_nothing_kept(backup_directory)


def test_a_copy_whose_journal_mode_cannot_be_set_is_a_storage_error(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuses(real: sqlite3.Connection, *arguments: Any) -> Any:
        if arguments[0] == "PRAGMA journal_mode=DELETE":
            raise sqlite_error(sqlite3.SQLITE_IOERR_WRITE, "SQLITE_IOERR_WRITE", "disk I/O error")
        return real.execute(*arguments)

    def choose(target: str, real: sqlite3.Connection) -> Any:
        return Connection(real, execute=refuses) if target.endswith(".partial") else real

    replace_connect(monkeypatch, choose)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert "cannot be made one file: disk I/O error (SQLITE_IOERR_WRITE)" in str(caught.value)
    assert_nothing_kept(backup_directory)


def test_a_rename_that_fails_is_a_storage_error_and_keeps_nothing(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, target: Any) -> Path:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "rename", refuse)

    with pytest.raises(BackupError) as caught:
        take(live_database, backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert f"The copy {backup_directory / NAME} cannot be stored" in str(caught.value)
    assert_nothing_kept(backup_directory)


class RecordingOs:
    """`os`, except that `open`, `fsync` and `close` are recorded, and a directory opens.

    Windows cannot open a directory to sync it, so a directory's descriptor is a fake one
    here. What is recorded is enough to tell the order: the file is synced before the
    rename, and the directory after it.
    """

    def __init__(self, final: Path) -> None:
        self.final = final
        self.events: list[tuple[str, str, bool]] = []
        self._paths: dict[int, Path] = {}
        self._fake = -1000

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def open(self, path: Any, flags: int, *arguments: Any) -> int:
        target = Path(path)
        if target.is_dir():
            self._fake -= 1
            descriptor = self._fake
        else:
            descriptor = os.open(path, flags, *arguments)
        self._paths[descriptor] = target
        self.events.append(("open", target.name, self.final.exists()))
        return descriptor

    def fsync(self, descriptor: int) -> None:
        target = self._paths[descriptor]
        self.events.append(("fsync", target.name, self.final.exists()))
        if descriptor >= 0:
            os.fsync(descriptor)

    def close(self, descriptor: int) -> None:
        self.events.append(("close", self._paths.pop(descriptor).name, self.final.exists()))
        if descriptor >= 0:
            os.close(descriptor)


def test_the_file_is_synced_before_the_rename_and_the_directory_after(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = RecordingOs(backup_directory / NAME)
    monkeypatch.setattr(backup, "os", recording)
    monkeypatch.setattr(backup, "_SYNC_DIRECTORIES", True)

    take(live_database, backup_directory)

    assert recording.events == [
        ("open", TEMPORARY, False),
        ("fsync", TEMPORARY, False),
        ("close", TEMPORARY, False),
        ("open", backup_directory.name, True),
        ("fsync", backup_directory.name, True),
        ("close", backup_directory.name, True),
    ]


def test_without_directory_sync_the_file_is_still_synced(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = RecordingOs(backup_directory / NAME)
    monkeypatch.setattr(backup, "os", recording)
    monkeypatch.setattr(backup, "_SYNC_DIRECTORIES", False)

    take(live_database, backup_directory)

    assert recording.events == [
        ("open", TEMPORARY, False),
        ("fsync", TEMPORARY, False),
        ("close", TEMPORARY, False),
    ]


def test_directories_are_synced_where_the_platform_can() -> None:
    assert backup._SYNC_DIRECTORIES is (os.name == "posix")


# --------------------------------------------------------------------------------------
# Listing and removing copies
# --------------------------------------------------------------------------------------


def test_list_copies_lists_only_copies_newest_first(backup_directory: Path) -> None:
    older, newest, middle = T0 - timedelta(days=2), T0, T0 - timedelta(hours=1)
    plant_copies(backup_directory, [older], b"12")
    plant_copies(backup_directory, [newest], b"1234")
    plant_copies(backup_directory, [middle], b"123")
    for stranger in ("notes.txt", "portfolio-20261302T030000123456Z.sqlite3", TEMPORARY):
        (backup_directory / stranger).write_bytes(b"not a copy")
    (backup_directory / backup_name(T0 - timedelta(days=9))).mkdir()

    assert list_copies(backup_directory) == (
        BackupFile(name=backup_name(newest), started_at=newest, size_bytes=4),
        BackupFile(name=backup_name(middle), started_at=middle, size_bytes=3),
        BackupFile(name=backup_name(older), started_at=older, size_bytes=2),
    )


def test_a_missing_directory_holds_no_copies(tmp_path: Path) -> None:
    assert list_copies(tmp_path / "never-made") == ()


def test_a_directory_that_cannot_be_read_is_a_storage_error(tmp_path: Path) -> None:
    in_the_way = tmp_path / "backups"
    in_the_way.write_bytes(b"a file")

    with pytest.raises(BackupError) as caught:
        list_copies(in_the_way)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert "cannot be read" in str(caught.value)


def test_a_copy_removed_while_the_directory_is_read_is_skipped(
    backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, kept = plant_copies(backup_directory, [T0 - timedelta(days=1), T0])
    real_is_file = Path.is_file

    def vanished(self: Path) -> bool:
        if self.name == gone:
            raise FileNotFoundError(2, "gone", str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", vanished)

    assert [copy_.name for copy_ in list_copies(backup_directory)] == [kept]


def test_a_copy_that_cannot_be_read_is_a_storage_error(
    backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (unreadable,) = plant_copies(backup_directory, [T0])
    real_stat = Path.stat

    def refuse(self: Path, *arguments: Any, **keywords: Any) -> os.stat_result:
        if self.name == unreadable:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *arguments, **keywords)

    monkeypatch.setattr(Path, "stat", refuse)

    with pytest.raises(BackupError) as caught:
        list_copies(backup_directory)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert f"The backup {backup_directory / unreadable} cannot be read" in str(caught.value)


def test_remove_copies_deletes_exactly_the_names_given(backup_directory: Path) -> None:
    doomed = plant_copies(backup_directory, [T0 - timedelta(days=3), T0 - timedelta(days=2)])
    kept = plant_copies(backup_directory, [T0])
    (backup_directory / "notes.txt").write_bytes(b"mine")
    already_gone = backup_name(T0 - timedelta(days=30))

    removed = remove_copies(backup_directory, iter([*doomed, already_gone]))

    assert removed == (*doomed, already_gone)
    assert copy_names(backup_directory) == {*kept, "notes.txt"}


@pytest.mark.parametrize("stranger", ["notes.txt", "../portfolio.db", TEMPORARY, ""])
def test_remove_copies_refuses_a_name_that_is_not_a_copy_before_deleting_anything(
    backup_directory: Path, stranger: str
) -> None:
    planted = plant_copies(backup_directory, [T0 - timedelta(days=1)])
    (backup_directory / "notes.txt").write_bytes(b"mine")

    with pytest.raises(ValueError, match="not a copy"):
        remove_copies(backup_directory, [*planted, stranger])

    assert copy_names(backup_directory) == {*planted, "notes.txt"}


def test_a_copy_that_cannot_be_deleted_is_a_storage_error(
    backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = plant_copies(backup_directory, [T0 - timedelta(days=2), T0 - timedelta(days=1)])
    real_unlink = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self.name == second:
            raise PermissionError(13, "Permission denied", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(BackupError) as caught:
        remove_copies(backup_directory, [first, second])

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert f"An old backup, {backup_directory / second}, could not be deleted" in str(caught.value)
    assert copy_names(backup_directory) == {second}


# --------------------------------------------------------------------------------------
# The database URL, the schema revisions, and the errors themselves
# --------------------------------------------------------------------------------------


def test_the_database_path_is_the_file_the_url_names_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert (
        database_path("sqlite+aiosqlite:///data/portfolio.db")
        == (tmp_path / "data" / "portfolio.db").resolve()
    )
    assert (
        database_path(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
        == (tmp_path / "x.db").resolve()
    )


def test_a_home_relative_database_path_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert (
        database_path("sqlite+aiosqlite:///~/portfolio.db") == (tmp_path / "portfolio.db").resolve()
    )


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite://",
        "sqlite+aiosqlite:///:memory:",
        "sqlite:///:memory:",
        "sqlite+aiosqlite:///file:shared?mode=memory&cache=shared&uri=true",
        "postgresql+asyncpg:///portfolio",
    ],
)
def test_a_url_with_no_database_file_is_a_database_error(url: str) -> None:
    with pytest.raises(BackupError) as caught:
        database_path(url)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value) == (
        "PORTFOLIO_DATABASE_URL names no database file, so there is nothing to copy. "
        "Backups need a file-backed SQLite database."
    )


def test_the_wal_is_beside_the_database() -> None:
    assert wal_path(Path("data") / "portfolio.db") == Path("data") / "portfolio.db-wal"


def test_the_known_revisions_are_the_packaged_migrations_and_head_is_what_they_migrate_to(
    live_database: Path,
) -> None:
    revisions, head = known_schema_revisions()

    with closing(reader(live_database)) as connection:
        (migrated_to,) = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert head == migrated_to
    assert head in revisions
    assert "0001_initial_schema" in revisions
    assert len(revisions) >= 10


def rebuilt_as_pickle_would(error: Any) -> Any:
    """What `pickle.loads(pickle.dumps(error))` does, without unpickling anything."""
    rebuild, arguments = error.__reduce__()
    return rebuild(*arguments)


@pytest.mark.parametrize(
    "error",
    [
        BackupError(BackupErrorKind.STORAGE_ERROR, "the disk is full"),
        RestoreRefusedError(RestoreRefusal.NEWER_SCHEMA, "too new"),
    ],
    ids=["backup error", "refusal"],
)
@pytest.mark.parametrize(
    "duplicate",
    [rebuilt_as_pickle_would, copy.copy, copy.deepcopy],
    ids=["pickle", "copy", "deepcopy"],
)
def test_the_errors_survive_a_copy_with_their_kind(
    error: BackupError | RestoreRefusedError, duplicate: Callable[[Any], Any]
) -> None:
    again = duplicate(error)

    assert type(again) is type(error)
    assert str(again) == str(error)
    if isinstance(error, BackupError):
        assert isinstance(again, BackupError)
        assert again.error_kind is error.error_kind
    else:
        assert isinstance(again, RestoreRefusedError)
        assert again.reason is error.reason


def test_the_refusal_reasons_are_the_five_the_spec_names() -> None:
    """The spec's three, R13's `not_self_contained` and R16's `leftover_journal`."""
    assert {reason.value for reason in RestoreRefusal} == {
        "database_open",
        "unknown_backup",
        "newer_schema",
        "not_self_contained",
        "leftover_journal",
    }


def test_backup_names_use_the_started_instant_not_the_clock(
    live_database: Path, backup_directory: Path
) -> None:
    """The copy's name is whatever instant it is handed, even one far in the past."""
    long_ago = datetime(2001, 2, 3, 4, 5, 6, 7, tzinfo=UTC)

    taken = take(live_database, backup_directory, long_ago)

    assert taken.name == "portfolio-20010203T040506000007Z.sqlite3"
    assert copy_names(backup_directory) == {taken.name}
