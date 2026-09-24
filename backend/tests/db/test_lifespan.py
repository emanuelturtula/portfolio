"""Application startup owns the schema and the engine.

This is the spec's addition to the issue: without it the production container comes up
against an empty database file. It is tested here so that removing it, if that is ever
the right call, is a visible revert rather than a silent regression.

`get_settings` is `lru_cache`d and `Settings` also reads a `.env` file, so the cache is
cleared on both sides of the environment override. Skipping the second clear would leave
a temporary database URL cached for every test that runs afterwards in the session.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from anyio import Path as AsyncPath
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from portfolio.config import get_settings
from portfolio.db.engine import create_database_engine, create_session_factory
from portfolio.db.models import Asset
from portfolio.domain.chains import ChainKey
from portfolio.main import create_app
from portfolio.providers.errors import ProviderUnavailableError
from portfolio.providers.prices.base import SUPPORTED_PAIRS, PriceQuote, PriceSource
from portfolio.services.sync_coordinator import SyncCoordinator
from tests.balance_harness import (
    DEFAULT_BITCOIN_ADDRESS,
    SYNC_RUNS_SQL,
    StubChainProvider,
    insert_user,
    insert_wallet,
    rows_of,
    stub_chain_providers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.pool import ConnectionPoolEntry

    from portfolio.providers.prices.base import PricePair
    from portfolio.services.scheduler import IntervalScheduler

EXPECTED_SEED_SYMBOLS = ["BTC", "KAS", "USDT"]


@pytest.fixture
def lifespan_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the process-wide settings at a temporary file, and put them back.

    Both schedules are off here for the reason `tests/auth/conftest.py` sets out at length:
    a lifespan with either loop running reaches a vendor at startup, because a database
    created a moment ago has no finished run and no cached price to suppress it. The tests
    below that are about a scheduler turn the one they need back on by name.
    """
    database_path = tmp_path / "lifespan" / "portfolio.db"
    monkeypatch.setenv(
        "PORTFOLIO_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_PRICE_REFRESH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        yield database_path
    finally:
        # Cleared again so the cached temporary URL cannot leak into a later test. The
        # environment variable itself is undone by `monkeypatch` after this.
        get_settings.cache_clear()


async def test_lifespan_migrates_and_exposes_a_session_factory(
    lifespan_database: Path,
) -> None:
    """Entering the lifespan brings up the schema and publishes the engine."""
    # The parent directory does not exist yet: `ensure_database_directory` has to create
    # it, or SQLite fails with an error that says nothing about a missing directory.
    database_file = AsyncPath(lifespan_database)
    assert not await database_file.parent.exists()
    app = create_app()

    closed: list[DBAPIConnection] = []

    def record_close(
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
    ) -> None:
        del connection_record
        closed.append(dbapi_connection)

    async with app.router.lifespan_context(app):
        engine = app.state.db_engine
        sessionmaker = app.state.db_sessionmaker
        assert isinstance(engine, AsyncEngine)
        assert isinstance(sessionmaker, async_sessionmaker)
        event.listen(engine.sync_engine, "close", record_close)

        assert await database_file.is_file()

        async with sessionmaker() as session:
            symbols = list(await session.scalars(select(Asset.symbol).order_by(Asset.symbol)))
            # The pragmas reach the application's own sessions, not only the tests'.
            foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))
            journal_mode = await session.scalar(text("PRAGMA journal_mode"))

    assert symbols == EXPECTED_SEED_SYMBOLS
    assert foreign_keys == 1
    assert journal_mode == "wal"
    # Leaving the lifespan disposes the engine; a pool left open leaks a file handle and
    # keeps the WAL from being checkpointed on shutdown.
    assert closed


async def test_lifespan_is_idempotent_across_two_startups(lifespan_database: Path) -> None:
    """A restart re-runs `upgrade head` against a database that is already at head."""
    for _ in range(2):
        app = create_app()
        async with app.router.lifespan_context(app):
            sessionmaker = app.state.db_sessionmaker
            async with sessionmaker() as session:
                symbols = list(await session.scalars(select(Asset.symbol).order_by(Asset.symbol)))
        assert symbols == EXPECTED_SEED_SYMBOLS

    assert await AsyncPath(lifespan_database).is_file()


async def test_two_applications_do_not_share_a_pool(lifespan_database: Path) -> None:
    """The engine lives on `app.state` rather than in a module global for this reason."""
    assert not await AsyncPath(lifespan_database).exists()
    first = create_app()
    second = create_app()

    async with first.router.lifespan_context(first), second.router.lifespan_context(second):
        assert first.state.db_engine is not second.state.db_engine
        assert first.state.db_sessionmaker is not second.state.db_sessionmaker


# --------------------------------------------------------------------------------------
# Criterion 5 of #10: the client, the sweep and the scheduler all belong to the lifespan
# --------------------------------------------------------------------------------------


@pytest.fixture
def scheduled_lifespan(
    lifespan_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """The same environment with the balance schedule **on**, and a shutdown that does not wait.

    Every test below that turns the loop on also stubs the provider registry, so nothing
    here reaches a network. The grace is zero so that the "a run outlived the grace" branch
    is reachable without the suite waiting ten seconds for it; the test that needs a real
    grace sets one of its own.
    """
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "true")
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS", "0")
    get_settings.cache_clear()
    try:
        yield lifespan_database
    finally:
        get_settings.cache_clear()


async def bring_the_schema_up(database: Path) -> None:
    """Run one lifespan for its migration and nothing else, then let it go.

    The schema does not exist until a lifespan has run, so a test that needs a row in place
    *before* the lifespan it is measuring has to bring the file up first. This entry has the
    schedule off, which is the shared default, so it reads nothing and costs nothing.
    """
    app = create_app()
    async with app.router.lifespan_context(app):
        pass
    assert await AsyncPath(database).is_file()


@asynccontextmanager
async def own_session(database: Path) -> AsyncIterator[AsyncSession]:
    """A session over the same file, on an engine this test owns.

    Writing through the application's own factory is not an option for these fixtures: the
    lifespan sweeps orphaned runs on the way **out** as well as on the way in, so a
    `running` row planted inside a lifespan is swept by that same lifespan before the test
    can measure anything.
    """
    engine = create_database_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def register_a_wallet(database: Path) -> None:
    """One owner and one active Bitcoin wallet, so a startup sync has something to read."""
    await bring_the_schema_up(database)
    async with own_session(database) as session:
        user_id = await insert_user(session)
        await insert_wallet(
            session,
            user_id=user_id,
            chain_key=ChainKey.BITCOIN,
            address=DEFAULT_BITCOIN_ADDRESS,
        )


async def runs_in(database: Path) -> list[dict[str, object]]:
    """Every `sync_runs` row, read through a connection of this test's own."""
    async with own_session(database) as session:
        return await rows_of(session, SYNC_RUNS_SQL)


def is_running(scheduler: IntervalScheduler) -> bool:
    """Read `running` afresh, through a call `mypy` cannot narrow across the lifespan's exit.

    Inline, the first `is True` narrows the property to a literal and the later `is False`
    reads as unreachable -- which it is not: leaving the lifespan is what changes it.
    """
    return scheduler.running


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until `condition` holds. Bounded by the caller's `wait_for`.

    `asyncio.sleep(0)` is a bare checkpoint, not a pause: it hands control to the scheduler
    task and takes it straight back, so this spins the loop rather than waiting on it. The
    condition is set deep inside a lifespan's own task and has no event to hang an
    `asyncio.Event` off without reaching into the application to plant one -- which would be
    a seam in production code that exists only for a test. `noqa: ASYNC110` for that reason;
    every caller wraps this in `asyncio.wait_for`, so a condition that never holds fails
    with a timeout rather than hanging the suite.
    """
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0)


async def test_the_http_client_is_closed_on_shutdown(
    lifespan_database: Path,
) -> None:
    """The wiring #6 through #9 each deferred to #10, and the half that gets forgotten.

    The client is process-wide *by construction*: the per-host rate limiter's state lives on
    its transport, so a second client would silently halve the interval it claims to
    enforce. Closing it is what releases the connection pool; a lifespan that built one and
    never closed it would leak a socket per restart on a machine expected to run for months.
    """
    del lifespan_database
    app = create_app()

    async with app.router.lifespan_context(app):
        client = app.state.http_client
        assert client.is_closed is False

    assert client.is_closed is True


async def test_the_sync_coordinator_is_published_for_the_router(
    lifespan_database: Path,
) -> None:
    """`get_sync_coordinator` reads `app.state`, so the lifespan has to put one there.

    It is process-wide rather than per-request on purpose: a run outlives the request that
    started it, because a second caller joins rather than starting its own.
    """
    del lifespan_database
    app = create_app()

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.sync_coordinator, SyncCoordinator)
        assert app.state.sync_coordinator.in_flight is False


async def test_the_scheduler_is_not_started_when_disabled(lifespan_database: Path) -> None:
    """The operator's off switch, asserted as "no task" rather than as "no tick yet".

    A scheduler that was built and then never ticked would look the same from outside for
    fifteen minutes, which is longer than any test will wait -- so the assertion is on the
    absence of the object, and on the table having stayed empty.
    """
    app = create_app()

    async with app.router.lifespan_context(app):
        assert app.state.balance_scheduler is None

    assert await runs_in(lifespan_database) == [], "a disabled schedule performs no run at all"


async def test_the_scheduler_starts_and_stops_with_the_application(
    scheduled_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 5: the task exists inside the lifespan and is gone once it has exited.

    Asserted on `running` at both moments rather than only after, because a property that is
    always false satisfies a check made at the end alone. The startup run is asserted too:
    it is the first tick, it is what proves the loop actually ran rather than merely
    existing, and its trigger says `startup` so an operator can tell it from the schedule.
    """
    # A real grace, so the startup tick is allowed to finish on the way out; the zero the
    # fixture sets belongs to the test below, which is about a run that does *not* finish.
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS", "5")
    get_settings.cache_clear()
    await register_a_wallet(scheduled_lifespan)
    provider = StubChainProvider(ChainKey.BITCOIN, {DEFAULT_BITCOIN_ADDRESS: 123_456_789})
    registry = stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: provider})
    app = create_app()

    async with app.router.lifespan_context(app):
        scheduler = app.state.balance_scheduler
        assert scheduler is not None
        assert is_running(scheduler) is True
        # Let the startup tick get through the coordinator and the provider.
        await asyncio.wait_for(until(lambda: bool(provider.calls)), timeout=5)

    assert is_running(scheduler) is False
    assert registry.created == ["bitcoin"]
    runs = await runs_in(scheduled_lifespan)
    assert [row["trigger"] for row in runs] == ["startup"]
    assert runs[0]["status"] == "success"


async def test_shutdown_waits_for_a_run_in_flight(
    scheduled_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grace period, seen as a row that finished rather than as elapsed time.

    The provider suspends several times before answering, so the startup run is genuinely
    mid-flight when the lifespan starts unwinding. With a grace it is allowed to finish, and
    the proof is the `success` row -- a shutdown that cancelled the run would leave
    `interrupted` and no snapshot, which is exactly what the companion test below asserts.
    """
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS", "5")
    get_settings.cache_clear()
    await register_a_wallet(scheduled_lifespan)

    async def dawdle(addresses: object) -> None:
        del addresses
        for _ in range(20):
            await asyncio.sleep(0)

    provider = StubChainProvider(
        ChainKey.BITCOIN,
        {DEFAULT_BITCOIN_ADDRESS: 123_456_789},
        on_fetch=dawdle,
    )
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: provider})
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(until(lambda: bool(provider.calls)), timeout=5)

    runs = await runs_in(scheduled_lifespan)
    assert [row["status"] for row in runs] == ["success"]
    assert runs[0]["finished_at"] is not None
    assert runs[0]["duration_ms"] is not None


async def test_a_run_that_outlasts_the_grace_is_recorded_as_interrupted(
    scheduled_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other shutdown, and the reason `interrupted` is a status rather than a guess.

    The provider never answers, the grace is zero, and the run is cancelled. What must not
    happen is the row being left at `running`: the next startup would sweep it, but between
    the two the table says a sync is happening in a process that no longer exists. The sweep
    at shutdown is what closes that window, and `finished_at` stays `NULL` because the run
    has no honest end time.
    """
    await register_a_wallet(scheduled_lifespan)
    forever = asyncio.Event()

    async def never_answer(addresses: object) -> None:
        del addresses
        await forever.wait()

    provider = StubChainProvider(
        ChainKey.BITCOIN,
        {DEFAULT_BITCOIN_ADDRESS: 1},
        on_fetch=never_answer,
    )
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: provider})
    app = create_app()

    try:
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(until(lambda: bool(provider.calls)), timeout=5)
    finally:
        # Release whatever is left, so a pending task cannot warn in a later, unrelated test.
        forever.set()
        await asyncio.sleep(0)

    runs = await runs_in(scheduled_lifespan)
    assert [row["status"] for row in runs] == ["interrupted"]
    assert runs[0]["finished_at"] is None
    assert runs[0]["duration_ms"] is None


async def test_a_running_row_from_a_dead_process_is_swept_at_startup(
    lifespan_database: Path,
) -> None:
    """The orphan sweep, driven through the lifespan rather than through the repository.

    `tests/db/test_sync_runs_repository.py` proves `sweep_interrupted` does what it says;
    what this adds is that startup calls it, which is the half a repository test cannot see.
    The row is planted by hand because the only other way to produce one is to kill a
    process mid-run.
    """
    await bring_the_schema_up(lifespan_database)
    async with own_session(lifespan_database) as session:
        await session.execute(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, wallets_total, "
                "wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', 'running', '2026-09-24 00:00:00.000000', 1, 0, 0)"
            )
        )
        await session.commit()

    assert [row["status"] for row in await runs_in(lifespan_database)] == ["running"]

    second = create_app()
    async with second.router.lifespan_context(second):
        pass

    swept = await runs_in(lifespan_database)
    assert [row["status"] for row in swept] == ["interrupted"]
    assert swept[0]["finished_at"] is None


# --------------------------------------------------------------------------------------
# The price refresh, which the spec regained after implementation had begun
# --------------------------------------------------------------------------------------


class FakePriceSource:
    """A price source that answers from a table, or refuses. Structural, checked by `mypy`.

    A near-copy of the fake in `tests/services/test_price_refresh_service.py`, and
    deliberately not imported from it: that module's fake carries the fields *its* tests need
    -- a volunteering mode, a per-call log -- and importing a test's fixture into another
    suite is how the first one stops being free to change.
    """

    name = "a-vendor"
    pairs = SUPPORTED_PAIRS

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.calls = 0

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return tuple(
            PriceQuote(
                asset_symbol=symbol,
                quote_currency=currency,
                amount=Decimal("1000.00"),
                source=self.name,
            )
            for symbol, currency in pairs
        )


_CONFORMS_AS_A_SOURCE: PriceSource = FakePriceSource()
"""`mypy --strict` is the assertion; see `tests/providers/fakes.py` for why not `isinstance`."""


@pytest.fixture
def priced_lifespan(
    lifespan_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """The same environment with the **price** timer on and the balance one still off."""
    monkeypatch.setenv("PORTFOLIO_PRICE_REFRESH_ENABLED", "true")
    get_settings.cache_clear()
    try:
        yield lifespan_database
    finally:
        get_settings.cache_clear()


def stub_price_sources(monkeypatch: pytest.MonkeyPatch, source: FakePriceSource) -> None:
    """Replace the vendors the lifespan builds, at the composition root that builds them.

    `price_scheduler_for` calls `price_sources(client, settings=...)`, so patching the name
    where `main` imported it intercepts every path into a vendor without this test knowing
    anything about Kraken's document shape -- the same argument `stub_chain_providers` makes
    about `ChainProviderRegistry.create`.
    """
    monkeypatch.setattr(
        "portfolio.main.price_sources",
        lambda client, **keywords: (source,),
    )


PRICES_SQL: Final = "SELECT asset_id, quote_currency, amount, source FROM prices ORDER BY id"

#: How many times a poll re-reads before giving up. A bound rather than a wait: every
#: iteration is a bare checkpoint, so this is a count of scheduler turns and not a duration,
#: and it is only ever exhausted when the write never happens.
POLL_TURNS: Final = 2000


async def prices_in(database: Path) -> list[dict[str, object]]:
    """Every `prices` row, read through a connection of this test's own."""
    async with own_session(database) as session:
        return await rows_of(session, PRICES_SQL)


async def wait_for_prices(database: Path, *, count: int) -> list[dict[str, object]]:
    """Poll `prices` until the refresh has **committed**, rather than until it was called.

    Waiting on the fake source's call counter is the obvious thing and it is one `await` too
    early: the vendor answers, and the rows are written afterwards. A test that left the
    lifespan at that moment would be asserting on a transaction that had not finished, and it
    would pass or fail depending on how the loop happened to interleave.

    `rollback` between reads is load-bearing. A session holds its read transaction open, and
    under WAL that transaction keeps seeing the snapshot it started with -- so a poll without
    it would re-read the same empty table forever however many times the writer committed.
    """
    async with own_session(database) as session:
        for _ in range(POLL_TURNS):
            rows = await rows_of(session, PRICES_SQL)
            if len(rows) >= count:
                return rows
            await session.rollback()
            await asyncio.sleep(0)
    message = f"the price refresh never committed {count} rows"
    raise AssertionError(message)


async def test_the_price_refresh_is_scheduled_and_actually_fills_the_cache(
    priced_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap the spec was corrected to close, asserted as rows rather than as an object.

    `GET /api/balances/current` is the first consumer of the price cache in the running
    application and nothing was going to fill it: #9 shipped `refresh_prices()` with no
    caller so the call budget could be measured by hand first. Without this timer a fresh
    deployment reads balances every fifteen minutes and reports every one of them
    `unpriced / never_fetched` forever -- the dashboard's flagship endpoint returning a zero
    total on a correct install.

    So the assertion is on the `prices` table. A test that checked `app.state.price_scheduler
    is not None` would pass for a timer whose first tick is an hour away, which is exactly
    the version of this that does not fix the problem.
    """
    await bring_the_schema_up(priced_lifespan)
    source = FakePriceSource()
    stub_price_sources(monkeypatch, source)
    app = create_app()

    async with app.router.lifespan_context(app):
        assert app.state.price_scheduler is not None
        assert app.state.price_scheduler.name == "price-refresh"
        rows = await wait_for_prices(priced_lifespan, count=len(SUPPORTED_PAIRS))

    assert len(rows) == len(SUPPORTED_PAIRS), "every supported pair is cached at startup"
    assert {row["source"] for row in rows} == {source.name}
    assert source.calls == 1, "one refresh, not one per pair"
    assert await prices_in(priced_lifespan) == rows, "and the rows survived the shutdown"


async def test_the_price_scheduler_is_not_started_when_disabled(
    lifespan_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's second off switch, asserted as "no task" and as "no vendor call".

    Both halves, because a timer that was built and never ticked and a timer that was never
    built look identical from outside for an hour -- longer than any test will wait.
    """
    source = FakePriceSource()
    stub_price_sources(monkeypatch, source)
    app = create_app()

    async with app.router.lifespan_context(app):
        assert app.state.price_scheduler is None

    assert source.calls == 0
    assert await prices_in(lifespan_database) == []


async def test_a_failing_price_refresh_does_not_stop_the_balance_sync(
    priced_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two timers, two tasks, and the spec's one-sentence promise made checkable.

    The realistic shape: Kraken has an afternoon, and the portfolio goes on recording what it
    holds even though it cannot say what that is worth. The opposite failure -- a chain index
    down taking the price cache with it -- is the same argument and is why the two have
    separate switches at all.

    Driven through the whole lifespan rather than through two `IntervalScheduler`s, because
    what `tests/services/test_scheduler.py` cannot see is whether the lifespan wired them as
    two tasks or awaited one before starting the other.
    """
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "true")
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS", "5")
    get_settings.cache_clear()
    await register_a_wallet(priced_lifespan)
    failing = FakePriceSource(raises=ProviderUnavailableError("the vendor did not answer"))
    stub_price_sources(monkeypatch, failing)
    provider = StubChainProvider(ChainKey.BITCOIN, {DEFAULT_BITCOIN_ADDRESS: 123_456_789})
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: provider})
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(until(lambda: bool(provider.calls) and failing.calls > 0), timeout=5)

    assert failing.calls > 0, "the price vendor really was asked, and really refused"
    assert await prices_in(priced_lifespan) == [], "a refused refresh writes no price"
    runs = await runs_in(priced_lifespan)
    assert [row["status"] for row in runs] == ["success"], (
        "the balance sync finished regardless of what the price vendor did"
    )


async def test_a_startup_that_fails_early_still_closes_what_it_opened(
    lifespan_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lifespan that fails before the coordinator exists still releases the client and engine.

    The failure is planted in the first step after the client is built, so the `finally`
    runs with no coordinator and no scheduler -- the branch where "drain the run in flight"
    has nothing to drain and must not try. The original exception has to be the one that
    comes out: a teardown that raised on the way down would bury the only error that says
    why the container did not start.
    """
    del lifespan_database

    async def refuse_to_bootstrap(*arguments: object) -> None:
        del arguments
        message = "the owner could not be created"
        raise RuntimeError(message)

    monkeypatch.setattr("portfolio.main.bootstrap_owner", refuse_to_bootstrap)
    app = create_app()

    with pytest.raises(RuntimeError, match="the owner could not be created"):
        async with app.router.lifespan_context(app):
            pytest.fail("the lifespan must not have started")

    assert app.state.http_client.is_closed is True
    assert getattr(app.state, "sync_coordinator", None) is None


class PartialPriceSource(FakePriceSource):
    """A source that answers every pair except one, so a refresh is incomplete."""

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        answered = await super().fetch(pairs)
        return tuple(quote for quote in answered if quote.pair != ("KAS", "EUR"))


async def test_an_incomplete_price_refresh_is_a_warning_naming_pairs_and_never_amounts(
    priced_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line an operator should see, and what it must not carry.

    A refresh that could not price a pair is a warning, because it is what leaves a holding
    `unpriced` on the dashboard. It names the pair, which is public, and counts the rest.
    **It never carries an amount**: nobody reads a scheduled refresh's output, it runs every
    hour forever, and a log line carrying a number is one careless edit from a log line
    carrying a quantity -- which is the owner's holdings. The fake answers `1000.00` for
    every pair precisely so that the absence of that string is a meaningful assertion.
    """
    await bring_the_schema_up(priced_lifespan)
    source = PartialPriceSource()
    stub_price_sources(monkeypatch, source)
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            await wait_for_prices(priced_lifespan, count=len(SUPPORTED_PAIRS) - 1)

    incomplete = [entry for entry in captured if entry["event"] == "price_refresh_incomplete"]
    assert len(incomplete) == 1
    assert incomplete[0]["log_level"] == "warning"
    assert incomplete[0]["unavailable"] == ("KAS/EUR",)
    assert incomplete[0]["refreshed"] == len(SUPPORTED_PAIRS) - 1
    assert "1000" not in repr(incomplete[0]), "a price must never reach a log line"
