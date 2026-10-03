"""Spec 029 (#22), criterion 6: restoring a copy, and every way a restore refuses.

Every refusal is asserted twice: that it refuses, with its reason and the start of its
message, and that it **wrote nothing** -- the live database's rows and every file in the
backup directory, byte for byte, are what they were before. A refusal that took a safety
copy first would pass a test that only looked at the exception.

A restore that goes through is asserted row for row against the chosen copy, with the
safety copy holding the database as it was before, and nothing left beside the live file
that would make the application's next start, or the next restore, misread it.

Ruling R2 is the next block: a restore onto a data volume with no database takes no safety
copy and says so. Ruling R3 follows: a live database too damaged to copy is moved aside and
the restore goes on, while a sound one, or a backup directory that cannot be written, still
refuses with nothing moved. Then R11 (a copy whose header says WAL leaves nothing beside it),
R12 (a live file that cannot be read is refused, not moved), R13 (a copy with frames in a
`-wal` beside it is refused), R15 (the move aside never overwrites, takes a `-journal` with
it, and says which part failed) and R16 (no new database beside a leftover `-journal`).
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from portfolio.db import backup
from portfolio.db.backup import (
    BackupError,
    BackupFile,
    CheckFailedError,
    RestoreRefusal,
    RestoreRefusedError,
    RestoreResult,
    damaged_path,
    known_schema_revisions,
    restore_copy,
    take_copy,
    wal_path,
)
from portfolio.domain.backups import BackupErrorKind, backup_name, instant_of
from tests.backup_harness import (
    T0,
    add_notes,
    copy_names,
    corrupt_with_orphan_pages,
    damage_the_header,
    directory_state,
    execute,
    fixed,
    header_versions,
    integrity,
    plant_copies,
    row_counts,
    scribble_over_a_table,
    sidecars_of,
    table_contents,
    truncate_to_nothing,
)

if TYPE_CHECKING:
    from collections.abc import Callable

#: The safety copy is named from the clock the restore is handed: a minute after the copy.
RESTORED_AT: Final = T0 + timedelta(days=1)
SAFETY_NAME: Final = backup_name(RESTORED_AT)


def restore(database: Path, directory: Path, name: str) -> RestoreResult:
    return restore_copy(database, directory, name, clock=fixed(RESTORED_AT))


@pytest.fixture
def chosen(live_database: Path, backup_directory: Path) -> str:
    """A copy of the live database holding two notes, after which the live one moved on.

    The live database gains two notes and loses the first, so the copy and the live file
    differ in both directions -- a restore that only appended, or only deleted, fails -- and
    in their counts: two notes in the copy, three in the live file.
    """
    add_notes(live_database, "first note", "second note")
    name = take_copy(live_database, backup_directory, T0).name
    add_notes(live_database, "third note, after the copy", "fourth note, after the copy")
    execute(live_database, "DELETE FROM notes WHERE body = 'first note'")
    return name


def snapshot(live_database: Path, directory: Path) -> tuple[dict[str, str], dict[str, str]]:
    """What a refusal must leave as it was: every file's bytes, in both directories.

    Bytes rather than rows, and read without SQLite: a connection opened to read the live
    rows would itself change what is beside the live file -- a read-write one removes a
    leftover `-wal` when it closes, and a read-only one leaves one behind.
    """
    return directory_state(live_database.parent), directory_state(directory)


# --------------------------------------------------------------------------------------
# A restore that goes through
# --------------------------------------------------------------------------------------


def test_the_restored_database_equals_the_chosen_copy_row_for_row(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    copy_rows = table_contents(backup_directory / chosen)
    assert table_contents(live_database) != copy_rows

    restore(live_database, backup_directory, chosen)

    assert table_contents(live_database) == copy_rows
    assert integrity(live_database) == [("ok",)]


def test_the_safety_copy_holds_the_database_as_it_was_before(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    before = table_contents(live_database)

    result = restore(live_database, backup_directory, chosen)

    assert result.safety_copy == SAFETY_NAME
    assert table_contents(backup_directory / SAFETY_NAME) == before
    assert header_versions(backup_directory / SAFETY_NAME) == (1, 1)


def test_the_result_names_both_copies_and_counts_every_table(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    result = restore(live_database, backup_directory, chosen)

    assert result.restored == chosen
    assert dict(result.row_counts) == row_counts(backup_directory / chosen)
    assert list(result.row_counts) == sorted(result.row_counts)
    assert result.row_counts["notes"] == 2
    assert result.row_counts["alembic_version"] == 1
    assert not [table for table in result.row_counts if table.startswith("sqlite_")]


def test_a_restore_leaves_nothing_beside_the_live_database(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """No `-wal` and no `-shm`: the application can start, and the next restore is not refused."""
    restore(live_database, backup_directory, chosen)

    assert sidecars_of(live_database) == []
    again = restore_copy(
        live_database, backup_directory, chosen, clock=fixed(RESTORED_AT + timedelta(hours=1))
    )
    assert again.restored == chosen


def test_a_restore_can_be_undone_by_restoring_its_safety_copy(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    before = table_contents(live_database)
    result = restore(live_database, backup_directory, chosen)
    assert result.safety_copy is not None

    restore_copy(
        live_database,
        backup_directory,
        result.safety_copy,
        clock=fixed(RESTORED_AT + timedelta(hours=1)),
    )

    assert table_contents(live_database) == before


def test_a_restore_rotates_nothing(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """Thirty days of copies that rotation would thin out are all still there afterwards."""
    old = plant_copies(backup_directory, [T0 - timedelta(days=day) for day in range(1, 31)], b"x")

    restore(live_database, backup_directory, chosen)

    assert copy_names(backup_directory) == {*old, chosen, SAFETY_NAME}


def test_the_chosen_copy_is_not_changed_by_restoring_it(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    before = (backup_directory / chosen).read_bytes()

    restore(live_database, backup_directory, chosen)

    assert (backup_directory / chosen).read_bytes() == before


def test_a_copy_from_an_older_schema_revision_is_restored_as_it_is(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """A known revision that is not head is accepted: the application migrates it at startup."""
    revisions, head = known_schema_revisions()
    older = sorted(revisions - {head})[0]
    execute(backup_directory / chosen, ("UPDATE alembic_version SET version_num = ?", (older,)))

    result = restore(live_database, backup_directory, chosen)

    assert result.restored == chosen
    with closing(sqlite3.connect(live_database)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (older,)
        ]


# --------------------------------------------------------------------------------------
# Step 1: refused while the database is open
# --------------------------------------------------------------------------------------


def test_a_restore_is_refused_while_a_connection_holds_the_database(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    with closing(sqlite3.connect(live_database)) as application:
        application.execute("SELECT count(*) FROM notes").fetchone()
        assert wal_path(live_database).exists()
        before = snapshot(live_database, backup_directory)

        with pytest.raises(RestoreRefusedError) as caught:
            restore(live_database, backup_directory, chosen)

        assert caught.value.reason is RestoreRefusal.DATABASE_OPEN
        assert str(caught.value).startswith(
            f"Refusing to restore: {wal_path(live_database)} exists, so the database is open. "
            "Stop the application first."
        )
        assert "start it and stop it once" in str(caught.value)
        assert snapshot(live_database, backup_directory) == before


def test_a_wal_left_by_an_unclean_stop_is_refused_too(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """Nothing has the database open, but a `-wal` is there: refused, with no `--force`."""
    wal_path(live_database).write_bytes(b"")
    before = snapshot(live_database, backup_directory)

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.reason is RestoreRefusal.DATABASE_OPEN
    assert snapshot(live_database, backup_directory) == before
    assert wal_path(live_database).read_bytes() == b""


def test_the_open_database_is_refused_before_the_name_is_even_read(
    live_database: Path, backup_directory: Path
) -> None:
    wal_path(live_database).write_bytes(b"")

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, "not even a name")

    assert caught.value.reason is RestoreRefusal.DATABASE_OPEN


# --------------------------------------------------------------------------------------
# The data directory, and step 2: a name that is not a copy
# --------------------------------------------------------------------------------------


def test_a_missing_data_directory_is_a_database_error_that_writes_nothing(
    tmp_path: Path, backup_directory: Path
) -> None:
    (name,) = plant_copies(backup_directory, [T0], b"x")
    database = tmp_path / "unmounted" / "portfolio.db"
    before = directory_state(backup_directory)

    with pytest.raises(BackupError) as caught:
        restore(database, backup_directory, name)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value) == (
        f"The database's directory {database.parent} does not exist. Check "
        "PORTFOLIO_DATABASE_URL, and that the data volume is mounted."
    )
    assert not database.parent.exists()
    assert directory_state(backup_directory) == before


@pytest.mark.parametrize(
    "name",
    [
        "portfolio.db",
        "../data/portfolio.db",
        f"../backups/{backup_name(T0)}",
        f"{backup_name(T0)}\n",
        backup_name(T0).replace(".sqlite3", ".sqlite"),
        "portfolio-20261302T030000123456Z.sqlite3",
        "",
    ],
)
def test_a_name_that_is_not_a_copys_is_refused_and_writes_nothing(
    live_database: Path, backup_directory: Path, chosen: str, name: str
) -> None:
    del chosen  # A real copy is there, under its own name.
    before = snapshot(live_database, backup_directory)

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, name)

    assert caught.value.reason is RestoreRefusal.UNKNOWN_BACKUP
    assert str(caught.value) == (
        f"Refusing to restore: {name!r} is not the name of a backup. A backup is named "
        "portfolio-YYYYMMDDTHHMMSSffffffZ.sqlite3, and list-backups shows them."
    )
    assert snapshot(live_database, backup_directory) == before


def test_a_copys_name_with_no_such_file_is_refused_and_writes_nothing(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    del chosen
    missing = backup_name(T0 - timedelta(days=3))
    before = snapshot(live_database, backup_directory)

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, missing)

    assert caught.value.reason is RestoreRefusal.UNKNOWN_BACKUP
    assert str(caught.value) == (
        f"Refusing to restore: there is no backup named {missing} in {backup_directory}. "
        "list-backups shows the ones there are."
    )
    assert snapshot(live_database, backup_directory) == before


def test_a_directory_named_like_a_copy_is_not_a_copy(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    del chosen
    impostor = backup_name(T0 - timedelta(days=4))
    (backup_directory / impostor).mkdir()

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, impostor)

    assert caught.value.reason is RestoreRefusal.UNKNOWN_BACKUP


# --------------------------------------------------------------------------------------
# Step 3: the copy is checked, and one from a newer schema is refused
# --------------------------------------------------------------------------------------


def test_a_copy_that_fails_its_integrity_check_is_refused_and_writes_nothing(
    live_database: Path, backup_directory: Path, tmp_path: Path
) -> None:
    damaged_source = tmp_path / "damaged" / "portfolio.db"
    damaged_source.parent.mkdir()
    shutil.copyfile(live_database, damaged_source)
    corrupt_with_orphan_pages(damaged_source)
    # One file, as every copy `take_copy` keeps is: a WAL header would make the check's
    # read-only connection leave a `-wal` beside it, which is not what this test is about.
    execute(damaged_source, "PRAGMA journal_mode=DELETE")
    name = backup_name(T0)
    backup_directory.mkdir()
    shutil.copyfile(damaged_source, backup_directory / name)
    before = snapshot(live_database, backup_directory)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, name)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert str(caught.value).startswith(
        f"Refusing to restore: the backup {name} did not pass its check ("
    )
    assert str(caught.value).endswith("). Nothing was changed. Choose another backup.")
    assert "never used" not in str(caught.value)
    assert snapshot(live_database, backup_directory) == before


@pytest.mark.parametrize(
    "content",
    [b"", b"definitely not a database, but long enough to look like a header" * 50],
    ids=["empty", "garbage"],
)
def test_a_file_that_is_not_a_database_is_refused_and_writes_nothing(
    live_database: Path, backup_directory: Path, content: bytes
) -> None:
    (name,) = plant_copies(backup_directory, [T0], content)
    before = snapshot(live_database, backup_directory)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, name)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert snapshot(live_database, backup_directory) == before


@pytest.mark.parametrize(
    ("statement", "rows"),
    [
        ("DELETE FROM alembic_version", 0),
        ("INSERT INTO alembic_version (version_num) VALUES ('0000_another')", 2),
    ],
    ids=["no revision", "two revisions"],
)
def test_a_copy_without_exactly_one_revision_is_refused_and_writes_nothing(
    live_database: Path, backup_directory: Path, chosen: str, statement: str, rows: int
) -> None:
    execute(backup_directory / chosen, statement)
    before = snapshot(live_database, backup_directory)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert f"alembic_version holds {rows} rows, not one" in str(caught.value)
    assert snapshot(live_database, backup_directory) == before


def test_a_copy_from_a_newer_schema_is_refused_naming_both_revisions(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    _, head = known_schema_revisions()
    newer = "9999_from_a_newer_version"
    execute(backup_directory / chosen, ("UPDATE alembic_version SET version_num = ?", (newer,)))
    before = snapshot(live_database, backup_directory)

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.reason is RestoreRefusal.NEWER_SCHEMA
    assert str(caught.value) == (
        f"Refusing to restore: {chosen} is at schema revision {newer}, which this version "
        f"does not know; the newest it knows is {head}. The copy was taken by a newer version "
        "of the application, which this one cannot migrate. Restore it with that version or "
        "a newer one."
    )
    assert snapshot(live_database, backup_directory) == before


# --------------------------------------------------------------------------------------
# Failures after the safety copy name it
# --------------------------------------------------------------------------------------


@pytest.fixture
def small_pages(backup_directory: Path, tmp_path: Path) -> str:
    """A copy that passes every check but cannot be written into a WAL database.

    Its page size is 1024 and the live database's is 4096. A WAL database cannot change its
    page size, so SQLite refuses the backup step with "attempt to write a readonly
    database" -- the one real write failure a test can produce on cue.
    """
    _, head = known_schema_revisions()
    source = tmp_path / "small-pages.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("PRAGMA page_size=1024")
        connection.execute("VACUUM")
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES (?)", (head,))
        connection.commit()
    backup_directory.mkdir(exist_ok=True)
    name = backup_name(T0 - timedelta(days=2))
    shutil.copyfile(source, backup_directory / name)
    return name


def test_a_write_that_fails_names_the_safety_copy_and_leaves_the_database_as_it_was(
    live_database: Path, backup_directory: Path, chosen: str, small_pages: str
) -> None:
    del chosen
    before = table_contents(live_database)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, small_pages)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value).startswith(f"Writing the backup into {live_database} failed: ")
    assert str(caught.value).endswith(
        f"The safety copy {SAFETY_NAME} holds the database as it was before the restore."
    )
    assert table_contents(live_database) == before
    assert table_contents(backup_directory / SAFETY_NAME) == before


def test_restored_rows_that_differ_from_the_copys_are_an_integrity_failure(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check: the counts read after the write must be the copy's own.

    The write is replaced with one that writes nothing, so the live database keeps its
    three notes against the copy's two.
    """
    monkeypatch.setattr(backup, "_copy_over", lambda *arguments: None)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert str(caught.value) == (
        f"After the restore, the rows per table of {live_database} differ from {chosen}'s. "
        f"The safety copy {SAFETY_NAME} holds the database as it was before the restore."
    )


def test_a_restored_database_that_fails_its_check_is_an_integrity_failure(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write is replaced with one that leaves a damaged database behind."""

    def write_damage(chosen_path: Path, database: Path, safety: str | None) -> None:
        del chosen_path, safety
        corrupt_with_orphan_pages(database)

    monkeypatch.setattr(backup, "_copy_over", write_damage)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert str(caught.value).startswith(
        f"After the restore, {live_database} did not pass its check ("
    )
    assert str(caught.value).endswith(
        f"The safety copy {SAFETY_NAME} holds the database as it was before the restore."
    )
    assert "never used" not in str(caught.value)


def test_a_restored_database_that_cannot_be_read_is_a_database_error(
    tmp_path: Path, backup_directory: Path, live_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No live database, so no safety copy, and a write that left no file behind."""
    name = take_copy(live_database, backup_directory, T0).name
    database = tmp_path / "fresh" / "portfolio.db"
    database.parent.mkdir()
    monkeypatch.setattr(backup, "_copy_over", lambda *arguments: None)

    with pytest.raises(BackupError) as caught:
        restore(database, backup_directory, name)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value).startswith(f"After the restore, {database} cannot be read: ")
    assert str(caught.value).endswith(
        "There was no database before the restore, so there is no safety copy."
    )
    assert not database.exists()


# --------------------------------------------------------------------------------------
# Ruling R2: no live database, so no safety copy
# --------------------------------------------------------------------------------------


def test_a_restore_onto_an_empty_data_volume_takes_no_safety_copy(
    tmp_path: Path, backup_directory: Path, live_database: Path
) -> None:
    add_notes(live_database, "kept in the backups volume")
    name = take_copy(live_database, backup_directory, T0).name
    fresh = tmp_path / "new-volume" / "portfolio.db"
    fresh.parent.mkdir()

    result = restore(fresh, backup_directory, name)

    assert result.safety_copy is None
    assert result.restored == name
    assert table_contents(fresh) == table_contents(backup_directory / name)
    assert copy_names(backup_directory) == {name}
    assert sidecars_of(fresh) == []
    assert integrity(fresh) == [("ok",)]


# --------------------------------------------------------------------------------------
# Ruling R3: a damaged live database is moved aside, and the restore goes on
# --------------------------------------------------------------------------------------

#: Where a damaged live database goes: beside it, named from the restore's clock.
DAMAGED_NAME: Final = "portfolio.db.damaged-20261003T030000123456Z"


def safety_copy_failures(monkeypatch: pytest.MonkeyPatch) -> list[BackupErrorKind]:
    """Record the kind each `take_copy` inside a restore fails with, and let it fail."""
    kinds: list[BackupErrorKind] = []
    real_take_copy = backup.take_copy

    def recording(database: Path, directory: Path, started_at: datetime) -> BackupFile:
        try:
            return real_take_copy(database, directory, started_at)
        except BackupError as exc:
            kinds.append(exc.error_kind)
            raise

    monkeypatch.setattr(backup, "take_copy", recording)
    return kinds


def scribble_over_the_notes(database: Path) -> None:
    scribble_over_a_table(database, "notes")


DAMAGE: Final[dict[str, tuple[Callable[[Path], None], BackupErrorKind]]] = {
    "a scribbled page": (scribble_over_the_notes, BackupErrorKind.INTEGRITY_FAILED),
    "a damaged header": (damage_the_header, BackupErrorKind.DATABASE_ERROR),
    "a 0-byte file": (truncate_to_nothing, BackupErrorKind.INTEGRITY_FAILED),
}


@pytest.mark.parametrize("damage", list(DAMAGE))
def test_a_damaged_live_database_is_moved_aside_and_the_restore_goes_on(
    live_database: Path,
    backup_directory: Path,
    chosen: str,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    """The reviewer's three damages. Each fails the safety copy the way the module docstring
    says it was measured to, and each is exactly when the owner needs the restore."""
    harm, safety_kind = DAMAGE[damage]
    harm(live_database)
    damaged_bytes = live_database.read_bytes()
    failures = safety_copy_failures(monkeypatch)

    result = restore(live_database, backup_directory, chosen)

    assert failures == [safety_kind]
    assert result.safety_copy is None
    assert result.damaged == live_database.with_name(DAMAGED_NAME)
    assert result.damaged.read_bytes() == damaged_bytes
    assert table_contents(live_database) == table_contents(backup_directory / chosen)
    assert dict(result.row_counts) == row_counts(backup_directory / chosen)
    assert integrity(live_database) == [("ok",)]
    assert sidecars_of(live_database) == []
    assert copy_names(backup_directory) == {chosen}, "a failed safety copy left something"
    assert copy_names(live_database.parent) == {live_database.name, DAMAGED_NAME}


def test_the_damaged_name_is_the_stamp_of_the_move_and_no_copys_name(tmp_path: Path) -> None:
    moved = damaged_path(tmp_path / "portfolio.db", RESTORED_AT)

    assert moved == tmp_path / DAMAGED_NAME
    assert instant_of(moved.name) is None, "rotation or list-backups could reach it"
    with pytest.raises(ValueError, match="timezone-aware"):
        damaged_path(tmp_path / "portfolio.db", RESTORED_AT.replace(tzinfo=None))


def test_a_stray_shared_memory_file_beside_a_damaged_database_is_removed(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """It may not be paired with the database the restore writes at the old name."""
    truncate_to_nothing(live_database)
    shared = live_database.with_name(f"{live_database.name}-shm")
    shared.write_bytes(b"\x00" * 32_768)

    restore(live_database, backup_directory, chosen)

    assert not shared.exists()
    assert sidecars_of(live_database) == []
    assert table_contents(live_database) == table_contents(backup_directory / chosen)


def test_an_empty_wal_left_by_the_restores_own_reads_is_removed(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only connection opens an empty `-wal` and never writes a frame into it."""
    truncate_to_nothing(live_database)
    wal = wal_path(live_database)

    def check_and_leave_an_empty_wal(database: Path) -> None:
        del database
        wal.write_bytes(b"")
        message = "the live file did not pass"
        raise CheckFailedError(message)

    monkeypatch.setattr(backup, "_check_live", check_and_leave_an_empty_wal)

    result = restore(live_database, backup_directory, chosen)

    assert result.damaged == live_database.with_name(DAMAGED_NAME)
    assert not wal.exists()
    assert table_contents(live_database) == table_contents(backup_directory / chosen)


def test_a_wal_with_content_that_appeared_during_the_restore_refuses_and_moves_nothing(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something opened the database read-write after step 1: it is not stopped after all."""
    truncate_to_nothing(live_database)
    wal = wal_path(live_database)

    def check_while_something_writes(database: Path) -> None:
        del database
        wal.write_bytes(b"a frame somebody else wrote")
        message = "the live file did not pass"
        raise CheckFailedError(message)

    monkeypatch.setattr(backup, "_check_live", check_while_something_writes)

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.reason is RestoreRefusal.DATABASE_OPEN
    assert str(caught.value) == (
        f"Refusing to restore: {wal} appeared during the restore, so the database was "
        "opened. Nothing was changed. Stop the application, then restore again."
    )
    assert live_database.read_bytes() == b""
    assert copy_names(live_database.parent) == {live_database.name, wal.name}
    assert copy_names(backup_directory) == {chosen}


@pytest.mark.parametrize("kind", [BackupErrorKind.INTEGRITY_FAILED, BackupErrorKind.DATABASE_ERROR])
def test_a_live_database_that_passes_its_own_check_is_not_moved_and_the_restore_refuses(
    live_database: Path,
    backup_directory: Path,
    chosen: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: BackupErrorKind,
) -> None:
    """The safety copy failed, but the live file is sound: the fault was in writing the copy,
    and moving a sound database aside would be the restore doing harm."""
    failure = BackupError(kind, "the safety copy failed")

    def fail(database: Path, directory: Path, started_at: datetime) -> BackupFile:
        raise failure

    monkeypatch.setattr(backup, "take_copy", fail)
    before = snapshot(live_database, backup_directory)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.error_kind is kind
    assert caught.value.__cause__ is failure
    assert str(caught.value) == (
        f"Refusing to restore: no safety copy of {live_database} could be taken, and the live "
        "database passes its own check, so it was not moved aside. Nothing was changed. "
        "the safety copy failed"
    )
    assert snapshot(live_database, backup_directory) == before


@pytest.mark.parametrize("damaged", [False, True], ids=["sound", "damaged"])
def test_a_safety_copy_that_cannot_be_written_refuses_and_leaves_the_live_file_alone(
    live_database: Path,
    backup_directory: Path,
    chosen: str,
    monkeypatch: pytest.MonkeyPatch,
    damaged: bool,
) -> None:
    """`storage_error` is the backup directory's fault and says nothing about the live file,
    which is not even checked -- however damaged it is."""
    if damaged:
        damage_the_header(live_database)
    failure = BackupError(BackupErrorKind.STORAGE_ERROR, "the backup directory is full")

    def fail(database: Path, directory: Path, started_at: datetime) -> BackupFile:
        raise failure

    def never(database: Path) -> None:
        pytest.fail(f"{database} was checked after a storage error")

    monkeypatch.setattr(backup, "take_copy", fail)
    monkeypatch.setattr(backup, "_check_live", never)
    before = snapshot(live_database, backup_directory)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert caught.value.__cause__ is failure
    assert str(caught.value) == (
        f"Refusing to restore: no safety copy of {live_database} could be taken, so nothing "
        "was changed. the backup directory is full"
    )
    assert snapshot(live_database, backup_directory) == before


def test_the_data_directory_is_synced_after_the_move(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    damage_the_header(live_database)
    synced: list[tuple[Path, bool, bool]] = []
    moved = live_database.with_name(DAMAGED_NAME)

    def record(directory: Path) -> None:
        synced.append((directory, moved.exists(), live_database.exists()))

    monkeypatch.setattr(backup, "_fsync_directory", record)

    restore(live_database, backup_directory, chosen)

    assert synced == [(live_database.parent, True, False)]


def test_a_move_that_fails_is_a_database_error_and_restores_nothing(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    damage_the_header(live_database)
    damaged_bytes = live_database.read_bytes()
    real_rename = Path.rename

    def refuse(self: Path, target: Any) -> Path:
        if self == live_database:
            raise PermissionError(13, "Permission denied", str(self))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value).startswith(
        f"The live database {live_database} did not pass its own check, and moving it aside "
        f"to {live_database.with_name(DAMAGED_NAME)} failed: "
    )
    assert str(caught.value).endswith(". Nothing was restored, and it is where it was.")
    assert isinstance(caught.value.__cause__, PermissionError)
    assert live_database.read_bytes() == damaged_bytes
    assert copy_names(backup_directory) == {chosen}


def test_a_leftover_that_cannot_be_removed_after_the_move_says_where_the_database_is(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    damage_the_header(live_database)
    shared = live_database.with_name(f"{live_database.name}-shm")
    shared.write_bytes(b"\x00" * 32_768)
    real_unlink = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self == shared:
            raise PermissionError(13, "in use", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    moved = live_database.with_name(DAMAGED_NAME)
    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value).startswith(
        f"The damaged live database was moved aside to {moved}, but {shared} cannot be removed: "
    )
    assert str(caught.value).endswith(". Nothing was restored.")
    assert moved.is_file()
    assert not live_database.exists()


def test_a_failure_after_the_move_says_where_the_damaged_database_is(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no safety copy to name, so the message names the moved file instead."""
    damage_the_header(live_database)
    monkeypatch.setattr(backup, "_copy_over", lambda *arguments: None)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value).endswith(
        "The live database did not pass its own check and was moved aside to "
        f"{live_database.with_name(DAMAGED_NAME)}, which holds it as it was; there is no "
        "safety copy."
    )


def test_checking_the_live_file_leaves_nothing_beside_it(live_database: Path) -> None:
    """`immutable=1`: a plain read-only connection would leave a `-wal` (R1), and a `-wal`
    with content beside the file is what makes the move refuse."""
    backup._check_live(live_database)

    assert sidecars_of(live_database) == []


# --------------------------------------------------------------------------------------
# Ruling R11: a copy whose header says WAL is read without leaving anything beside it
# --------------------------------------------------------------------------------------


@pytest.fixture
def wal_chosen(backup_directory: Path, chosen: str) -> str:
    """`chosen`, switched to WAL: its header says so, as a copy brought back by hand can.

    The copies this application takes are one self-contained file, but a copy taken by
    other means and put into the directory by hand need not be. Closed cleanly by the
    connection that switched it, so nothing is beside it and every row is in the file.
    """
    copy = backup_directory / chosen
    with closing(sqlite3.connect(copy)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    assert header_versions(copy) == (2, 2)
    assert sidecars_of(copy) == []
    return chosen


def test_a_copy_whose_header_says_wal_is_restored_and_nothing_is_left_beside_it(
    live_database: Path, backup_directory: Path, wal_chosen: str
) -> None:
    """R11: neither the check of step 3 nor the read of step 5 leaves a `-wal` or `-shm`.

    A read-only connection to a WAL database creates both and does not remove them (R1's
    premise). Beside a copy they are clutter nothing rotates, and the copy's own bytes must
    not change either: reading a copy is not a reason to write it.
    """
    copy = backup_directory / wal_chosen
    copy_rows = table_contents(copy)
    copy_bytes = directory_state(backup_directory)[wal_chosen]

    result = restore(live_database, backup_directory, wal_chosen)

    assert sidecars_of(copy) == []
    assert directory_state(backup_directory) == {
        wal_chosen: copy_bytes,
        SAFETY_NAME: directory_state(backup_directory)[SAFETY_NAME],
    }
    assert result.safety_copy == SAFETY_NAME
    assert table_contents(live_database) == copy_rows
    assert sidecars_of(live_database) == []


@pytest.mark.parametrize(
    ("statement", "refused_with"),
    [
        (
            ("UPDATE alembic_version SET version_num = ?", ("9999_from_a_newer_version",)),
            RestoreRefusedError,
        ),
        (
            ("INSERT INTO alembic_version (version_num) VALUES (?)", ("0000_not_a_revision",)),
            BackupError,
        ),
    ],
    ids=["a newer schema", "two revisions"],
)
def test_a_copy_whose_header_says_wal_and_is_refused_leaves_nothing_beside_it(
    live_database: Path,
    backup_directory: Path,
    wal_chosen: str,
    statement: tuple[str, tuple[Any, ...]],
    refused_with: type[Exception],
) -> None:
    """R11 on the refusing path: step 3 reads the copy, refuses, and writes nothing at all."""
    copy = backup_directory / wal_chosen
    execute(copy, statement)
    assert (header_versions(copy), sidecars_of(copy)) == ((2, 2), [])
    before = snapshot(live_database, backup_directory)

    with pytest.raises(refused_with):
        restore(live_database, backup_directory, wal_chosen)

    assert snapshot(live_database, backup_directory) == before


# --------------------------------------------------------------------------------------
# Ruling R12: a live database that cannot be read is refused, not moved aside
# --------------------------------------------------------------------------------------


def sqlite_error(code: int | None, text: str) -> sqlite3.Error:
    """An error as SQLite raises it, with the result code `_check_live` reads; `None` for one
    that carries no code at all, as an error this module did not get from SQLite would."""
    error = sqlite3.OperationalError(text)
    if code is not None:
        error.sqlite_errorcode = code
    return error


def safety_copy_that_cannot_read(monkeypatch: pytest.MonkeyPatch) -> BackupError:
    """Step 4's copy fails as a copy of an unreadable file does: `database_error`."""
    failure = BackupError(BackupErrorKind.DATABASE_ERROR, "the safety copy could not read it")

    def fail(database: Path, directory: Path, started_at: datetime) -> BackupFile:
        raise failure

    monkeypatch.setattr(backup, "take_copy", fail)
    return failure


CANNOT_READ: Final = {
    "cannot open": sqlite3.SQLITE_CANTOPEN,
    "cannot open, a directory": sqlite3.SQLITE_CANTOPEN_ISDIR,
    "an I/O error": sqlite3.SQLITE_IOERR,
    "a failed read": sqlite3.SQLITE_IOERR_READ,
    "a short read": sqlite3.SQLITE_IOERR_SHORT_READ,
    "a denied access check": sqlite3.SQLITE_IOERR_ACCESS,
}


@pytest.mark.parametrize("code", list(CANNOT_READ.values()), ids=list(CANNOT_READ))
def test_a_live_database_that_cannot_be_read_refuses_and_stays_where_it_is(
    live_database: Path,
    backup_directory: Path,
    chosen: str,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    """R12: an I/O fault or a permission says nothing about what the file holds, so a file
    that cannot be read at that moment is not judged damaged. Every `SQLITE_IOERR_*` counts,
    by its primary code; so does `SQLITE_CANTOPEN` with its extended ones."""
    safety_copy_that_cannot_read(monkeypatch)
    error = sqlite_error(code, "disk I/O error")

    def unreadable(database: Path) -> None:
        raise error

    monkeypatch.setattr(backup, "_check_live", unreadable)
    before = snapshot(live_database, backup_directory)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert caught.value.__cause__ is error
    assert str(caught.value) == (
        f"Refusing to restore: the live database {live_database} cannot be read "
        "(disk I/O error), so no safety copy could be taken, and a file that cannot be read "
        "is not judged damaged. Nothing was changed: it is where it was. Check the data "
        "volume, its permissions and the storage device, then restore again."
    )
    assert snapshot(live_database, backup_directory) == before


def test_sqlites_own_cannot_open_error_is_refused_with_its_name(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error SQLite itself raises when it cannot open a file -- here the real check run on
    a path with nothing behind it -- and the message carries SQLite's name for it."""
    safety_copy_that_cannot_read(monkeypatch)
    real_check_live = backup._check_live
    monkeypatch.setattr(
        backup, "_check_live", lambda database: real_check_live(database.with_name("gone.db"))
    )
    before = snapshot(live_database, backup_directory)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    cause = caught.value.__cause__
    assert isinstance(cause, sqlite3.OperationalError)
    assert cause.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN
    assert (
        f"the live database {live_database} cannot be read "
        "(unable to open database file (SQLITE_CANTOPEN))"
    ) in str(caught.value)
    assert snapshot(live_database, backup_directory) == before


OPENS_AND_IS_WRONG: Final = {
    "not a database": sqlite3.SQLITE_NOTADB,
    "corrupt": sqlite3.SQLITE_CORRUPT,
    "corrupt, extended": sqlite3.SQLITE_CORRUPT_VTAB,
    "no such table": sqlite3.SQLITE_ERROR,
    "read-only": sqlite3.SQLITE_READONLY,
    "no code at all": None,
}


@pytest.mark.parametrize("code", list(OPENS_AND_IS_WRONG.values()), ids=list(OPENS_AND_IS_WRONG))
def test_every_other_error_from_the_live_check_still_moves_it_aside(
    live_database: Path,
    backup_directory: Path,
    chosen: str,
    monkeypatch: pytest.MonkeyPatch,
    code: int | None,
) -> None:
    """R12 keeps R3 for a file that opens and is wrong: only the two cannot-read families are
    refused. `SQLITE_CORRUPT_VTAB` is 267 -- 11 plus an extended byte -- and must not be read
    as anything but corrupt."""
    safety_copy_that_cannot_read(monkeypatch)
    error = sqlite_error(code, "the file is wrong")

    def wrong(database: Path) -> None:
        raise error

    monkeypatch.setattr(backup, "_check_live", wrong)

    result = restore(live_database, backup_directory, chosen)

    assert result.damaged == live_database.with_name(DAMAGED_NAME)
    assert table_contents(live_database) == table_contents(backup_directory / chosen)


# --------------------------------------------------------------------------------------
# Ruling R13: a chosen copy with a -wal that holds frames is refused
# --------------------------------------------------------------------------------------


def frames_beside(copy: Path, body: str) -> bytes:
    """Leave `copy` as it was, with a `-wal` beside it holding one more note than it does.

    What a copy carried off by hand with its `-wal` looks like: the transaction is in the
    `-wal` and nowhere in the file. The file is switched to WAL, the note is written with
    automatic checkpoints off, the `-wal` is read while the connection still holds it, and the
    file's bytes from before the note are put back once the connection has closed.
    """
    with closing(sqlite3.connect(copy)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    without_the_note = copy.read_bytes()
    with closing(sqlite3.connect(copy)) as connection:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO notes (body) VALUES (?)", (body,))
        connection.commit()
        frames = wal_path(copy).read_bytes()
    copy.write_bytes(without_the_note)
    wal_path(copy).write_bytes(frames)
    assert len(frames) > 0
    assert sidecars_of(copy) == [f"{copy.name}-wal"]
    return frames


def test_a_copy_with_frames_in_a_wal_beside_it_is_refused_and_nothing_changes(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """R13: the restore reads the copy immutably, the file alone (R11), so the note in the
    `-wal` would be silently left out. It is refused, and the message says how to make the
    copy one file."""
    copy = backup_directory / chosen
    frames_beside(copy, "a note only the -wal holds")
    before = snapshot(live_database, backup_directory)

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.reason is RestoreRefusal.NOT_SELF_CONTAINED
    assert str(caught.value) == (
        f"Refusing to restore: {chosen} is not a self-contained copy: {chosen}-wal beside it "
        "holds transactions the file itself does not, and the restore reads the file alone. "
        "Nothing was changed. Copy both files out of the backup directory, open the copy there "
        "once with sqlite3, which writes those transactions into it, and copy it back alone."
    )
    assert snapshot(live_database, backup_directory) == before


def test_the_refusal_comes_before_the_copy_is_checked(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """R13 is step 2: a copy with frames beside it is refused as not self-contained even when
    its file would also fail step 3's check -- the `-wal` is the reason to give first."""
    copy = backup_directory / chosen
    corrupt_with_orphan_pages(copy)
    wal_path(copy).write_bytes(b"frames from elsewhere")
    before = snapshot(live_database, backup_directory)

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.reason is RestoreRefusal.NOT_SELF_CONTAINED
    assert snapshot(live_database, backup_directory) == before


def test_a_wal_beside_a_name_that_is_not_there_is_still_an_unknown_backup(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """The copy must exist before what is beside it means anything."""
    missing = backup_name(T0 - timedelta(days=3))
    (backup_directory / f"{missing}-wal").write_bytes(b"frames of nothing")

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, missing)

    assert caught.value.reason is RestoreRefusal.UNKNOWN_BACKUP
    del chosen


def test_an_empty_wal_beside_the_copy_holds_nothing_and_is_no_reason_to_refuse(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """A `-wal` of zero bytes holds no transaction, so the file is the whole copy. It is left
    as it was: the restore reads the copy and writes nothing beside it."""
    copy = backup_directory / chosen
    wal_path(copy).write_bytes(b"")
    copy_rows = table_contents(copy)

    result = restore(live_database, backup_directory, chosen)

    assert result.restored == chosen
    assert table_contents(live_database) == copy_rows
    assert wal_path(copy).read_bytes() == b""


# --------------------------------------------------------------------------------------
# Ruling R15: the move aside never overwrites, takes a -journal along, and says what failed
# --------------------------------------------------------------------------------------

MOVED_AT: Final = RESTORED_AT


@pytest.mark.parametrize("taken", ["the file's name", "its journal's name"])
def test_a_move_aside_onto_a_name_already_taken_refuses_and_changes_nothing(
    live_database: Path, taken: str
) -> None:
    """R15: the target is the instant to the microsecond, so a file already there means a
    wrong clock or a hand-placed file -- and overwriting it could destroy an earlier damaged
    database. On `_move_aside` itself, for the journal's reason the journal tests give."""
    damage_the_header(live_database)
    target = live_database.with_name(DAMAGED_NAME)
    if taken == "the file's name":
        target.write_bytes(b"an earlier damaged database")
        occupied = target
    else:
        backup._journal_path(live_database).write_bytes(b"a journal of the damaged file")
        occupied = target.with_name(f"{target.name}-journal")
        occupied.write_bytes(b"an earlier damaged database's journal")
    before = directory_state(live_database.parent)

    with pytest.raises(BackupError) as caught:
        backup._move_aside(live_database, MOVED_AT)

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert str(caught.value) == (
        f"Refusing to restore: the live database {live_database} did not pass its own check, "
        f"and {occupied}, where it would be moved aside, already exists. Nothing was changed: "
        "the live database is where it was."
    )
    assert directory_state(live_database.parent) == before


def test_a_restore_onto_a_taken_name_refuses_with_the_live_file_in_place(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """The same refusal through a whole restore: nothing in either directory changes."""
    damage_the_header(live_database)
    live_database.with_name(DAMAGED_NAME).write_bytes(b"an earlier damaged database")
    before = snapshot(live_database, backup_directory)

    with pytest.raises(BackupError, match="already exists"):
        restore(live_database, backup_directory, chosen)

    assert snapshot(live_database, backup_directory) == before


def test_a_journal_beside_the_damaged_file_is_moved_with_it_under_its_name(
    live_database: Path,
) -> None:
    """R15: a rollback journal belongs to the file it was written for. Called on `_move_aside`
    itself because a whole restore's step 4 ends with R1's read-write open, which lets SQLite
    roll a journal back or remove it first, as the module docstring says."""
    damage_the_header(live_database)
    database_bytes = live_database.read_bytes()
    journal = backup._journal_path(live_database)
    journal.write_bytes(b"pages from before an interrupted transaction")
    live_database.with_name(f"{live_database.name}-shm").write_bytes(b"\x00" * 32_768)
    wal_path(live_database).write_bytes(b"")

    moved = backup._move_aside(live_database, MOVED_AT)

    assert moved == live_database.with_name(DAMAGED_NAME)
    assert copy_names(live_database.parent) == {DAMAGED_NAME, f"{DAMAGED_NAME}-journal"}
    assert moved.read_bytes() == database_bytes
    assert moved.with_name(f"{DAMAGED_NAME}-journal").read_bytes() == (
        b"pages from before an interrupted transaction"
    )


def test_a_journal_that_cannot_be_moved_says_where_both_are(
    live_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is already moved; the journal is not. The operator must move it by hand, and
    R16 refuses the next restore until they do."""
    damage_the_header(live_database)
    journal = backup._journal_path(live_database)
    journal.write_bytes(b"pages from before an interrupted transaction")
    real_rename = Path.rename

    def refuse(self: Path, target: Any) -> Path:
        if self == journal:
            raise PermissionError(13, "Permission denied", str(self))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse)

    with pytest.raises(BackupError) as caught:
        backup._move_aside(live_database, MOVED_AT)

    moved = live_database.with_name(DAMAGED_NAME)
    destination = moved.with_name(f"{DAMAGED_NAME}-journal")
    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert isinstance(caught.value.__cause__, PermissionError)
    assert str(caught.value).startswith(
        f"The damaged live database was moved aside to {moved}, but moving {journal} to "
        f"{destination} failed: "
    )
    assert str(caught.value).endswith(
        ". Nothing was restored. Move it there by hand before restoring again: it belongs to "
        "the damaged file."
    )
    assert moved.is_file()
    assert journal.is_file()
    assert not destination.exists()


def test_a_sync_that_fails_after_the_rename_says_the_file_was_moved(
    live_database: Path, backup_directory: Path, chosen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R15: the rename went through, so the message names where the file now is, and says it
    is the durability that failed -- not the move."""
    damage_the_header(live_database)
    damaged_bytes = live_database.read_bytes()
    failure = OSError(5, "Input/output error")

    def fail(directory: Path) -> None:
        raise failure

    monkeypatch.setattr(backup, "_fsync_directory", fail)

    with pytest.raises(BackupError) as caught:
        restore(live_database, backup_directory, chosen)

    moved = live_database.with_name(DAMAGED_NAME)
    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert caught.value.__cause__ is failure
    assert str(caught.value) == (
        f"The damaged live database was moved aside to {moved}, but the move could not be "
        f"made durable: syncing {live_database.parent} failed: {failure}. Nothing was restored."
    )
    assert moved.read_bytes() == damaged_bytes
    assert not live_database.exists()


# --------------------------------------------------------------------------------------
# Ruling R16: no new database is written beside a leftover -journal
# --------------------------------------------------------------------------------------


def test_a_leftover_journal_where_there_is_no_database_refuses_and_changes_nothing(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """R16: the journal belongs to a database that is gone -- most likely one an earlier
    restore moved aside without it -- and writing a new file would make SQLite discard it."""
    live_database.unlink()
    journal = backup._journal_path(live_database)
    journal.write_bytes(b"pages from before an interrupted transaction")
    before = snapshot(live_database, backup_directory)

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.reason is RestoreRefusal.LEFTOVER_JOURNAL
    assert str(caught.value) == (
        f"Refusing to restore: there is no database at {live_database}, but "
        "portfolio.db-journal lies beside its path. It is a rollback journal that belongs to "
        "the database that was there, and writing the restored file would make SQLite discard "
        "it. Nothing was changed. Move it first: beside the damaged file it belongs to, as "
        "portfolio.db.damaged-<stamp>-journal, or out of the data directory. Then restore again."
    )
    assert snapshot(live_database, backup_directory) == before


def test_a_leftover_journal_is_refused_before_the_name_is_read(
    live_database: Path, backup_directory: Path
) -> None:
    """Step 1, like the open database: the name is not even looked at."""
    live_database.unlink()
    backup._journal_path(live_database).write_bytes(b"pages")

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, "not a backup's name")

    assert caught.value.reason is RestoreRefusal.LEFTOVER_JOURNAL


def test_an_empty_journal_where_there_is_no_database_is_no_reason_to_refuse(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """A journal of zero bytes holds nothing, and SQLite does not treat it as hot either."""
    live_database.unlink()
    backup._journal_path(live_database).write_bytes(b"")

    result = restore(live_database, backup_directory, chosen)

    assert result.safety_copy is None
    assert table_contents(live_database) == table_contents(backup_directory / chosen)


def test_a_journal_beside_a_database_that_exists_is_left_to_sqlite(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """R16 is for a database that is gone. Beside one that exists, here a damaged one, the
    journal is SQLite's to recover or R15's to move, and the restore goes on."""
    damage_the_header(live_database)
    backup._journal_path(live_database).write_bytes(b"\x01" * 512)

    result = restore(live_database, backup_directory, chosen)

    assert result.damaged == live_database.with_name(DAMAGED_NAME)
    assert table_contents(live_database) == table_contents(backup_directory / chosen)


def test_an_open_database_is_refused_before_a_leftover_journal(
    live_database: Path, backup_directory: Path, chosen: str
) -> None:
    """A `-wal` means the application is running: that is the first thing to say."""
    live_database.unlink()
    backup._journal_path(live_database).write_bytes(b"pages")
    wal_path(live_database).write_bytes(b"")

    with pytest.raises(RestoreRefusedError) as caught:
        restore(live_database, backup_directory, chosen)

    assert caught.value.reason is RestoreRefusal.DATABASE_OPEN
