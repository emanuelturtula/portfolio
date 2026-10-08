"""Spec 038: the `balance-rebuild` timer in the real lifespan.

It is not built when switched off; when on, it rebuilds at startup a database that has never
been rebuilt, and a recent rebuild means it sleeps what is left of the interval instead. Its
two log lines carry counts, wallet ids and reasons, never an address or an amount.

The chain registry is the one substitution, through `stub_chain_providers`, with a stub that
can also read a history; everything else is real: the migrations, the service, the
repository and the table read back through a connection of the test's own. Every other timer
is off, and the HTTP client is the offline one, so nothing here can reach a vendor.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import pytest
from anyio import to_thread
from sqlalchemy import text
from structlog.testing import capture_logs

from portfolio.config import get_settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import (
    create_database_engine,
    create_session_factory,
    ensure_database_directory,
)
from portfolio.domain.chains import ChainKey
from portfolio.main import BALANCE_REBUILD_TASK_NAME, create_app, latest_balance_rebuild
from portfolio.providers.base import (
    AddressHistory,
    HistoryIncomplete,
    TransactionHistoryReader,
    TxEffect,
)
from portfolio.services.scheduler import IntervalScheduler
from tests.balance_harness import (
    DEFAULT_BITCOIN_ADDRESS,
    DEFAULT_KASPA_ADDRESS,
    StubChainProvider,
    insert_user,
    insert_wallet,
    rows_of,
    sqlite_timestamp,
    stub_chain_providers,
)
from tests.offline_http import use_an_offline_http_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, MutableMapping, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

PARK_TIMEOUT: Final = 10
DAY_SECONDS: Final = 86_400

#: An amount no other field of a log line could hold by accident.
RECEIVED: Final = 123_456_789

REBUILT_SQL: Final = (
    "SELECT wallet_id, day, confirmed, decimals FROM reconstructed_balances ORDER BY wallet_id, day"
)


class HistoryStub(StubChainProvider):
    """The harness's stub, able to read a history too: one received amount, three days ago.

    `incomplete` makes every history it answers unproven, with that reason.
    """

    def __init__(self, *, incomplete: HistoryIncomplete | None = None) -> None:
        super().__init__(ChainKey.BITCOIN)
        self.incomplete = incomplete
        self.histories: list[str] = []

    async def address_history(self, address: str) -> AddressHistory:
        self.histories.append(address)
        received = datetime.now(UTC) - timedelta(days=3)
        return AddressHistory(
            address=address,
            balance=RECEIVED,
            decimals=8,
            effects=(TxEffect(occurred_at=received, delta=RECEIVED),),
            incomplete=self.incomplete,
        )


_CONFORMS: TransactionHistoryReader = HistoryStub()


class GatedSleep:
    """The timer's sleep: it records the delay, then parks until the test is done."""

    def __init__(self) -> None:
        self.delays: list[int] = []
        self._parked: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, delay: int) -> None:
        self.delays.append(delay)
        self._parked.put_nowait(None)
        await asyncio.Event().wait()

    async def parked(self) -> None:
        """Wait until the loop has reached its first sleep: any startup tick is done."""
        await asyncio.wait_for(self._parked.get(), timeout=PARK_TIMEOUT)


@pytest.fixture
def rebuild_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A lifespan with only the rebuild timer on, over an offline client."""
    database_path = tmp_path / "lifespan" / "portfolio.db"
    monkeypatch.setenv("PORTFOLIO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_PRICE_REFRESH_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_PRICE_BACKFILL_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_BACKUP_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_BACKUP_DIR", str(tmp_path / "lifespan" / "backups"))
    monkeypatch.setenv("PORTFOLIO_BALANCE_REBUILD_ENABLED", "true")
    monkeypatch.delenv("PORTFOLIO_BALANCE_REBUILD_INTERVAL_MINUTES", raising=False)
    use_an_offline_http_client(monkeypatch)
    get_settings.cache_clear()
    try:
        yield database_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def sleep(monkeypatch: pytest.MonkeyPatch) -> GatedSleep:
    gated = GatedSleep()

    class GatedScheduler(IntervalScheduler):
        def __init__(self, **keywords: Any) -> None:
            keywords.setdefault("sleep", gated)
            super().__init__(**keywords)

    monkeypatch.setattr("portfolio.main.IntervalScheduler", GatedScheduler)
    return gated


@asynccontextmanager
async def own_session(database: Path) -> AsyncIterator[AsyncSession]:
    """A session over the same file, on an engine this test owns."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    try:
        async with create_session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()


async def a_wallet(database: Path) -> int:
    """Migrate the file and plant one owner and one active Bitcoin wallet. Returns its id."""
    url = f"sqlite+aiosqlite:///{database.as_posix()}"
    ensure_database_directory(url)
    await to_thread.run_sync(upgrade_to_head, url)
    async with own_session(database) as session:
        user_id = await insert_user(session)
        return await insert_wallet(
            session, user_id=user_id, chain_key=ChainKey.BITCOIN, address=DEFAULT_BITCOIN_ADDRESS
        )


async def plant_a_rebuilt_day(
    database: Path, wallet_id: int, *, rebuilt_at: datetime, day: str = "2026-01-01"
) -> None:
    async with own_session(database) as session:
        await session.execute(
            text(
                "INSERT INTO reconstructed_balances (wallet_id, day, confirmed, decimals, "
                "rebuilt_at) VALUES (:wallet_id, :day, 5, 8, :at)"
            ),
            {"wallet_id": wallet_id, "day": day, "at": sqlite_timestamp(rebuilt_at)},
        )
        await session.commit()


async def rebuilt_in(database: Path) -> list[dict[str, object]]:
    async with own_session(database) as session:
        return await rows_of(session, REBUILT_SQL)


def events_named(
    captured: Sequence[MutableMapping[str, Any]], name: str
) -> list[MutableMapping[str, Any]]:
    return [entry for entry in captured if entry["event"] == name]


def is_running(scheduler: IntervalScheduler) -> bool:
    """Read afresh, through a call `mypy` cannot narrow across the lifespan's exit."""
    return scheduler.running


async def until(condition: Callable[[], bool]) -> None:
    """Poll every 10 ms, for the reason `tests/db/test_lifespan.py::until` gives."""
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


async def test_a_database_never_rebuilt_is_rebuilt_at_startup(
    rebuild_database: Path, monkeypatch: pytest.MonkeyPatch, sleep: GatedSleep
) -> None:
    """A running daily timer, rows on disk, and one `info` line with counts and nothing else."""
    wallet_id = await a_wallet(rebuild_database)
    stub = HistoryStub()
    registry = stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: stub})
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            scheduler = app.state.balance_rebuild_scheduler
            assert isinstance(scheduler, IntervalScheduler)
            assert scheduler.name == BALANCE_REBUILD_TASK_NAME == "balance-rebuild"
            assert scheduler.interval_seconds == 1440 * 60, "daily by default"
            assert is_running(scheduler) is True
            await sleep.parked()
            assert registry.clients == [app.state.http_client], "over the shared client"

    assert is_running(scheduler) is False
    assert stub.histories == [DEFAULT_BITCOIN_ADDRESS]
    rows = await rebuilt_in(rebuild_database)
    assert len(rows) == 4, "three days ago to today"
    assert {row["wallet_id"] for row in rows} == {wallet_id}
    assert {row["confirmed"] for row in rows} == {RECEIVED}
    (finished,) = events_named(captured, "balance_rebuild_finished")
    assert finished["log_level"] == "info"
    assert (finished["wallets"], finished["days"]) == (1, 4)
    assert str(RECEIVED) not in repr(finished), "an amount must never reach a log line"
    assert DEFAULT_BITCOIN_ADDRESS not in repr(captured), "nor an address"
    assert events_named(captured, "balance_rebuild_incomplete") == []


async def test_an_incomplete_rebuild_is_a_warning_naming_the_wallet_and_reason(
    rebuild_database: Path, monkeypatch: pytest.MonkeyPatch, sleep: GatedSleep
) -> None:
    """Unproven: a warning with `wallet_id:reason`, nothing stored, no address or amount."""
    wallet_id = await a_wallet(rebuild_database)
    stub = HistoryStub(incomplete=HistoryIncomplete.COUNT_MISMATCH)
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: stub})
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            await sleep.parked()

    (incomplete,) = events_named(captured, "balance_rebuild_incomplete")
    assert incomplete["log_level"] == "warning"
    assert (incomplete["wallets"], incomplete["days"]) == (0, 0)
    assert incomplete["incomplete"] == (f"{wallet_id}:count_mismatch",)
    assert (incomplete["failed"], incomplete["unsupported"]) == ((), ())
    assert str(RECEIVED) not in repr(incomplete)
    assert DEFAULT_BITCOIN_ADDRESS not in repr(captured)
    assert events_named(captured, "balance_rebuild_finished") == []
    assert await rebuilt_in(rebuild_database) == []


async def test_a_failed_and_an_unsupported_wallet_are_listed_apart(
    rebuild_database: Path, monkeypatch: pytest.MonkeyPatch, sleep: GatedSleep
) -> None:
    """A chain nobody registered fails; a provider with no history reader is unsupported."""
    bitcoin = await a_wallet(rebuild_database)
    async with own_session(rebuild_database) as session:
        user_id = await insert_user(session, "second")
        kaspa = await insert_wallet(
            session, user_id=user_id, chain_key=ChainKey.KASPA, address=DEFAULT_KASPA_ADDRESS
        )
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN)})
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            await sleep.parked()

    (incomplete,) = events_named(captured, "balance_rebuild_incomplete")
    assert incomplete["unsupported"] == (f"{bitcoin}",)
    assert incomplete["failed"] == (f"{kaspa}:UnknownChainError",)
    assert incomplete["incomplete"] == ()


async def test_a_recent_rebuild_waits_out_the_rest_of_the_interval(
    rebuild_database: Path, monkeypatch: pytest.MonkeyPatch, sleep: GatedSleep
) -> None:
    """Rebuilt an hour ago: no history is read, and the first sleep is the 23 hours left."""
    wallet_id = await a_wallet(rebuild_database)
    await plant_a_rebuilt_day(
        rebuild_database, wallet_id, rebuilt_at=datetime.now(UTC) - timedelta(hours=1)
    )
    stub = HistoryStub()
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: stub})
    app = create_app()

    async with app.router.lifespan_context(app):
        await sleep.parked()

    assert stub.histories == []
    (delay,) = sleep.delays
    assert DAY_SECONDS - 3_700 < delay <= DAY_SECONDS - 3_500


async def test_the_last_run_is_the_newest_rebuilt_row(
    rebuild_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`latest_balance_rebuild`, the timer's `last_run_at`: `None`, then the newest row's.

    The timer is off here: the function reads the table over the lifespan's own sessions and
    needs no timer, and one left on would rebuild at startup and write a row of its own.
    """
    monkeypatch.setenv("PORTFOLIO_BALANCE_REBUILD_ENABLED", "false")
    get_settings.cache_clear()
    wallet_id = await a_wallet(rebuild_database)
    newer = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)
    app = create_app()

    async with app.router.lifespan_context(app):
        assert await latest_balance_rebuild(app) is None
        await plant_a_rebuilt_day(rebuild_database, wallet_id, rebuilt_at=newer)
        await plant_a_rebuilt_day(
            rebuild_database, wallet_id, rebuilt_at=newer - timedelta(days=6), day="2026-01-02"
        )
        assert await latest_balance_rebuild(app) == newer


async def test_the_rebuild_is_not_built_when_disabled(
    rebuild_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switched off: `None` on `app.state`, one `scheduler_disabled` line, no provider built."""
    monkeypatch.setenv("PORTFOLIO_BALANCE_REBUILD_ENABLED", "false")
    get_settings.cache_clear()
    await a_wallet(rebuild_database)
    registry = stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: HistoryStub()})
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            assert app.state.balance_rebuild_scheduler is None

    disabled = [entry["scheduler"] for entry in events_named(captured, "scheduler_disabled")]
    assert disabled.count("balance-rebuild") == 1
    assert registry.created == []
    assert await rebuilt_in(rebuild_database) == []
