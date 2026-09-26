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
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import pytest
from anyio import Path as AsyncPath
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from portfolio.config import get_settings
from portfolio.db.engine import create_database_engine, create_session_factory
from portfolio.db.models import Asset
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import ExchangeKey
from portfolio.main import create_app, drain_coordinators
from portfolio.providers.errors import ProviderUnavailableError
from portfolio.providers.prices.base import SUPPORTED_PAIRS, PriceQuote, PriceSource
from portfolio.services.scheduler import IntervalScheduler
from portfolio.services.sync_coordinator import SyncCoordinator
from tests.balance_harness import (
    DEFAULT_BITCOIN_ADDRESS,
    SYNC_RUNS_SQL,
    PacedSleep,
    StubChainProvider,
    insert_user,
    insert_wallet,
    rows_of,
    sqlite_timestamp,
    stub_chain_providers,
)
from tests.exchange_sync_harness import SimulatedVenue
from tests.offline_http import (
    ReachedAVendorError,
    take_offline_attempts,
    the_real_http_client,
    use_an_offline_http_client,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.pool import ConnectionPoolEntry

    from portfolio.providers.prices.base import PricePair

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
    monkeypatch.setenv("PORTFOLIO_EXCHANGE_SYNC_ENABLED", "false")
    monkeypatch.delenv("PORTFOLIO_EXCHANGE_HISTORY_START", raising=False)
    # Offline unless a test says otherwise: see `tests/offline_http.py`. The two tests here
    # whose subject is the client itself take the real one back and prove they got it.
    use_an_offline_http_client(monkeypatch)
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
    """Wait until `condition` holds, polling every 10 ms. Bounded by the caller's `wait_for`.

    **A real sleep, not `asyncio.sleep(0)`.** A bare checkpoint hands control to the scheduler
    and takes it straight back, so the loop spins, and it spins against the one thread that
    can make the condition true: what these tests wait for is written through `aiosqlite`,
    whose work runs in a worker thread competing for the GIL. On a two-core CI runner under
    coverage that starved the worker past the five-second bound twice, in two different tests
    (#71, and `test_shutdown_drains_both_coordinators` on PR #78, where the exchange run the
    spin was waiting on took 3.3 s). `tests/test_no_network.py` learned the same lesson first.

    The condition is set deep inside a lifespan's own task and has no event to hang an
    `asyncio.Event` off without reaching into the application to plant one -- which would be
    a seam in production code that exists only for a test. `noqa: ASYNC110` for that reason;
    every caller wraps this in `asyncio.wait_for`, so a condition that never holds fails
    with a timeout rather than hanging the suite.
    """
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


async def test_the_http_client_is_closed_on_shutdown(
    lifespan_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring #6 through #9 each deferred to #10, and the half that gets forgotten.

    The client is process-wide *by construction*: the per-host rate limiter's state lives on
    its transport, so a second client would silently halve the interval it claims to
    enforce. Closing it is what releases the connection pool; a lifespan that built one and
    never closed it would leak a socket per restart on a machine expected to run for months.

    **The real client**, deliberately, and `built` proves it: this is one of the two tests
    in the suite whose subject is the client production builds, so it opts out of the
    offline one every other lifespan here runs on.
    """
    del lifespan_database
    built = the_real_http_client(monkeypatch)
    app = create_app()

    async with app.router.lifespan_context(app):
        client = app.state.http_client
        assert built == [client], "the lifespan built the real client, exactly once"
        assert client.is_closed is False

    assert client.is_closed is True


async def test_the_suite_runs_the_lifespan_on_a_client_that_refuses_every_request(
    lifespan_database: Path,
) -> None:
    """The second no-network layer, asserted rather than assumed.

    With the offline client in place a request through `app.state.http_client` fails at
    once, naming the host. Without it this test would either hang on a real connection or
    reach a real vendor -- the failure the layer exists to make loud -- so it is also what
    fails if `lifespan_database` stops installing it.

    The path carries a testnet address, as a chain provider's would, and the assertion that
    it is absent from the message is the half that keeps the refusal itself from becoming a
    place an address is printed.
    """
    del lifespan_database
    app = create_app()

    async with app.router.lifespan_context(app):
        with pytest.raises(ReachedAVendorError) as caught:
            await app.state.http_client.get(
                f"https://an-index.invalid/address/{DEFAULT_BITCOIN_ADDRESS}"
            )

    assert "an-index.invalid" in str(caught.value)
    assert DEFAULT_BITCOIN_ADDRESS not in str(caught.value)
    assert take_offline_attempts() == ["an-index.invalid"], "recorded once, host only"


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
        # Read **inside** the running lifespan. After it exits, the shutdown sweep would have
        # marked the row anyway, and a check made there proves nothing about startup --
        # which is how a lifespan with its startup sweep deleted passed this test before.
        during = await runs_in(lifespan_database)

    assert [row["status"] for row in during] == ["interrupted"], "swept at startup"
    assert during[0]["finished_at"] is None


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
    # The real client: whether *it* is closed on a failed startup is the question.
    built = the_real_http_client(monkeypatch)
    app = create_app()

    with pytest.raises(RuntimeError, match="the owner could not be created"):
        async with app.router.lifespan_context(app):
            pytest.fail("the lifespan must not have started")

    assert built == [app.state.http_client], "the real client was the one built"
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
            # The line itself, not the commit before it: the warning is logged one awaited
            # read after the rows land, and a lifespan left inside that window cancels the
            # tick before it logs. Waiting on the rows failed 2 runs in 15 for that reason.
            await asyncio.wait_for(
                until(
                    lambda: any(entry["event"] == "price_refresh_incomplete" for entry in captured)
                ),
                timeout=5,
            )

    incomplete = [entry for entry in captured if entry["event"] == "price_refresh_incomplete"]
    assert len(incomplete) == 1
    assert incomplete[0]["log_level"] == "warning"
    assert incomplete[0]["unavailable"] == ("KAS/EUR",)
    assert incomplete[0]["refreshed"] == len(SUPPORTED_PAIRS) - 1
    assert "1000" not in repr(incomplete[0]), "a price must never reach a log line"


async def test_the_shared_client_is_still_open_while_shutdown_waits_for_a_run(
    scheduled_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown drains the run in flight *before* it closes the client that run is using.

    The order is the content of the lifespan's `finally`: stop the timers, drain the run,
    sweep, and only then close the client and dispose the engine. Closing the client first
    passes every other test here, because they stub the provider above the client and never
    touch it. This provider does touch it: it runs during the grace period, after the timers
    have stopped, and records whether the client it was built over is still open -- failing
    its chain if not, as a real provider would with a closed client.

    It waits a bounded number of loop turns for the client to close rather than looking once.
    With the right order nothing can close it until the run ends, so it never closes; with
    the wrong one it closes within a few turns, and a single early look could miss that.
    """
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS", "5")
    get_settings.cache_clear()
    await register_a_wallet(scheduled_lifespan)
    observed_closed: list[bool] = []

    async def use_the_client_during_shutdown(addresses: object) -> None:
        del addresses
        client = registry.clients[-1]
        # Shutdown has begun once the lifespan has stopped the balance timer.
        await asyncio.wait_for(
            until(lambda: not is_running(app.state.balance_scheduler)), timeout=5
        )
        for _ in range(200):
            if client.is_closed:
                break
            await asyncio.sleep(0)
        observed_closed.append(client.is_closed)
        if client.is_closed:
            message = "the shared client was closed while a run still needed it"
            raise RuntimeError(message)

    provider = StubChainProvider(
        ChainKey.BITCOIN,
        {DEFAULT_BITCOIN_ADDRESS: 123_456_789},
        on_fetch=use_the_client_during_shutdown,
    )
    registry = stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: provider})
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(until(lambda: bool(provider.calls)), timeout=5)

    assert observed_closed == [False], "the client was open for the whole of the drained run"
    runs = await runs_in(scheduled_lifespan)
    assert [row["status"] for row in runs] == ["success"]


# --------------------------------------------------------------------------------------
# G: the price timer's startup condition, driven with a sleep the test controls
# --------------------------------------------------------------------------------------


def with_a_paced_sleep(monkeypatch: pytest.MonkeyPatch, sleep: PacedSleep) -> None:
    """Give every timer the lifespan builds a sleep this test controls.

    Patched at the composition root, where `main` looks the class up, and only the `sleep`
    is changed. It is what lets a test observe the one thing that otherwise has no outside
    trace: that the timer decided **not** to run at startup and went straight to sleep.
    """

    class PacedScheduler(IntervalScheduler):
        def __init__(self, **keywords: Any) -> None:
            keywords.setdefault("sleep", sleep)
            super().__init__(**keywords)

    monkeypatch.setattr("portfolio.main.IntervalScheduler", PacedScheduler)


async def seed_every_price(database: Path, *, fetched_at: datetime) -> None:
    """A price for every supported pair, as if the last refresh happened at `fetched_at`."""
    async with own_session(database) as session:
        for symbol, currency in sorted(SUPPORTED_PAIRS):
            await session.execute(
                text(
                    "INSERT INTO prices (asset_id, quote_currency, amount, source, as_of, "
                    "fetched_at) VALUES ((SELECT id FROM assets WHERE symbol = :symbol), "
                    ":currency, '1000.000000000000', 'a-vendor', :at, :at)"
                ),
                {"symbol": symbol, "currency": currency, "at": sqlite_timestamp(fetched_at)},
            )
        await session.commit()


@pytest.mark.parametrize(
    ("age", "refreshed_at_startup"),
    [(timedelta(minutes=5), False), (timedelta(hours=2), True)],
    ids=["five minutes old", "two hours old"],
)
async def test_fresh_prices_suppress_the_startup_refresh(
    priced_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
    age: timedelta,
    refreshed_at_startup: bool,
) -> None:
    """A restart with prices five minutes old asks no vendor; one with prices two hours old does.

    The price side of the crash-loop argument: a container restarting every thirty seconds
    must not ask Kraken for four pairs every thirty seconds. The startup condition reads the
    newest `fetched_at` in the cache, so the test seeds it and then watches what the timer
    does before its first sleep. Both ages are asserted, because a condition that never ran
    at startup would pass the fresh case alone.
    """
    await bring_the_schema_up(priced_lifespan)
    await seed_every_price(priced_lifespan, fetched_at=datetime.now(UTC) - age)
    source = FakePriceSource()
    stub_price_sources(monkeypatch, source)
    sleep = PacedSleep()
    with_a_paced_sleep(monkeypatch, sleep)
    app = create_app()

    async with app.router.lifespan_context(app):
        await sleep.reached()

    assert source.calls == (1 if refreshed_at_startup else 0)


# --------------------------------------------------------------------------------------
# Review contract 3: the balance timer counts attempts, so a crash loop is not a sync loop
# --------------------------------------------------------------------------------------


async def seed_runs(database: Path, runs: list[tuple[str, timedelta]]) -> None:
    """`sync_runs` rows by status and age, in the order given -- which is identity order."""
    now = datetime.now(UTC)
    async with own_session(database) as session:
        for status, age in runs:
            started = sqlite_timestamp(now - age)
            finished = started if status == "success" else None
            await session.execute(
                text(
                    "INSERT INTO sync_runs (trigger, status, started_at, finished_at, "
                    "duration_ms, wallets_total, wallets_succeeded, wallets_failed) "
                    "VALUES ('startup', :status, :started, :finished, :duration, 1, 0, 0)"
                ),
                {
                    "status": status,
                    "started": started,
                    "finished": finished,
                    "duration": 1 if finished else None,
                },
            )
        await session.commit()


@pytest.mark.parametrize(
    ("history", "synced_at_startup"),
    [
        (
            [
                ("success", timedelta(days=2)),
                ("interrupted", timedelta(minutes=5)),
                ("interrupted", timedelta(minutes=3)),
                ("interrupted", timedelta(minutes=1)),
            ],
            False,
        ),
        ([("success", timedelta(days=2))], True),
    ],
    ids=["three recent interrupted attempts", "only an old success"],
)
async def test_recent_attempts_suppress_the_startup_sync_even_when_none_finished(
    scheduled_lifespan: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: list[tuple[str, timedelta]],
    synced_at_startup: bool,
) -> None:
    """The review's scenario: a success two days old, and interrupted runs 5, 3 and 1 minute ago.

    A process that crashes mid-sync leaves `interrupted` runs and no finished one. Reading
    only finished runs, every restart found the success two days old, decided a sync was
    due, and walked straight back into the crash -- two public indexes called once per
    restart, for as long as the loop lasted. Counting attempts, the newest is a minute old
    and nothing is due.

    The second case is the control: the same lifespan with only the old success does sync
    at startup, so the first case's silence is the condition, not a timer that never ticks.
    """
    await register_a_wallet(scheduled_lifespan)
    await seed_runs(scheduled_lifespan, history)
    provider = StubChainProvider(ChainKey.BITCOIN, {DEFAULT_BITCOIN_ADDRESS: 123_456_789})
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: provider})
    sleep = PacedSleep()
    with_a_paced_sleep(monkeypatch, sleep)
    app = create_app()

    async with app.router.lifespan_context(app):
        await sleep.reached()

    assert bool(provider.calls) is synced_at_startup


# --------------------------------------------------------------------------------------
# #15: the exchange timer, its coordinator, and both run tables
# --------------------------------------------------------------------------------------

EXCHANGE_RUNS_SQL: Final = (
    "SELECT id, trigger, status, finished_at FROM exchange_sync_runs ORDER BY id"
)


class ProviderMappingCalls:
    """Stands in for `exchange_providers` at the composition root, and counts its calls."""

    def __init__(self, providers: dict[ExchangeKey, SimulatedVenue]) -> None:
        self.providers = providers
        self.calls = 0

    def __call__(self, client: object, **keywords: object) -> MappingProxyType[ExchangeKey, Any]:
        del client, keywords
        self.calls += 1
        return MappingProxyType(dict(self.providers))


def configure_venues(
    monkeypatch: pytest.MonkeyPatch, providers: dict[ExchangeKey, SimulatedVenue]
) -> ProviderMappingCalls:
    """Hand the lifespan these venues as the configured ones, where `main` looks them up."""
    stub = ProviderMappingCalls(providers)
    monkeypatch.setattr("portfolio.main.exchange_providers", stub)
    return stub


async def exchange_runs_in(database: Path) -> list[dict[str, object]]:
    async with own_session(database) as session:
        return await rows_of(session, EXCHANGE_RUNS_SQL)


async def with_an_owner(database: Path) -> None:
    await bring_the_schema_up(database)
    async with own_session(database) as session:
        await insert_user(session)


@pytest.fixture
def exchange_timer_on(lifespan_database: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The exchange schedule **on**, a grace that lets a startup run finish."""
    monkeypatch.setenv("PORTFOLIO_EXCHANGE_SYNC_ENABLED", "true")
    monkeypatch.setenv("PORTFOLIO_EXCHANGE_SYNC_SHUTDOWN_GRACE_SECONDS", "5")
    get_settings.cache_clear()
    try:
        yield lifespan_database
    finally:
        get_settings.cache_clear()


async def test_the_exchange_coordinator_is_published_even_with_nothing_configured(
    lifespan_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual sync must work with the timer off, so the coordinator always exists."""
    del lifespan_database
    stub = configure_venues(monkeypatch, {})
    app = create_app()

    async with app.router.lifespan_context(app):
        coordinator = app.state.exchange_sync_coordinator
        assert isinstance(coordinator, SyncCoordinator)
        assert coordinator.task_name == "exchange-sync"
        assert coordinator.log_prefix == "exchange_sync"
        assert coordinator is not app.state.sync_coordinator
        assert app.state.configured_exchanges == frozenset()
        assert app.state.exchange_scheduler is None

    assert stub.calls == 1


async def test_the_configured_set_is_the_provider_mappings_keys_and_nothing_else(
    lifespan_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only thing the read side learns about credentials: which venues have them."""
    del lifespan_database
    venue = SimulatedVenue()
    configure_venues(monkeypatch, {ExchangeKey.BITGET: venue})
    app = create_app()

    async with app.router.lifespan_context(app):
        assert app.state.configured_exchanges == frozenset({ExchangeKey.BITGET})
        assert isinstance(app.state.configured_exchanges, frozenset)
        state: dict[str, object] = app.state._state
        leaked = [
            name
            for name, value in state.items()
            if value is venue or (isinstance(value, Mapping) and venue in value.values())
        ]
        assert leaked == [], "a provider, or the mapping holding it, was published"


async def test_the_exchange_timer_is_not_built_when_nothing_is_configured(
    exchange_timer_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install without credentials writes no empty run every fifteen minutes."""
    configure_venues(monkeypatch, {})
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            assert app.state.exchange_scheduler is None

    assert await exchange_runs_in(exchange_timer_on) == []
    disabled = [
        entry
        for entry in captured
        if entry["event"] == "scheduler_disabled" and entry.get("scheduler") == "exchange-sync"
    ]
    assert [entry["reason"] for entry in disabled] == ["no_exchange_configured"]


async def test_the_exchange_timer_is_not_built_when_disabled(
    lifespan_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await with_an_owner(lifespan_database)
    venue = SimulatedVenue()
    configure_venues(monkeypatch, {ExchangeKey.BITGET: venue})
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            assert app.state.exchange_scheduler is None

    assert venue.calls == []
    assert await exchange_runs_in(lifespan_database) == []
    disabled = [
        entry
        for entry in captured
        if entry["event"] == "scheduler_disabled" and entry.get("scheduler") == "exchange-sync"
    ]
    assert [entry["reason"] for entry in disabled] == ["disabled"]


async def test_the_exchange_timer_is_built_when_enabled_and_configured_and_syncs_at_startup(
    exchange_timer_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup tick is a `startup` run, through the coordinator, against the venue."""
    await with_an_owner(exchange_timer_on)
    venue = SimulatedVenue()
    stub = configure_venues(monkeypatch, {ExchangeKey.BITGET: venue})
    app = create_app()

    async with app.router.lifespan_context(app):
        scheduler = app.state.exchange_scheduler
        assert scheduler is not None
        assert scheduler.name == "exchange-sync"
        assert scheduler.interval_seconds == 15 * 60
        assert is_running(scheduler) is True
        await asyncio.wait_for(until(lambda: bool(venue.calls)), timeout=5)

    assert is_running(scheduler) is False
    assert stub.calls == 1, "the provider mapping is built once, not per run"
    runs = await exchange_runs_in(exchange_timer_on)
    assert [(row["trigger"], row["status"]) for row in runs] == [("startup", "success")]
    assert await runs_in(exchange_timer_on) == [], "the balance timer stayed off"


async def test_both_run_tables_are_swept_at_startup(lifespan_database: Path) -> None:
    await bring_the_schema_up(lifespan_database)
    async with own_session(lifespan_database) as session:
        await session.execute(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, wallets_total, "
                "wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', 'running', '2026-09-24 00:00:00.000000', 1, 0, 0)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO exchange_sync_runs (trigger, status, started_at, accounts_total) "
                "VALUES ('scheduled', 'running', '2026-09-24 00:00:00.000000', 1)"
            )
        )
        await session.commit()
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            during_balances = await runs_in(lifespan_database)
            during_exchanges = await exchange_runs_in(lifespan_database)

    assert [row["status"] for row in during_balances] == ["interrupted"]
    assert [row["status"] for row in during_exchanges] == ["interrupted"]
    assert during_exchanges[0]["finished_at"] is None
    swept = [
        entry for entry in captured if entry["event"] == "exchange_sync_runs_marked_interrupted"
    ]
    assert [entry["runs"] for entry in swept] == [1]


async def test_an_exchange_run_that_outlasts_the_grace_is_recorded_as_interrupted(
    exchange_timer_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The venue never answers, the grace is zero: the shutdown sweep closes the row."""
    monkeypatch.setenv("PORTFOLIO_EXCHANGE_SYNC_SHUTDOWN_GRACE_SECONDS", "0")
    get_settings.cache_clear()
    await with_an_owner(exchange_timer_on)
    forever = asyncio.Event()
    venue = SimulatedVenue()

    async def never_answer(call: object) -> None:
        del call
        await forever.wait()

    venue.on_call = never_answer
    configure_venues(monkeypatch, {ExchangeKey.BITGET: venue})
    app = create_app()

    try:
        with capture_logs() as captured:
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(until(lambda: bool(venue.calls)), timeout=5)
    finally:
        forever.set()
        await asyncio.sleep(0)

    runs = await exchange_runs_in(exchange_timer_on)
    assert [row["status"] for row in runs] == ["interrupted"]
    assert runs[0]["finished_at"] is None
    assert "exchange_sync_cancelled_at_shutdown" in [entry["event"] for entry in captured]


async def test_shutdown_drains_both_coordinators(
    exchange_timer_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A balance run and an exchange run both in flight at shutdown, both allowed to finish."""
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "true")
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS", "5")
    get_settings.cache_clear()
    await register_a_wallet(exchange_timer_on)

    async def dawdle(ignored: object) -> None:
        del ignored
        for _ in range(20):
            await asyncio.sleep(0)

    chain = StubChainProvider(
        ChainKey.BITCOIN, {DEFAULT_BITCOIN_ADDRESS: 123_456_789}, on_fetch=dawdle
    )
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: chain})
    venue = SimulatedVenue()
    venue.on_call = dawdle
    configure_venues(monkeypatch, {ExchangeKey.BITGET: venue})
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(until(lambda: bool(chain.calls) and bool(venue.calls)), timeout=5)

    assert [row["status"] for row in await runs_in(exchange_timer_on)] == ["success"]
    assert [row["status"] for row in await exchange_runs_in(exchange_timer_on)] == ["success"]


async def test_a_drain_that_raises_does_not_stop_the_other() -> None:
    """`drain_coordinators` runs in the lifespan's `finally`: it logs, it never raises."""

    class Broken:
        async def drain(self, *, grace_seconds: int) -> bool:
            del grace_seconds
            message = "the drain itself failed"
            raise RuntimeError(message)

    class Healthy:
        def __init__(self) -> None:
            self.graces: list[int] = []

        async def drain(self, *, grace_seconds: int) -> bool:
            self.graces.append(grace_seconds)
            return True

    healthy = Healthy()

    with capture_logs() as captured:
        await drain_coordinators((Broken(), 5), (None, 5), (healthy, -3))

    assert healthy.graces == [0], "a negative grace is clamped to zero"
    failed = [entry for entry in captured if entry["event"] == "sync_drain_failed"]
    assert [entry["error_type"] for entry in failed] == ["RuntimeError"]


async def test_the_two_coordinators_are_drained_concurrently_not_one_after_the_other() -> None:
    """Two grace periods in sequence would spend the container's whole stop grace period.

    Each fake drain waits at a two-party barrier, which releases only when both drains are
    waiting at once -- so the barrier *is* the concurrency check, and nothing sleeps on the
    passing path. Drained one after the other, the first would wait alone until its bound
    expired, and the failure would be logged and counted below.
    """
    barrier = asyncio.Barrier(2)
    finished: list[str] = []

    class AtTheBarrier:
        def __init__(self, name: str) -> None:
            self.name = name

        async def drain(self, *, grace_seconds: int) -> bool:
            del grace_seconds
            await asyncio.wait_for(barrier.wait(), timeout=2)
            finished.append(self.name)
            return True

    with capture_logs() as captured:
        await asyncio.wait_for(
            drain_coordinators((AtTheBarrier("balance"), 10), (AtTheBarrier("exchange"), 10)),
            timeout=5,
        )

    assert sorted(finished) == ["balance", "exchange"]
    assert [entry for entry in captured if entry["event"] == "sync_drain_failed"] == []


async def test_an_exchange_sweep_that_fails_does_not_stop_startup_or_the_balance_sweep(
    lifespan_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each sweep in its own `try`: bookkeeping never stops a healthy application."""
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

    async def refuse(self: object) -> int:
        del self
        message = "database is locked"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "portfolio.repositories.exchange_sync_runs.ExchangeSyncRunRepository.sweep_interrupted",
        refuse,
    )
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            during = await runs_in(lifespan_database)

    assert [row["status"] for row in during] == ["interrupted"]
    events = [entry["event"] for entry in captured]
    assert events.count("exchange_sync_orphan_sweep_failed") == 2, "at startup and at shutdown"


async def seed_exchange_run(database: Path, *, status: str, age: timedelta) -> None:
    started = sqlite_timestamp(datetime.now(UTC) - age)
    async with own_session(database) as session:
        await session.execute(
            text(
                "INSERT INTO exchange_sync_runs (trigger, status, started_at, accounts_total) "
                "VALUES ('startup', :status, :started, 1)"
            ),
            {"status": status, "started": started},
        )
        await session.commit()


@pytest.mark.parametrize(
    ("status", "age", "synced_at_startup"),
    [
        ("interrupted", timedelta(minutes=1), False),
        ("success", timedelta(hours=2), True),
    ],
    ids=["an interrupted attempt a minute ago", "a success two hours ago"],
)
async def test_a_recent_exchange_attempt_suppresses_the_startup_sync(
    exchange_timer_on: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    age: timedelta,
    synced_at_startup: bool,
) -> None:
    """A crash-looping container must not ask a venue again on every restart."""
    await with_an_owner(exchange_timer_on)
    await seed_exchange_run(exchange_timer_on, status=status, age=age)
    venue = SimulatedVenue()
    configure_venues(monkeypatch, {ExchangeKey.BITGET: venue})
    sleep = PacedSleep()
    with_a_paced_sleep(monkeypatch, sleep)
    app = create_app()

    async with app.router.lifespan_context(app):
        await sleep.reached()

    assert bool(venue.calls) is synced_at_startup
