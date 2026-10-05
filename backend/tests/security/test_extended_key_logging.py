"""Spec 031, criterion 7: an extended key, and every address it derives, stay off stdout.

An extended public key is worse to leak than an address: it is every address the wallet has
used and every one it ever will. A scan reads dozens of derived addresses, each in a URL
path, through the same `httpx` client whose own request line carries that path. So these
tests run the whole thing -- the balance sync, the real Esplora provider, the real transport
-- with the production pipeline installed at DEBUG and `httpx`'s floor lifted, and read the
bytes that reached stdout. `tests/security/conftest.py` says why stdout and not
`capture_logs`.

Every absence is paired with a presence (`assert_carried_something`), because an absence in
a log that never ran passes.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from portfolio.logging import URL_LOGGING_LIBRARIES
from portfolio.repositories.sync_runs import SyncRunStatus, SyncTrigger
from portfolio.services.balance_sync import build_balance_sync_service
from tests.balance_harness import insert_user
from tests.extended_key_harness import (
    AddressBook,
    Holding,
    criterion_three_book,
    insert_key_wallet,
    provider_over,
)
from tests.extended_key_vectors import SCAN_KEY, SCAN_RECEIVE
from tests.security.conftest import assert_absent, assert_carried_something
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.repositories.sync_runs import SyncRunSummary
    from tests.security.conftest import ProductionLoggingInstaller

OBSERVED_AT: Final = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
SCAN_EVENT: Final = "balance_sync_extended_key_scanned"


async def sync_once(tmp_path: Path, book: AddressBook, *, rescan: bool = False) -> SyncRunSummary:
    """Plant one extended-key wallet and sync it over `book` -- twice when `rescan`."""
    async with migrated_sessionmaker(tmp_path) as factory:
        async with factory() as session:
            user_id = await insert_user(session)
            await insert_key_wallet(session, user_id=user_id, created_at=OBSERVED_AT)
        summary = await sync_over(factory, book)
        if rescan:
            summary = await sync_over(factory, book)
        return summary


async def sync_over(factory: async_sessionmaker[AsyncSession], book: AddressBook) -> SyncRunSummary:
    provider, client = provider_over(book, max_attempts=2)
    async with client, factory() as session:
        return await build_balance_sync_service(
            session,
            provider_for=lambda _chain_key: provider,
            clock=lambda: OBSERVED_AT,
            monotonic=lambda: 0,
        ).sync(SyncTrigger.MANUAL)


def lift_vendor_floors() -> None:
    """`httpx` and `httpcore` at DEBUG: the worst a misconfigured Pi could run with.

    `restored_logging` (through `production_logging`) puts their levels back afterwards.
    """
    for name in URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.DEBUG)


def json_lines(written: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in written.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


async def test_a_scan_at_debug_writes_neither_the_key_nor_a_derived_address(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    production_logging: ProductionLoggingInstaller,
) -> None:
    """Criterion 7, as the test plan words it: the sentinel-style scan with DEBUG on."""
    production_logging(log_level="DEBUG")
    lift_vendor_floors()
    book = criterion_three_book()
    # A retried address, so the transport's retry line is on stdout too.
    book.failures = {SCAN_RECEIVE[5]: [httpx.Response(503)]}

    summary = await sync_once(tmp_path, book, rescan=True)

    written = capsys.readouterr().out
    assert summary.status is SyncRunStatus.SUCCESS
    assert_carried_something(written, marker=SCAN_EVENT)
    assert_carried_something(written, marker="provider_request")
    assert_carried_something(written, marker="provider_request_retry")
    assert_carried_something(written, marker="HTTP Request: GET")
    assert len(set(book.asked)) == 69
    assert_absent(written, SCAN_KEY, *set(book.asked))
    assert SCAN_KEY[4:-4] not in written
    # With its floor lifted, `httpx` writes the request path; the value redaction is what
    # keeps the address out of it. Every path it wrote ends in the redaction, none in an
    # address. One line per address read: the retry happens inside the transport, below
    # the client that writes the line, so the 503 costs a request but not a line.
    paths = written.count("/api/address/")
    assert (paths, len(book.asked)) == (2 * 69, 2 * 69 + 1)
    assert written.count("/api/address/[REDACTED]") == paths


async def test_the_scan_line_on_stdout_carries_its_three_counts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    production_logging: ProductionLoggingInstaller,
) -> None:
    """Spec 031: the line "carries `wallet_id` and three counts" -- on the Pi, not only in a test.

    `capture_logs` replaces the processor chain, so it cannot see what the redaction does to
    a field. Rendered through the production chain, a count is only useful if it survives.
    """
    production_logging(log_level="INFO")

    await sync_once(tmp_path, criterion_three_book())

    written = capsys.readouterr().out
    assert_carried_something(written, marker=SCAN_EVENT)
    (record,) = [line for line in json_lines(written) if line.get("event") == SCAN_EVENT]
    # Exactly these keys: an added field is one more thing on the Pi's stdout to review.
    assert set(record) == {
        "event",
        "level",
        "timestamp",
        "wallet_id",
        "derived_scanned",
        "derived_new",
        "derived_newly_used",
    }, f"the line as written: {record}"
    assert (record["derived_scanned"], record["derived_new"], record["derived_newly_used"]) == (
        69,
        69,
        0,
    ), f"the line as written: {record}"


async def test_an_unused_key_at_debug_writes_no_derived_address_either(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    production_logging: ProductionLoggingInstaller,
) -> None:
    """Nothing used: forty addresses read, and the DEBUG scan line rather than the INFO one."""
    production_logging(log_level="DEBUG")
    lift_vendor_floors()
    book = AddressBook(holdings={SCAN_RECEIVE[0]: Holding()})

    await sync_once(tmp_path, book, rescan=True)

    written = capsys.readouterr().out
    assert_carried_something(written, marker=SCAN_EVENT)
    assert '"level": "debug"' in written
    assert_absent(written, SCAN_KEY, *set(book.asked))
