"""Spec 029 (#22), criterion 8 and rule 3: a copy, a failed copy and a restore log no row.

A copy of the database holds the owner's complete financial data and the account's password
hash. The service logs `backup_completed` and `backup_failed` with a name, sizes, a duration,
a kind and a class name -- **never a row and never an error's message**, because a message is
free text and the rule is the one every line here follows. A restore logs nothing; its row
counts go to the operator's terminal and nowhere else.

This drives a successful copy, a copy that fails its check, and a restore through
`cli.main`, with the production JSON renderer, and searches every byte on stdout and every
standard-library record for a sentinel stored in the database. The positive companions come
first, so an empty capture cannot pass.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final

import pytest

from portfolio import cli
from portfolio.config import Settings, get_settings
from portfolio.db.backup import take_copy
from portfolio.domain.backups import BackupErrorKind
from portfolio.logging import configure_logging
from portfolio.services.backup import BackupError, BackupService
from tests.backup_harness import (
    ROW_SENTINEL,
    T0,
    add_notes,
    corrupt_with_orphan_pages,
    fixed,
    sqlite_url,
    table_contents,
)
from tests.security.conftest import EveryRecord, assert_carried_something

if TYPE_CHECKING:
    from pathlib import Path

    from tests.security.conftest import ProductionLoggingInstaller

#: Sentinels for the two kinds of thing a copy carries that a log must not: a row's value,
#: and the free text of an error's message.
HASH_SENTINEL: Final = "$argon2id$v=19$m=" + "Qx7" * 6


def json_events(output: str) -> list[dict[str, Any]]:
    """Every JSON record on stdout, parsed. A line that is not JSON is not a record."""
    events = []
    for line in output.splitlines():
        if line.startswith("{"):
            events.append(json.loads(line))
    return events


def leaks(searched: str, *sentinels: str) -> list[str]:
    found = []
    for sentinel in sentinels:
        if sentinel in searched:
            line = next(one for one in searched.splitlines() if sentinel in one)
            found.append(f"{sentinel[:24]}: {line[:300]}")
    return found


@pytest.fixture
def sensitive_database(live_database: Path) -> Path:
    """The live database with a note and a stand-in password hash only a leak would show."""
    add_notes(live_database, ROW_SENTINEL, HASH_SENTINEL)
    # The search below is only worth something if the sentinels are really in the file.
    stored = " ".join(table_contents(live_database)["notes"])
    assert ROW_SENTINEL in stored
    assert HASH_SENTINEL in stored
    return live_database


def service_over(database: Path, directory: Path) -> BackupService:
    return BackupService(
        database_url=sqlite_url(database),
        directory=directory,
        enabled=True,
        interval_minutes=1440,
        keep_daily=7,
        keep_weekly=4,
        clock=fixed(T0),
    )


async def test_a_copy_and_a_failed_copy_log_their_fields_and_no_row_or_message(
    sensitive_database: Path,
    backup_directory: Path,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = service_over(sensitive_database, backup_directory)
    production_logging()
    records = EveryRecord()
    logging.getLogger().addHandler(records)
    try:
        capsys.readouterr()
        taken = await service.take()
        succeeded = capsys.readouterr().out
        corrupt_with_orphan_pages(sensitive_database)
        with pytest.raises(BackupError) as caught:
            await service.take()
        failed = capsys.readouterr().out
    finally:
        logging.getLogger().removeHandler(records)

    # The positive companions: both lines are there, with exactly their fields.
    assert_carried_something(succeeded, marker="backup_completed")
    assert_carried_something(failed, marker="backup_failed")
    (completed,) = [
        event for event in json_events(succeeded) if event["event"] == "backup_completed"
    ]
    assert {key: completed[key] for key in ("name", "bytes", "deleted")} == {
        "name": taken.name,
        "bytes": taken.size_bytes,
        "deleted": 0,
    }
    assert isinstance(completed["duration_ms"], int)
    (failure,) = [event for event in json_events(failed) if event["event"] == "backup_failed"]
    assert failure["error_kind"] == BackupErrorKind.INTEGRITY_FAILED.value
    assert failure["error_type"] == "CheckFailedError"
    assert failure["level"] == "error"
    # The claim: no row, and not the message the operator's command would print.
    searched = "\n".join([succeeded, failed, *records.rendered])
    assert leaks(searched, ROW_SENTINEL, HASH_SENTINEL, str(caught.value)) == []
    assert "did not pass its check" not in searched


def test_a_restore_logs_no_row_and_its_counts_reach_only_the_terminal(
    sensitive_database: Path,
    backup_directory: Path,
    restored_logging: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through `cli.main`, which configures logging itself: the witness is added after it."""
    del restored_logging  # The fixture's value is its teardown.
    name = take_copy(sensitive_database, backup_directory, T0).name
    add_notes(sensitive_database, "written after the copy")
    monkeypatch.setenv("PORTFOLIO_DATABASE_URL", sqlite_url(sensitive_database))
    monkeypatch.setenv("PORTFOLIO_BACKUP_DIR", str(backup_directory))
    monkeypatch.setenv("PORTFOLIO_LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()
    records = EveryRecord()

    def configure_then_witness(settings: Settings) -> None:
        configure_logging(settings)
        logging.getLogger().addHandler(records)

    monkeypatch.setattr("portfolio.cli.configure_logging", configure_then_witness)
    try:
        capsys.readouterr()
        exit_code = cli.main(["restore-backup", name])
        output = capsys.readouterr().out
    finally:
        logging.getLogger().removeHandler(records)
        get_settings.cache_clear()

    assert exit_code == 0
    # The positive companion: the counts were printed, to the terminal.
    assert "Rows per table after the restore:" in output
    assert "  notes: 2" in output
    # The claim: no record carries them, or a row.
    rendered = "\n".join(records.rendered)
    assert "Rows per table" not in rendered
    assert "notes: 2" not in rendered
    assert leaks(rendered, ROW_SENTINEL, HASH_SENTINEL) == []
    assert leaks(output, ROW_SENTINEL, HASH_SENTINEL) == []
