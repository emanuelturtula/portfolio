"""#104 and rule 3: what a venue holds reaches one response, and no log line or other column.

Spec 025: *no log line, column or response carries an asset name or an amount, except the
reconciliation endpoint itself.* A balance is the owner's holdings. The log may say which
venue was read, how many assets came back, and what kind of failure a read was.

Two drives, both through the **production JSON logging**, at the production level and at
DEBUG, the noisiest the application can be made:

* **The real application.** A manual sync against simulated venues whose balances are marked
  -- a successful read, a read the venue fails, an answer that names an asset twice, and an
  answer the *table* refuses, whose traceback is logged -- then the reconciliation endpoint,
  signed in and anonymous. Every byte on stdout and every standard-library record is
  searched for the markers, and so is every column of every table but the one that stores
  balances, and every other response the sync's endpoints give.
* **The sync service alone**, once per failure class, for the same search.

Each has its positive companions first, so an empty capture cannot pass: the events that
must be there are named, and the markers are shown to be in the one response allowed to
carry them.

The markers are synthetic: an asset called `ZZMARKED` and two amounts of eighteen digits,
`424242.424242424242` and `737373.737373737373`. Nothing here is anybody's holdings.

**The amounts are long on purpose.** A log line is full of digits that are not amounts --
timestamps, `created` and `relativeCreated` floats, thread ids, object addresses -- and a
four-digit run turns up among them by chance: a first version of this module searched for
`4242` and failed about one run in six on a timestamp. Each amount is searched for as its
first eleven characters with the point, and as a twelve-digit run without it, which no clock
reading contains.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.domain.exchanges import ExchangeKey
from portfolio.main import create_app
from portfolio.providers.exchanges.errors import (
    ExchangeAuthError,
    ExchangeInsufficientScopeError,
    ExchangeInvalidRequestError,
    ExchangeRateLimitedError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
)
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in
from tests.balance_harness import insert_user
from tests.exchange_sync_harness import (
    SimulatedVenue,
    always_balances,
    held,
    make_fill,
)
from tests.security.conftest import EveryRecord, assert_carried_something
from tests.services.test_exchange_sync import Harness, nine_fills
from tests.services.test_exchange_sync_balances import RawBalance, UnassembledVenue
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.security.conftest import ProductionLoggingInstaller

RECONCILIATION: Final = "/api/accounting/reconciliation"

MARKED_ASSET: Final = "ZZMARKED"
MARKED_AMOUNT: Final = "424242.424242424242"
OTHER_MARKED_AMOUNT: Final = "737373.737373737373"
#: What is searched for. A prefix with the point and a digit run without it, so any spelling
#: of either amount is caught: as given, padded to eighteen places, inside `Decimal('...')`,
#: or scaled to an integer of base units.
SENTINELS: Final = (
    MARKED_ASSET,
    MARKED_ASSET.lower(),
    "424242.4242",
    "424242424242",
    "737373.7373",
    "737373737373",
)

#: Every table but `exchange_balances`, the one place a balance is stored.
OTHER_TABLES_SQL: Final = (
    "SELECT name FROM sqlite_master WHERE type = 'table' "
    "AND name NOT IN ('exchange_balances') ORDER BY name"
)


#: The production default, and the level an operator turns on to see what is happening.
LOG_LEVELS: Final = ("INFO", "DEBUG")


async def until(condition: Callable[[], bool]) -> None:
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


def leaks(searched: str) -> list[str]:
    """Each sentinel found, with the line it was found on."""
    found = []
    for sentinel in SENTINELS:
        if sentinel in searched:
            line = next(one for one in searched.splitlines() if sentinel in one)
            found.append(f"{sentinel}: {line[:300]}")
    return found


def marked_balances() -> tuple[Any, ...]:
    return (held(MARKED_ASSET, MARKED_AMOUNT), held("BTC", OTHER_MARKED_AMOUNT))


def test_the_search_finds_each_marker_however_it_is_spelled() -> None:
    """The control for every absence below."""
    assert leaks('{"event": "exchange_balances_read", "asset": "ZZMARKED"}')
    assert leaks("quantity=Decimal('424242.424242424242')")
    assert leaks("[parameters: (1, 'BTC', '737373.737373737373000000')]")
    assert leaks("confirmed=424242424242424242")
    assert leaks('"timestamp": "2026-10-01T19:50:42.424242Z", "created": 1791234242.7373') == []
    assert leaks("coin zzmarked is not recognised")
    assert leaks('{"event": "exchange_balances_read", "assets": 2}') == []


async def dump_other_tables(factory: Any) -> str:
    """Every column of every row outside `exchange_balances`, as one string."""
    dumped: list[str] = []
    async with factory() as session:
        names = [str(name) for (name,) in await session.execute(text(OTHER_TABLES_SQL))]
        for name in names:
            # A table name read from `sqlite_master`, never input.
            rows = await session.execute(text(f'SELECT * FROM "{name}"'))  # noqa: S608
            dumped.extend(f"{name}: {dict(row)!r}" for row in rows.mappings().all())
    return "\n".join(dumped)


@pytest.mark.parametrize("log_level", LOG_LEVELS)
async def test_no_asset_or_amount_of_a_balance_reaches_a_log_a_column_or_another_response(
    api_environment: Path,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    log_level: str,
) -> None:
    """The real application: four balance reads that go three different ways."""
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    bitget = SimulatedVenue(
        [make_fill(5001 + n, now - timedelta(minutes=10 - n), quantity="0.5") for n in range(4)],
        balances=marked_balances(),
    )
    bingx = SimulatedVenue(
        exchange_key=ExchangeKey.BINGX,
        balances=marked_balances(),
        balance_fault=always_balances(ExchangeUnavailableError(status=503)),
    )
    venues = MappingProxyType({ExchangeKey.BITGET: bitget, ExchangeKey.BINGX: bingx})
    monkeypatch.setattr("portfolio.main.exchange_providers", lambda client, **_: venues)
    app = create_app()
    production_logging(log_level)
    records = EveryRecord()
    logging.getLogger().addHandler(records)

    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client,
        ):
            task: asyncio.Task[Any] = app.state.accounting_startup_task
            await asyncio.wait_for(until(task.done), timeout=5)
            await sign_in(client)
            capsys.readouterr()
            records.rendered.clear()

            # 1. Bitget's read succeeds; BingX's fails `unavailable`.
            first = await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
            # 2. BingX answers with an asset named twice: refused by `assemble_balances`.
            bingx.balance_fault = None
            bingx.balances = [
                held(MARKED_ASSET, MARKED_AMOUNT),
                held(MARKED_ASSET, OTHER_MARKED_AMOUNT),
            ]
            second = await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
            served = await client.get(RECONCILIATION)
            listing = await client.get("/api/exchanges")
            runs = await client.get("/api/exchanges/runs")
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as anonymous:
                refused = await anonymous.get(RECONCILIATION)
            tables = await dump_other_tables(app.state.db_sessionmaker)
            written = capsys.readouterr().out
            searched = "\n".join([written, *records.rendered])
    finally:
        logging.getLogger().removeHandler(records)

    # The controls: the syncs ran, the balances were stored and served, the refusal happened.
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["status"] == second.json()["status"] == "success"
    assert (bitget.balance_calls, bingx.balance_calls) == (2, 2)
    assert served.status_code == 200
    assert MARKED_ASSET in served.text, "the one response allowed to carry it does"
    assert "424242.424242424242000000" in served.text
    assert "737373.737373737373000000" in served.text
    kinds = {entry["exchange_key"]: entry["balances_error"] for entry in served.json()["exchanges"]}
    assert kinds == {"bitget": None, "bingx": "schema"}
    assert refused.status_code == 401
    # The positive companions: the capture is the log that ran, and each event is in it.
    assert_carried_something(written, marker="exchange_balances_read")
    assert '"event": "exchange_balances_read_failed"' in written
    assert '"error_kind": "unavailable"' in written
    assert '"error_kind": "schema"' in written
    assert '"assets": 2' in written, "the count is logged, and only the count"
    assert "request_refused" in written
    assert tables.count("exchange_accounts:") == 2, "the dump read the tables"
    assert "'balances_error': 'schema'" in tables
    # The claim, three ways: no log line, no other column, no other response.
    assert leaks(searched) == []
    assert leaks(tables) == []
    assert leaks("\n".join([first.text, second.text, listing.text, runs.text, refused.text])) == []


FAILURES: Final = [
    pytest.param(ExchangeAuthError(status=401), "auth", id="auth"),
    pytest.param(ExchangeInsufficientScopeError(status=403), "insufficient_scope", id="scope"),
    pytest.param(
        ExchangeInvalidRequestError(status=400, venue_code="40017"),
        "invalid_request",
        id="invalid request",
    ),
    pytest.param(ExchangeUnavailableError(status=503), "unavailable", id="unavailable"),
    pytest.param(ExchangeSchemaError("available must not be negative"), "schema", id="schema"),
    pytest.param(
        ExchangeRateLimitedError(status=429, venue_code="429", retry_after_ms=1000),
        "rate_limited",
        id="rate limited",
    ),
]


@pytest.mark.parametrize("log_level", LOG_LEVELS)
@pytest.mark.parametrize(("error", "kind"), FAILURES)
async def test_a_failed_balance_read_logs_its_kind_and_nothing_of_the_balances(
    tmp_path: Path,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    kind: str,
    log_level: str,
) -> None:
    """Each class of failure, with a good reading already stored that the log must not echo."""
    async with migrated_sessionmaker(tmp_path) as factory:
        async with factory() as session:
            await insert_user(session)
        harness = Harness()
        await harness.run(factory, SimulatedVenue(nine_fills(), balances=marked_balances()))
        harness.clock.advance(timedelta(minutes=15))
        venue = SimulatedVenue(
            nine_fills(), balances=marked_balances(), balance_fault=always_balances(error)
        )
        production_logging(log_level)
        records = EveryRecord()
        logging.getLogger().addHandler(records)
        try:
            capsys.readouterr()
            await harness.run(factory, venue)
            written = capsys.readouterr().out
            searched = "\n".join([written, *records.rendered])
        finally:
            logging.getLogger().removeHandler(records)

    assert_carried_something(written, marker="exchange_balances_read_failed")
    assert f'"error_kind": "{kind}"' in written
    assert f'"error_type": "{type(error).__name__}"' in written
    assert leaks(searched) == []


@pytest.mark.parametrize("log_level", LOG_LEVELS)
async def test_a_reading_the_table_refuses_logs_a_traceback_without_the_values(
    tmp_path: Path,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
    log_level: str,
) -> None:
    """Our own failure while storing the answer: `internal`, logged with its traceback.

    The statement that failed is an `INSERT` whose parameters are the asset and the amount.
    The engine is built with `hide_parameters=True`, which is the one thing standing between
    that traceback and the owner's balances; this is the test that says so.
    """
    async with migrated_sessionmaker(tmp_path) as factory:
        async with factory() as session:
            await insert_user(session)
        harness = Harness()
        venue = UnassembledVenue(nine_fills())
        venue.raw = (
            RawBalance(MARKED_ASSET, MARKED_AMOUNT),
            RawBalance(MARKED_ASSET, OTHER_MARKED_AMOUNT),
        )
        production_logging(log_level)
        records = EveryRecord()
        logging.getLogger().addHandler(records)
        try:
            capsys.readouterr()
            await harness.run(factory, venue)
            written = capsys.readouterr().out
            searched = "\n".join([written, *records.rendered])
        finally:
            logging.getLogger().removeHandler(records)

    assert_carried_something(written, marker="exchange_balances_read_failed")
    assert '"error_kind": "internal"' in written
    assert '"error_type": "IntegrityError"' in written
    assert "Traceback" in written, "the traceback is what is being searched"
    assert "UNIQUE constraint failed" in written
    assert "exchange_balances" in written, "the failed statement is named"
    assert leaks(searched) == []
