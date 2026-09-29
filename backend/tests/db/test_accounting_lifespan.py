"""Criterion 3 of #19: the recompute runs after a sync that inserted fills, and once at startup.

`run_accounting_recompute(app, reason)` in `main.py` is the one trigger. It takes
`app.state.accounting_lock`, recomputes every owner over a session of its own, logs the
outcome, records it on `app.state.accounting_status`, and **never raises**. It is called in
two places, and each has its tests here:

* **once at startup**, as a background task the lifespan starts after the migrations and
  cancels on shutdown -- which must not delay readiness, because the deploy's health check
  would otherwise wait on a recompute over the owner's whole history;
* **inside `exchange_sync_runner`**, after the sync returned and before its summary is
  handed back, only when the run inserted fills -- and a failing recompute leaves that
  summary exactly as the sync returned it.

The sync is replaced by a fake whose summary the test chooses, because what is under test
is the runner's decision, not the sync; `tests/api/test_accounting.py` drives the real sync
end to end through the manual endpoint.

Every application here starts with all three timers off and an HTTP client that refuses
every request, for the reason `tests/db/test_lifespan.py` gives.
"""

from __future__ import annotations

import asyncio
import warnings
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import pytest
from anyio import to_thread
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from structlog.testing import capture_logs

from portfolio.config import get_settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import create_database_engine, create_session_factory
from portfolio.domain.exchanges import ExchangeKey
from portfolio.main import (
    ACCOUNTING_STARTUP_TASK_NAME,
    create_app,
    exchange_sync_runner,
    run_accounting_recompute,
)
from portfolio.repositories.exchange_sync_runs import (
    AccountOutcome,
    AccountOutcomeStatus,
    ExchangeSyncRunSummary,
)
from portfolio.repositories.sync_runs import SyncRunStatus, SyncTrigger
from portfolio.services.accounting import (
    AccountingService,
    AccountingStatus,
    RecomputeOutcome,
    RecomputeReason,
)
from tests.accounting_harness import (
    HEADER_SQL,
    at,
    plant_account,
    plant_fills,
    plant_owner,
    plant_unconvertible_fill,
    rows,
)
from tests.exchange_sync_harness import make_fill
from tests.offline_http import use_an_offline_http_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: How long any wait here may take before it is a failure rather than a slow machine.
BOUND: Final = 5

#: Distinctive text that must never reach a log line.
LEAKY_TRADE_ID: Final = "tid-9KX4-lifespan"
LEAKY_MESSAGE: Final = "message-sentinel-" + "Z8" * 6


@pytest.fixture
def accounting_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temporary database file, every timer off, and an offline HTTP client."""
    database_path = tmp_path / "accounting" / "portfolio.db"
    database_path.parent.mkdir()
    monkeypatch.setenv("PORTFOLIO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_PRICE_REFRESH_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_EXCHANGE_SYNC_ENABLED", "false")
    monkeypatch.delenv("PORTFOLIO_EXCHANGE_HISTORY_START", raising=False)
    monkeypatch.delenv("PORTFOLIO_BOOTSTRAP_PASSWORD", raising=False)
    # Cheap hashing: the lifespan warms the dummy hash, and the shipped cost is a quarter of
    # a second per application for a hash nothing here verifies.
    monkeypatch.setenv("PORTFOLIO_ARGON2_TIME_COST", "1")
    monkeypatch.setenv("PORTFOLIO_ARGON2_MEMORY_COST", "64")
    monkeypatch.setenv("PORTFOLIO_ARGON2_PARALLELISM", "1")
    use_an_offline_http_client(monkeypatch)
    get_settings.cache_clear()
    try:
        yield database_path
    finally:
        get_settings.cache_clear()


@asynccontextmanager
async def own_factory(database: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory over the file on an engine this test owns, migrated to head."""
    url = f"sqlite+aiosqlite:///{database.as_posix()}"
    await to_thread.run_sync(upgrade_to_head, url)
    engine = create_database_engine(url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def plant_owner_with_fills(database: Path, count: int = 3) -> int:
    """An owner, a Bitget account and `count` buys, before any application has started."""
    async with own_factory(database) as factory, factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(session, user_id)
        await plant_fills(session, account, [make_fill(1001 + n, at(n)) for n in range(count)])
    return user_id


async def headers_in(database: Path) -> list[dict[str, Any]]:
    async with own_factory(database) as factory:
        return await rows(factory, HEADER_SQL)


async def until(condition: Callable[[], bool]) -> None:
    """Poll every 10 ms; every caller bounds it with `wait_for` (see `test_lifespan.until`)."""
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


def status_of(app: FastAPI) -> AccountingStatus | None:
    status: AccountingStatus | None = app.state.accounting_status
    return status


async def startup_settled(app: FastAPI) -> AccountingStatus:
    """Wait for the startup recompute to record its outcome, and return it."""
    await asyncio.wait_for(until(lambda: status_of(app) is not None), timeout=BOUND)
    task: asyncio.Task[Any] = app.state.accounting_startup_task
    await asyncio.wait_for(until(task.done), timeout=BOUND)
    status = status_of(app)
    assert status is not None
    return status


class RecomputeCalls:
    """Wraps `AccountingService.recompute` and records each call's user, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.users: list[int] = []
        original = AccountingService.recompute
        calls = self

        async def recording(service: AccountingService, user_id: int) -> Any:
            calls.users.append(user_id)
            return await original(service, user_id)

        monkeypatch.setattr(AccountingService, "recompute", recording)


def summary_with(fills_inserted: int) -> ExchangeSyncRunSummary:
    """A finished run's summary, with the inserted count the test chooses."""
    started = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    return ExchangeSyncRunSummary(
        run_id=77,
        trigger=SyncTrigger.MANUAL,
        status=SyncRunStatus.SUCCESS,
        started_at=started,
        finished_at=started + timedelta(seconds=2),
        duration_ms=2000,
        accounts_total=1,
        accounts_succeeded=1,
        accounts_failed=0,
        accounts_skipped=0,
        accounts=(
            AccountOutcome(
                exchange_account_id=1,
                exchange_key=ExchangeKey.BITGET,
                status=AccountOutcomeStatus.SUCCESS,
                windows_completed=1,
                pages=1,
                fills_seen=fills_inserted,
                fills_inserted=fills_inserted,
            ),
        ),
    )


class FakeSync:
    """Stands in for the exchange sync service: returns the summary it was given."""

    def __init__(self, summary: ExchangeSyncRunSummary) -> None:
        self.summary = summary
        self.triggers: list[SyncTrigger] = []

    async def sync(self, trigger: SyncTrigger) -> ExchangeSyncRunSummary:
        self.triggers.append(trigger)
        return self.summary


def fake_the_sync(monkeypatch: pytest.MonkeyPatch, fake: FakeSync) -> None:
    """Hand the runner the fake wherever `main` builds the real service."""

    def build(session: object, **keywords: object) -> FakeSync:
        del session, keywords
        return fake

    monkeypatch.setattr("portfolio.main.build_exchange_sync_service", build)


def events_named(captured: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [entry for entry in captured if entry["event"] == name]


# --------------------------------------------------------------------------------------
# Once at startup
# --------------------------------------------------------------------------------------


async def test_the_startup_recompute_writes_a_snapshot_over_the_fills_already_stored(
    accounting_database: Path,
) -> None:
    """The first deploy over existing fills: a snapshot, a status, and a log line with timing."""
    user_id = await plant_owner_with_fills(accounting_database, count=3)
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            status = await startup_settled(app)

    assert status.outcome is RecomputeOutcome.WRITTEN
    assert status.error is None
    assert status.at.tzinfo is not None
    assert abs(datetime.now(UTC) - status.at) < timedelta(minutes=1)
    headers = await headers_in(accounting_database)
    assert [(row["user_id"], row["event_count"]) for row in headers] == [(user_id, 3)]
    (finished,) = events_named(captured, "accounting_recompute_finished")
    assert finished["reason"] == "startup"
    assert str(finished["outcome"]) == "written"
    assert finished["event_count"] == 3
    assert isinstance(finished["duration_ms"], int)
    assert finished["duration_ms"] >= 0
    assert events_named(captured, "accounting_recompute_failed") == []


async def test_the_startup_recompute_runs_exactly_once(
    accounting_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One recompute per owner at startup, on a task named for it, finished and not repeated."""
    user_id = await plant_owner_with_fills(accounting_database)
    calls = RecomputeCalls(monkeypatch)
    app = create_app()

    async with app.router.lifespan_context(app):
        await startup_settled(app)
        task: asyncio.Task[Any] = app.state.accounting_startup_task
        # Long enough for a second, unwanted run to have started if anything scheduled one.
        await asyncio.sleep(0.2)
        assert calls.users == [user_id]
        assert task.get_name() == ACCOUNTING_STARTUP_TASK_NAME == "accounting-startup-recompute"
        assert task.done()
        assert not task.cancelled()

    assert calls.users == [user_id]


async def test_a_second_start_over_the_same_fills_is_unchanged(accounting_database: Path) -> None:
    """A restart recomputes, finds the fingerprint it stored, and writes nothing."""
    await plant_owner_with_fills(accounting_database)
    first = create_app()
    async with first.router.lifespan_context(first):
        await startup_settled(first)
    (before,) = await headers_in(accounting_database)

    second = create_app()
    async with second.router.lifespan_context(second):
        status = await startup_settled(second)

    assert status.outcome is RecomputeOutcome.UNCHANGED
    assert await headers_in(accounting_database) == [before]


async def test_every_owner_is_recomputed(accounting_database: Path) -> None:
    """The loop over users: two owners, two snapshots, and the logged count is their sum."""
    first = await plant_owner_with_fills(accounting_database, count=2)
    async with own_factory(accounting_database) as factory, factory() as session:
        second = await plant_owner(session, "second-owner")
        account = await plant_account(session, second, ExchangeKey.BINGX)
        await plant_fills(session, account, [make_fill(4001 + n, at(n)) for n in range(5)])
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            await startup_settled(app)

    counts = {row["user_id"]: row["event_count"] for row in await headers_in(accounting_database)}
    assert counts == {first: 2, second: 5}
    (finished,) = events_named(captured, "accounting_recompute_finished")
    assert finished["event_count"] == 7


@pytest.mark.parametrize(
    "failure",
    ["a write the column refuses", "a row that does not convert"],
)
async def test_one_owners_failure_neither_stops_nor_undoes_anothers(
    accounting_database: Path, failure: str
) -> None:
    """Each owner in their own transaction, over one session (spec 021, *The trigger*).

    The first owner's recompute fails -- at the write, with a basis of 1.8E20 that
    `NumericText(18)` refuses after the old header's `DELETE` has already run, or before it,
    on a stored row that does not convert. Either way their previous snapshot stands, and
    the second owner's new fill is still written in the same run.
    """
    first = await plant_owner_with_fills(accounting_database, count=2)
    async with own_factory(accounting_database) as factory, factory() as session:
        second = await plant_owner(session, "second-owner")
        second_account = await plant_account(session, second, ExchangeKey.BINGX)
        await plant_fills(session, second_account, [make_fill(4001 + n, at(n)) for n in range(3)])

    async with settled_app(accounting_database) as app:
        before = {row["user_id"]: row for row in await headers_in(accounting_database)}
        async with own_factory(accounting_database) as factory, factory() as session:
            first_account = int(
                await session.scalar(
                    text("SELECT id FROM exchange_accounts WHERE user_id = :user"),
                    {"user": first},
                )
            )
            if failure == "a write the column refuses":
                absurd = "90000000000000000000"
                await plant_fills(
                    session,
                    first_account,
                    [
                        make_fill(
                            1999,
                            at(70),
                            quantity="1",
                            price=absurd,
                            quote_quantity=absurd,
                            fee_amount=absurd,
                            fee_asset="USDT",
                        )
                    ],
                )
            else:
                await plant_unconvertible_fill(
                    session, first_account, trade_id=LEAKY_TRADE_ID, shape="same_asset"
                )
            await plant_fills(session, second_account, [make_fill(4999, at(80))])
        status = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
        after = {row["user_id"]: row for row in await headers_in(accounting_database)}

    assert status.outcome is RecomputeOutcome.FAILED
    assert after[first] == before[first], "the failing owner's snapshot is untouched"
    assert (before[second]["event_count"], after[second]["event_count"]) == (3, 4)


async def test_one_owner_written_and_another_unchanged_is_a_written_run(
    accounting_database: Path,
) -> None:
    """The run's outcome is `written` when any owner's snapshot was, not only when all were."""
    first = await plant_owner_with_fills(accounting_database, count=2)
    async with own_factory(accounting_database) as factory, factory() as session:
        second = await plant_owner(session, "second-owner")
        second_account = await plant_account(session, second, ExchangeKey.BINGX)
        await plant_fills(session, second_account, [make_fill(4001, at(1))])

    async with settled_app(accounting_database) as app:
        before = {row["user_id"]: row for row in await headers_in(accounting_database)}
        async with own_factory(accounting_database) as factory, factory() as session:
            await plant_fills(session, second_account, [make_fill(4002, at(2))])
        with capture_logs() as captured:
            status = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
        after = {row["user_id"]: row for row in await headers_in(accounting_database)}

    assert status.outcome is RecomputeOutcome.WRITTEN
    assert after[first] == before[first], "the first owner's snapshot was unchanged"
    assert after[second]["event_count"] == 2
    (finished,) = events_named(captured, "accounting_recompute_finished")
    assert (str(finished["outcome"]), finished["event_count"]) == ("written", 4)


async def test_the_first_owners_failure_is_the_one_reported(accounting_database: Path) -> None:
    """Two owners failing differently: the status and the log name the first failure's class."""
    first = await plant_owner_with_fills(accounting_database, count=1)
    async with own_factory(accounting_database) as factory, factory() as session:
        second = await plant_owner(session, "second-owner")
        second_account = await plant_account(session, second, ExchangeKey.BINGX)
        await plant_fills(session, second_account, [make_fill(4001, at(1))])

    async with settled_app(accounting_database) as app:
        async with own_factory(accounting_database) as factory, factory() as session:
            first_account = int(
                await session.scalar(
                    text("SELECT id FROM exchange_accounts WHERE user_id = :user"),
                    {"user": first},
                )
            )
            await plant_unconvertible_fill(
                session, first_account, trade_id=LEAKY_TRADE_ID, shape="same_asset"
            )
            absurd = "90000000000000000000"
            await plant_fills(
                session,
                second_account,
                [
                    make_fill(
                        4999,
                        at(9),
                        quantity="1",
                        price=absurd,
                        quote_quantity=absurd,
                        fee_amount=absurd,
                        fee_asset="USDT",
                    )
                ],
            )
        with capture_logs() as captured:
            status = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)

    assert (status.outcome, status.error) == (RecomputeOutcome.FAILED, "UnconvertibleFillError")
    (failed,) = events_named(captured, "accounting_recompute_failed")
    assert failed["error"] == "UnconvertibleFillError"


async def test_a_startup_recompute_never_delays_readiness_and_is_cancelled_at_shutdown(
    accounting_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recompute that never finishes: the application still comes up and still goes down.

    The health check answers while the recompute is stuck, which is what the deploy waits
    on; shutdown cancels the task and waits for it, so nothing is left pending, nothing is
    recorded as a failure, and no "task was destroyed" warning is emitted.
    """
    await plant_owner_with_fills(accounting_database)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def stuck(service: AccountingService, user_id: int) -> Any:
        del service, user_id
        entered.set()
        await never.wait()

    monkeypatch.setattr(AccountingService, "recompute", stuck)
    app = create_app()

    with capture_logs() as captured, warnings.catch_warnings(record=True) as warned:
        warnings.simplefilter("always")
        context = app.router.lifespan_context(app)
        await asyncio.wait_for(context.__aenter__(), timeout=BOUND)
        try:
            await asyncio.wait_for(entered.wait(), timeout=BOUND)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                health = await asyncio.wait_for(client.get("/api/health"), timeout=BOUND)
            task: asyncio.Task[Any] = app.state.accounting_startup_task
            assert not task.done(), "the recompute is still stuck while the app answers"
        finally:
            await asyncio.wait_for(context.__aexit__(None, None, None), timeout=BOUND)

    assert health.status_code == 200
    assert task.cancelled()
    assert status_of(app) is None, "a cancelled recompute is not recorded as an outcome"
    assert events_named(captured, "accounting_recompute_failed") == []
    assert [str(entry.message) for entry in warned if "destroyed" in str(entry.message)] == []


async def test_a_failing_startup_recompute_does_not_fail_the_startup(
    accounting_database: Path,
) -> None:
    """An unconvertible stored row: startup completes, the status says why, the log names it."""
    async with own_factory(accounting_database) as factory, factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(session, user_id)
        await plant_unconvertible_fill(
            session, account, trade_id=LEAKY_TRADE_ID, shape="fee_consumes_received"
        )
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            status = await startup_settled(app)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                health = await client.get("/api/health")

    assert health.status_code == 200
    assert status.outcome is RecomputeOutcome.FAILED
    assert status.error == "UnconvertibleFillError"
    assert await headers_in(accounting_database) == []
    (failed,) = events_named(captured, "accounting_recompute_failed")
    assert failed["reason"] == "startup"
    assert failed["error"] == "UnconvertibleFillError"
    assert "exc_info" not in failed
    assert LEAKY_TRADE_ID not in repr(captured)


# --------------------------------------------------------------------------------------
# After an exchange sync that inserted fills
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def settled_app(database: Path) -> AsyncIterator[FastAPI]:
    """An application whose startup recompute has already finished."""
    app = create_app()
    async with app.router.lifespan_context(app):
        await startup_settled(app)
        yield app
    del database


async def test_a_sync_that_inserted_fills_recomputes_before_handing_back_its_summary(
    accounting_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary comes back unchanged, and by then the new snapshot is on disk."""
    user_id = await plant_owner_with_fills(accounting_database, count=2)
    fake = FakeSync(summary_with(fills_inserted=3))
    fake_the_sync(monkeypatch, fake)

    async with settled_app(accounting_database) as app:
        async with own_factory(accounting_database) as factory, factory() as session:
            account = await plant_account(session, user_id, ExchangeKey.BINGX)
            await plant_fills(session, account, [make_fill(3001 + n, at(10 + n)) for n in range(3)])
        calls = RecomputeCalls(monkeypatch)
        runner = exchange_sync_runner(app, {}, get_settings())
        with capture_logs() as captured:
            summary = await runner(SyncTrigger.MANUAL)
        headers = await headers_in(accounting_database)
        status = status_of(app)

    assert summary is fake.summary
    assert fake.triggers == [SyncTrigger.MANUAL]
    assert calls.users == [user_id]
    assert [row["event_count"] for row in headers] == [5], "recomputed before the return"
    assert status is not None
    assert status.outcome is RecomputeOutcome.WRITTEN
    (finished,) = events_named(captured, "accounting_recompute_finished")
    assert finished["reason"] == str(RecomputeReason.EXCHANGE_SYNC) == "exchange_sync"


async def test_a_sync_that_inserted_nothing_does_not_recompute(
    accounting_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await plant_owner_with_fills(accounting_database)
    fake = FakeSync(summary_with(fills_inserted=0))
    fake_the_sync(monkeypatch, fake)

    async with settled_app(accounting_database) as app:
        startup_status = status_of(app)
        calls = RecomputeCalls(monkeypatch)
        runner = exchange_sync_runner(app, {}, get_settings())
        with capture_logs() as captured:
            summary = await runner(SyncTrigger.SCHEDULED)
        status = status_of(app)

    assert summary is fake.summary
    assert calls.users == []
    assert status is startup_status, "the status still describes the startup recompute"
    assert events_named(captured, "accounting_recompute_finished") == []


async def test_a_failing_recompute_leaves_the_sync_summary_intact_and_logs_the_class_only(
    accounting_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recompute raises with a message that must not travel: the sync's result is untouched."""
    await plant_owner_with_fills(accounting_database)
    fake = FakeSync(summary_with(fills_inserted=1))
    fake_the_sync(monkeypatch, fake)

    async def broken(service: AccountingService, user_id: int) -> Any:
        del service, user_id
        raise RuntimeError(LEAKY_MESSAGE)

    async with settled_app(accounting_database) as app:
        monkeypatch.setattr(AccountingService, "recompute", broken)
        runner = exchange_sync_runner(app, {}, get_settings())
        with capture_logs() as captured:
            summary = await runner(SyncTrigger.MANUAL)
        status = status_of(app)

    assert summary is fake.summary
    assert summary == summary_with(fills_inserted=1)
    assert status is not None
    assert (status.outcome, status.error) == (RecomputeOutcome.FAILED, "RuntimeError")
    (failed,) = events_named(captured, "accounting_recompute_failed")
    assert (failed["reason"], failed["error"]) == ("exchange_sync", "RuntimeError")
    assert LEAKY_MESSAGE not in repr(captured)


# --------------------------------------------------------------------------------------
# The trigger itself: never raises, records its outcome, and is serialised
# --------------------------------------------------------------------------------------


async def test_the_trigger_returns_and_records_its_status(accounting_database: Path) -> None:
    await plant_owner_with_fills(accounting_database)

    async with settled_app(accounting_database) as app:
        returned = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
        recorded = status_of(app)

    assert isinstance(returned, AccountingStatus)
    assert recorded is returned
    assert returned.outcome is RecomputeOutcome.UNCHANGED, "startup already wrote it"
    assert returned.error is None


async def test_the_trigger_never_raises(
    accounting_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any `Exception` out of the recompute is an outcome, not a crash of the caller."""
    await plant_owner_with_fills(accounting_database)

    async def broken(service: AccountingService, user_id: int) -> Any:
        del service, user_id
        raise LookupError(LEAKY_MESSAGE)

    async with settled_app(accounting_database) as app:
        monkeypatch.setattr(AccountingService, "recompute", broken)
        returned = await run_accounting_recompute(app, RecomputeReason.STARTUP)

    assert (returned.outcome, returned.error) == (RecomputeOutcome.FAILED, "LookupError")


async def test_concurrent_triggers_are_serialised_by_the_lock(
    accounting_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two triggers at once: the second recompute starts only after the first has finished."""
    await plant_owner_with_fills(accounting_database)
    trace: list[str] = []
    original = AccountingService.recompute

    async def traced(service: AccountingService, user_id: int) -> Any:
        label = f"run{len([entry for entry in trace if entry.startswith('enter')]) + 1}"
        trace.append(f"enter {label}")
        # Several real yields: without the lock the other trigger would enter here.
        for _ in range(5):
            await asyncio.sleep(0.01)
        result = await original(service, user_id)
        trace.append(f"exit {label}")
        return result

    async with settled_app(accounting_database) as app:
        assert isinstance(app.state.accounting_lock, asyncio.Lock)
        monkeypatch.setattr(AccountingService, "recompute", traced)
        first, second = await asyncio.wait_for(
            asyncio.gather(
                run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC),
                run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC),
            ),
            timeout=BOUND,
        )

    assert trace == ["enter run1", "exit run1", "enter run2", "exit run2"]
    assert first.outcome is second.outcome is RecomputeOutcome.UNCHANGED


async def test_the_trigger_waits_for_the_published_lock(
    accounting_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is `app.state.accounting_lock` that is taken, not a lock of its own."""
    await plant_owner_with_fills(accounting_database)

    async with settled_app(accounting_database) as app:
        calls = RecomputeCalls(monkeypatch)
        lock: asyncio.Lock = app.state.accounting_lock
        await lock.acquire()
        try:
            pending = asyncio.create_task(
                run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
            )
            await asyncio.sleep(0.1)
            assert calls.users == [], "the recompute started while the lock was held"
            assert not pending.done()
        finally:
            lock.release()
        status = await asyncio.wait_for(pending, timeout=BOUND)

    assert len(calls.users) == 1
    assert status.outcome is RecomputeOutcome.UNCHANGED


def test_the_lock_and_the_status_exist_without_the_lifespan() -> None:
    """Installed by `create_app`, so a router or a test never meets a missing attribute."""
    app = create_app()

    assert isinstance(app.state.accounting_lock, asyncio.Lock)
    assert app.state.accounting_status is None
