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
from typing import TYPE_CHECKING

import pytest
from anyio import Path as AsyncPath
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from portfolio.config import get_settings
from portfolio.db.engine import create_database_engine, create_session_factory
from portfolio.db.models import Asset
from portfolio.domain.chains import ChainKey
from portfolio.main import create_app
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
    from collections.abc import AsyncIterator, Callable, Iterator
    from pathlib import Path

    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.pool import ConnectionPoolEntry

EXPECTED_SEED_SYMBOLS = ["BTC", "KAS", "USDT"]


@pytest.fixture
def lifespan_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the process-wide settings at a temporary file, and put them back.

    The balance schedule is off here for the reason `tests/auth/conftest.py` sets out at
    length: a lifespan with the loop running syncs at startup against two real public
    indexes, because a database created a moment ago has no finished run to suppress it.
    The tests below that are about the scheduler turn it back on by name.
    """
    database_path = tmp_path / "lifespan" / "portfolio.db"
    monkeypatch.setenv(
        "PORTFOLIO_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "false")
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


async def test_the_http_client_is_built_and_closed_with_the_application(
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
        assert scheduler.running is True
        # Let the startup tick get through the coordinator and the provider.
        await asyncio.wait_for(until(lambda: bool(provider.calls)), timeout=5)

    assert scheduler.running is False
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
