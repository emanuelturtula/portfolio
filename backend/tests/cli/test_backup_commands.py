"""Spec 029 (#22): `backup`, `list-backups` and `restore-backup`, as an operator runs them.

Called in process as `cli.main(argv)`, like the other commands here, against a real migrated
database file and a real backup directory under `tmp_path`. What is pinned is what the
operator reads -- each line's exact wording, the order, the exit code -- and that a failure
is **one line on stderr and exit 1**, never a traceback.

stdout also carries the log, because `cli.main` configures logging before it runs a command
and the development renderer writes to stdout. The command's own lines are picked out from
the log records rather than assumed to be the whole stream, for the reason
`test_refresh_prices.py` gives.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from portfolio import cli
from portfolio.config import get_settings
from portfolio.db.backup import take_copy, wal_path
from portfolio.domain.backups import backup_name, instant_of
from tests.backup_harness import (
    add_notes,
    copy_names,
    damage_the_header,
    migrated_database,
    plant_copies,
    row_counts,
    table_contents,
)
from tests.logging_harness import preserved_logging

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

LOG_LINE: Final = re.compile(r"^\S+Z \[\w+\s*\] ")
TOOK: Final = re.compile(r"^Took backup (portfolio-\d{8}T\d{12}Z\.sqlite3) \((\d+) bytes\)\.$")
ROTATED: Final = re.compile(r"^Rotation deleted (\d+) older backup\(s\): (.+)\.$")


@pytest.fixture(autouse=True)
def restored_logging() -> Iterator[None]:
    """`cli.main` reconfigures logging for the process; put back what was there."""
    with preserved_logging():
        yield


@pytest.fixture
def backups(cli_database: Path) -> Path:
    """The directory `cli_database` points `PORTFOLIO_BACKUP_DIR` at."""
    return cli_database.parent / "backups"


@pytest.fixture
def live(cli_database: Path) -> Path:
    """The command line's database, migrated, with two notes of the test's own."""
    migrated_database(cli_database)
    add_notes(cli_database, "first", "second")
    return cli_database


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str], list[str]]:
    """Exit code, the command's own stdout lines, and every stderr line."""
    capsys.readouterr()
    exit_code = cli.main(argv)
    captured = capsys.readouterr()
    out = [line for line in captured.out.splitlines() if line and not LOG_LINE.match(line)]
    return exit_code, out, captured.err.splitlines()


# --------------------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------------------


def test_backup_takes_a_copy_and_prints_its_name_and_size(
    live: Path, backups: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = datetime.now(UTC)

    exit_code, out, err = run(["backup"], capsys)

    assert (exit_code, err) == (0, [])
    assert len(out) == 1
    took = TOOK.match(out[0])
    assert took is not None, out
    name, size = took.group(1), int(took.group(2))
    assert copy_names(backups) == {name}
    assert (backups / name).stat().st_size == size
    started = instant_of(name)
    assert started is not None
    assert before <= started <= datetime.now(UTC)
    assert table_contents(backups / name) == table_contents(live)


def test_backup_says_which_older_copies_rotation_deleted(
    live: Path,
    backups: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One day and no weeks, so every copy from an earlier day goes."""
    monkeypatch.setenv("PORTFOLIO_BACKUP_KEEP_DAILY", "1")
    monkeypatch.setenv("PORTFOLIO_BACKUP_KEEP_WEEKLY", "0")
    get_settings.cache_clear()
    now = datetime.now(UTC)
    older = plant_copies(backups, [now - timedelta(days=3), now - timedelta(days=10)], b"old")
    (backups / "notes.txt").write_bytes(b"the operator's own file")

    exit_code, out, err = run(["backup"], capsys)

    assert (exit_code, err) == (0, [])
    assert len(out) == 2
    took = TOOK.match(out[0])
    rotated = ROTATED.match(out[1])
    assert took is not None
    assert rotated is not None, out
    assert rotated.group(1) == "2"
    assert set(rotated.group(2).split(", ")) == set(older)
    assert copy_names(backups) == {took.group(1), "notes.txt"}


def test_backup_works_with_the_timer_switched_off(
    live: Path, backups: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PORTFOLIO_BACKUP_ENABLED", "false")
    get_settings.cache_clear()

    exit_code, out, _ = run(["backup"], capsys)

    assert exit_code == 0
    assert TOOK.match(out[0])
    assert len(copy_names(backups)) == 1


def test_a_failed_backup_is_one_line_on_stderr_and_exit_1(
    cli_database: Path, backups: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No database at the configured path: the message names it, and nothing is kept."""
    cli_database.parent.mkdir(parents=True)

    exit_code, out, err = run(["backup"], capsys)

    assert exit_code == 1
    assert out == []
    assert err == [f"There is no database at {cli_database.resolve()} to copy."]
    assert copy_names(backups) == set()


def test_a_backup_directory_that_cannot_be_written_is_one_line_too(
    live: Path, backups: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backups.write_bytes(b"a file where the directory should be")

    exit_code, out, err = run(["backup"], capsys)

    assert exit_code == 1
    assert out == []
    assert len(err) == 1
    assert err[0].startswith(f"The backup directory {backups} cannot be prepared: ")


# --------------------------------------------------------------------------------------
# list-backups
# --------------------------------------------------------------------------------------


def test_list_backups_prints_each_copy_newest_first(
    backups: Path, cli_database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del cli_database
    oldest = datetime(2026, 9, 30, 3, 0, 0, 1, tzinfo=UTC)
    newest = datetime(2026, 10, 2, 9, 15, 0, 654321, tzinfo=UTC)
    middle = datetime(2026, 10, 1, 3, 0, 0, 123456, tzinfo=UTC)
    plant_copies(backups, [oldest], b"1")
    plant_copies(backups, [newest], b"1234")
    plant_copies(backups, [middle], b"12")
    (backups / "notes.txt").write_bytes(b"not a copy")

    exit_code, out, err = run(["list-backups"], capsys)

    assert (exit_code, err) == (0, [])
    assert out == [
        "portfolio-20261002T091500654321Z.sqlite3  2026-10-02T09:15:00.654321Z  4 bytes",
        "portfolio-20261001T030000123456Z.sqlite3  2026-10-01T03:00:00.123456Z  2 bytes",
        "portfolio-20260930T030000000001Z.sqlite3  2026-09-30T03:00:00.000001Z  1 bytes",
    ]


def test_list_backups_with_none_names_the_directory(
    backups: Path, cli_database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del cli_database

    exit_code, out, err = run(["list-backups"], capsys)

    assert (exit_code, err) == (0, [])
    assert out == [f"No backups in {backups}."]


def test_a_directory_that_cannot_be_listed_is_one_line_on_stderr(
    backups: Path, cli_database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del cli_database
    backups.parent.mkdir(parents=True)
    backups.write_bytes(b"a file where the directory should be")

    exit_code, out, err = run(["list-backups"], capsys)

    assert exit_code == 1
    assert out == []
    assert len(err) == 1
    assert err[0].startswith(f"The backup directory {backups} cannot be read: ")


def test_the_utc_text_is_the_apis_form_to_the_microsecond() -> None:
    assert (
        cli.utc_text(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)) == "2026-01-02T03:04:05.000006Z"
    )


# --------------------------------------------------------------------------------------
# restore-backup
# --------------------------------------------------------------------------------------


@pytest.fixture
def chosen(live: Path, backups: Path) -> str:
    """A copy with the two notes, after which the live database gained a third."""
    name = take_copy(live, backups, datetime(2026, 10, 1, 3, tzinfo=UTC)).name
    add_notes(live, "third, after the copy")
    return name


def test_restore_backup_prints_both_copies_and_the_rows_per_table(
    live: Path, backups: Path, chosen: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = table_contents(live)

    exit_code, out, err = run(["restore-backup", chosen], capsys)

    assert (exit_code, err) == (0, [])
    safety = sorted(copy_names(backups) - {chosen})
    assert len(safety) == 1
    counts = row_counts(backups / chosen)
    assert out == [
        f"Restored {chosen}.",
        f"The database as it was before is in the safety copy {safety[0]}.",
        "Rows per table after the restore:",
        *[f"  {table}: {rows}" for table, rows in counts.items()],
    ]
    assert "  notes: 2" in out
    assert table_contents(live) == table_contents(backups / chosen)
    assert table_contents(backups / safety[0]) == before


def test_restore_backup_onto_an_empty_data_volume_says_no_safety_copy_was_taken(
    live: Path, backups: Path, chosen: str, capsys: pytest.CaptureFixture[str]
) -> None:
    live.unlink()

    exit_code, out, err = run(["restore-backup", chosen], capsys)

    assert (exit_code, err) == (0, [])
    assert out[:3] == [
        f"Restored {chosen}.",
        "There was no database to copy first, so no safety copy was taken.",
        "Rows per table after the restore:",
    ]
    assert copy_names(backups) == {chosen}


def test_restore_backup_over_a_damaged_database_says_where_it_was_moved(
    live: Path, backups: Path, chosen: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """R3: the operator is told where the owner's data went, and what to do with it."""
    damage_the_header(live)
    damaged_bytes = live.read_bytes()

    exit_code, out, err = run(["restore-backup", chosen], capsys)

    assert (exit_code, err) == (0, [])
    (moved,) = [entry for entry in live.parent.iterdir() if ".damaged-" in entry.name]
    assert re.fullmatch(r"portfolio\.db\.damaged-\d{8}T\d{12}Z", moved.name)
    assert moved.read_bytes() == damaged_bytes
    assert out[:3] == [
        f"Restored {chosen}.",
        "The live database opened but did not pass its own check, so no safety copy was "
        f"taken: it was moved aside to {moved.resolve()}. Keep it until the restore is "
        "checked, then delete it.",
        "Rows per table after the restore:",
    ]
    assert copy_names(backups) == {chosen}
    assert table_contents(live) == table_contents(backups / chosen)


@pytest.mark.parametrize(
    ("argument", "message"),
    [
        (
            "portfolio.db",
            "Refusing to restore: 'portfolio.db' is not the name of a backup. A backup is named "
            "portfolio-YYYYMMDDTHHMMSSffffffZ.sqlite3, and list-backups shows them.",
        ),
        (
            backup_name(datetime(2020, 1, 1, tzinfo=UTC)),
            "Refusing to restore: there is no backup named "
            "portfolio-20200101T000000000000Z.sqlite3 in {backups}. list-backups shows the ones "
            "there are.",
        ),
    ],
    ids=["not a name", "no such copy"],
)
def test_a_refused_restore_is_one_line_on_stderr_and_exit_1(
    live: Path,
    backups: Path,
    chosen: str,
    capsys: pytest.CaptureFixture[str],
    argument: str,
    message: str,
) -> None:
    before = table_contents(live)
    names = copy_names(backups)

    exit_code, out, err = run(["restore-backup", argument], capsys)

    assert exit_code == 1
    assert out == []
    assert err == [message.format(backups=backups)]
    assert table_contents(live) == before
    assert copy_names(backups) == names
    del chosen


def test_a_restore_while_the_database_is_open_is_refused_on_one_line(
    live: Path, backups: Path, chosen: str, capsys: pytest.CaptureFixture[str]
) -> None:
    wal_path(live).write_bytes(b"")
    names = copy_names(backups)

    exit_code, out, err = run(["restore-backup", chosen], capsys)

    assert exit_code == 1
    assert out == []
    assert len(err) == 1
    assert err[0].startswith(
        f"Refusing to restore: {wal_path(live)} exists, so the database is open."
    )
    assert copy_names(backups) == names


def test_a_copy_that_is_not_self_contained_is_refused_on_one_line(
    live: Path, backups: Path, chosen: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """R13, as the operator sees it: one line that says what to do, and nothing changed."""
    wal_path(backups / chosen).write_bytes(b"frames brought from elsewhere")
    before = table_contents(live)
    names = copy_names(backups)

    exit_code, out, err = run(["restore-backup", chosen], capsys)

    assert exit_code == 1
    assert out == []
    assert len(err) == 1
    assert err[0].startswith(f"Refusing to restore: {chosen} is not a self-contained copy: ")
    assert "open the copy there once with sqlite3" in err[0]
    assert table_contents(live) == before
    assert copy_names(backups) == names


def test_a_leftover_journal_is_refused_on_one_line(
    live: Path, backups: Path, chosen: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """R16, as the operator sees it: the journal stays, and no database is written."""
    live.unlink()
    journal = live.with_name(f"{live.name}-journal")
    journal.write_bytes(b"pages of a database that is gone")
    names = copy_names(backups)

    exit_code, out, err = run(["restore-backup", chosen], capsys)

    assert exit_code == 1
    assert out == []
    assert len(err) == 1
    assert err[0].startswith(
        f"Refusing to restore: there is no database at {live}, but {journal.name} lies beside "
        "its path."
    )
    assert not live.exists()
    assert journal.read_bytes() == b"pages of a database that is gone"
    assert copy_names(backups) == names


def test_restore_backup_needs_a_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["restore-backup"])

    assert exited.value.code == 2
    assert "name" in capsys.readouterr().err


def test_the_three_commands_are_on_the_parser() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["backup"]).handler is cli.take_backup
    assert parser.parse_args(["list-backups"]).handler is cli.list_backups
    restore = parser.parse_args(["restore-backup", "portfolio-x.sqlite3"])
    assert restore.handler is cli.restore_backup
    assert restore.name == "portfolio-x.sqlite3"
