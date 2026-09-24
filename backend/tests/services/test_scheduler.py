"""Criterion 1: the interval loop, its default, and the run it does or does not do at startup.

A scheduler is four decisions and each one has a way of being wrong that says nothing at the
time:

| Decision | What a wrong answer looks like in production |
|---|---|
| how long between ticks | a loop with no sleep against a public API, and a ban |
| whether to run at startup | a dashboard blank for a quarter hour, or two API calls |
|  | on every crash-loop restart |
| what to do when a run raises | the schedule stops, silently, until a restart |
| when to stop | a task still syncing while the engine is being disposed |

**Nothing here sleeps.** The sleep is injected and is an event a test releases by hand, so
every assertion is about how many ticks happened rather than about how long they took. The
alternative -- a real interval of a few milliseconds -- makes the verdict a measurement of
the host, which is the standard `tests/providers/harness.py` sets and the reason its clock
is a counter a test moves.

`test_a_running_row_from_a_dead_process_is_swept_to_interrupted` is named in the spec's test
plan against this module, and it lives in two better places instead: the sweep itself is
`tests/db/test_balances_repository.py`, because `sweep_interrupted` is a repository method,
and the fact that startup performs it before the scheduler starts is
`tests/db/test_lifespan.py`, because that ordering is the lifespan's. Neither is this
module's: the scheduler never touches the table.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.config import Settings
from portfolio.domain.chains import ChainKey
from portfolio.repositories.sync_runs import SyncRunStatus, SyncRunSummary, SyncTrigger
from portfolio.services.balance_sync import build_balance_sync_service
from portfolio.services.scheduler import (
    SECONDS_PER_MINUTE,
    BalanceSyncScheduler,
    build_balance_scheduler,
)
from portfolio.services.sync_coordinator import SyncCoordinator
from tests.balance_harness import (
    DEFAULT_BITCOIN_ADDRESS,
    StubChainProvider,
    plant_wallets,
    snapshots,
    sync_runs,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW: Final = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

#: The issue's default, in the unit the setting is written in.
DEFAULT_INTERVAL_MINUTES: Final = 15

BTC_UNITS: Final = 123_456_789

#: A bound on a wait for something a test expects to happen at once. Reached only when the
#: behaviour is wrong, and then it turns a hang into a readable failure.
DEADLOCK_TIMEOUT: Final = 5


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A migrated file, for the one test that asserts the loop persists something."""
    async with migrated_sessionmaker(tmp_path) as factory:
        yield factory


class PacedSleep:
    """The injected sleep. It records what it was asked to wait and waits for the test.

    Backpressure is the point. A sleep that simply returned would let the loop spin
    thousands of times before the test regained control, and "two ticks happened" would be
    a statement about scheduling luck. Here the loop stops at every sleep until `step()`
    releases it, so the number of ticks is exactly the number the test asked for.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._arrived = asyncio.Event()
        self._resume = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self._arrived.set()
        await self._resume.wait()
        self._resume.clear()

    async def reached(self) -> None:
        """Wait until the loop is parked in a sleep, so a tick can be counted."""
        await asyncio.wait_for(self._arrived.wait(), timeout=DEADLOCK_TIMEOUT)
        self._arrived.clear()

    async def step(self) -> None:
        """Let the loop out of the sleep it is parked in."""
        await self.reached()
        self._resume.set()
        await asyncio.sleep(0)


class CountingRunner:
    """The sync itself, as a counting stub, wrapped in the **real** coordinator.

    Wrapped rather than replaced: the coordinator is a small object proven in
    `test_sync_coordinator.py`, and a second stand-in for it here would be a second thing
    that could disagree with the scheduler about what `sync` returns. What this module needs
    to control is the *work*, and that is the runner.

    `fail_times` makes the first N ticks raise, which is how a loop that dies on an
    exception is told apart from one that carries on.
    """

    def __init__(self, *, raises: BaseException | None = None, fail_times: int = 0) -> None:
        self.triggers: list[SyncTrigger] = []
        self.raises = raises
        self.fail_times = fail_times

    async def __call__(self, trigger: SyncTrigger) -> SyncRunSummary:
        self.triggers.append(trigger)
        if self.raises is not None and len(self.triggers) <= self.fail_times:
            raise self.raises
        return SyncRunSummary(
            run_id=len(self.triggers),
            trigger=trigger,
            status=SyncRunStatus.SUCCESS,
            started_at=NOW,
            finished_at=NOW,
            duration_ms=1,
            wallets_total=0,
            wallets_succeeded=0,
            wallets_failed=0,
            chains=(),
        )


def finished_at(moment: datetime | None) -> Callable[[], Awaitable[datetime | None]]:
    """A `latest_finished_at` callable answering one fixed instant, or never having run."""

    async def answer() -> datetime | None:
        return moment

    return answer


def scheduler_over(
    coordinator: SyncCoordinator,
    sleep: PacedSleep,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_MINUTES * SECONDS_PER_MINUTE,
    latest: datetime | None = None,
) -> BalanceSyncScheduler:
    """The scheduler with its clock, its sleep and its startup query all injected."""
    return BalanceSyncScheduler(
        coordinator=coordinator,
        interval_seconds=interval_seconds,
        latest_finished_at=finished_at(latest),
        clock=lambda: NOW,
        sleep=sleep,
    )


# --------------------------------------------------------------------------------------
# Criterion 1: once per interval
# --------------------------------------------------------------------------------------


async def test_the_loop_runs_once_per_interval_and_persists_snapshots(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 1 end to end: the schedule, the real sync, and rows on disk afterwards.

    The only test in this module that wires the real service underneath, because criterion 1
    says "and persists snapshots" and a counting stub cannot answer that half. Two ticks
    rather than one: a scheduler that ran once and then fell out of its loop would satisfy a
    single-tick assertion, and that is the failure a `while` written as an `if` produces.
    """
    await plant_wallets(sessions, kaspa=())
    provider = StubChainProvider(ChainKey.BITCOIN, {DEFAULT_BITCOIN_ADDRESS: BTC_UNITS})

    async def run(trigger: SyncTrigger) -> SyncRunSummary:
        async with sessions() as session:
            service = build_balance_sync_service(
                session,
                provider_for=lambda _key: provider,
            )
            return await service.sync(trigger)

    sleep = PacedSleep()
    coordinator = SyncCoordinator(run)
    scheduler = scheduler_over(coordinator, sleep)

    await scheduler.start()
    await sleep.step()  # the startup run has happened; let the first interval elapse
    await sleep.reached()  # the second run has happened and the loop is parked again
    await scheduler.stop()

    runs = await sync_runs(sessions)
    stored = await snapshots(sessions)
    assert len(runs) == 2, "two ticks, two runs"
    assert [row["trigger"] for row in runs] == [SyncTrigger.STARTUP, SyncTrigger.SCHEDULED]
    assert len(stored) == 2, "each tick persists a snapshot; that is what the schedule is for"
    assert {row["confirmed"] for row in stored} == {BTC_UNITS}
    assert provider.calls == [(DEFAULT_BITCOIN_ADDRESS,)] * 2


async def test_the_interval_it_sleeps_is_the_one_it_was_given() -> None:
    """The delay handed to `sleep` is the configured interval, in seconds.

    Asserted on the argument rather than on elapsed time, which is the only way to say
    anything exact about a duration without waiting for it.
    """
    sleep = PacedSleep()
    runner = CountingRunner()
    coordinator = SyncCoordinator(runner)
    scheduler = scheduler_over(coordinator, sleep, interval_seconds=900)

    await scheduler.start()
    await sleep.step()
    await sleep.reached()
    await scheduler.stop()

    assert sleep.delays == [900, 900]


async def test_the_interval_comes_from_settings() -> None:
    """`build_balance_scheduler` converts minutes to seconds, in one place.

    The default is the issue's fifteen minutes and it is read off `Settings` rather than
    written here, so a changed default fails this test instead of silently disagreeing with
    the documentation. A second, non-default value is built as well: a converter that
    ignored its input and returned 900 would pass the first assertion alone.
    """
    sleep = PacedSleep()
    assert Settings().balance_sync_interval_minutes == DEFAULT_INTERVAL_MINUTES
    assert SECONDS_PER_MINUTE == 60

    scheduler = build_balance_scheduler(
        Settings(balance_sync_interval_minutes=7),
        coordinator=SyncCoordinator(CountingRunner()),
        latest_finished_at=finished_at(None),
        clock=lambda: NOW,
        sleep=sleep,
    )

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert sleep.delays == [7 * SECONDS_PER_MINUTE]


# --------------------------------------------------------------------------------------
# Criterion 1: the run at startup, and the one it does not do
# --------------------------------------------------------------------------------------


async def test_a_fresh_database_syncs_at_startup() -> None:
    """Nothing has ever run, so the first tick is now rather than in fifteen minutes.

    Sleeping first is the obvious loop and it leaves a fresh deployment blank for a quarter
    of an hour with nothing to say why. The trigger is `startup`, not `scheduled`: an
    operator looking at why two vendors were called at 03:04 should be able to see that the
    container had just booted.
    """
    sleep = PacedSleep()
    runner = CountingRunner()
    coordinator = SyncCoordinator(runner)
    scheduler = scheduler_over(coordinator, sleep, latest=None)

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert runner.triggers == [SyncTrigger.STARTUP]


async def test_a_recent_run_is_not_repeated_on_restart() -> None:
    """A run one minute old and a fifteen-minute interval: the container restarts quietly.

    This is the half that protects a public API from a crash-looping container. Without it
    every restart is two vendor calls, and a container that restarts every thirty seconds
    is a small denial of service aimed at somebody who volunteered to run an index.
    """
    sleep = PacedSleep()
    runner = CountingRunner()
    coordinator = SyncCoordinator(runner)
    scheduler = scheduler_over(coordinator, sleep, latest=NOW - timedelta(minutes=1))

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert runner.triggers == [], "a fresh run must not be repeated at startup"


async def test_a_run_older_than_one_interval_is_repeated_on_restart() -> None:
    """The control for the test above: the condition has to be able to say yes.

    Without this, a scheduler that never ran at startup at all would satisfy
    `test_a_recent_run_is_not_repeated_on_restart` perfectly, and a deployment left for an
    hour would come back up and wait another fifteen minutes before reading anything.
    """
    sleep = PacedSleep()
    runner = CountingRunner()
    coordinator = SyncCoordinator(runner)
    scheduler = scheduler_over(coordinator, sleep, latest=NOW - timedelta(minutes=30))

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert runner.triggers == [SyncTrigger.STARTUP]


async def test_the_startup_condition_is_measured_against_the_interval_not_a_constant() -> None:
    """The same age, two intervals, two answers. Fifteen minutes old is the boundary case.

    A condition hard-coded to fifteen minutes would pass every test above and would ignore
    the setting entirely -- an operator who set the interval to an hour would still get a
    sync on every restart.
    """
    age = NOW - timedelta(minutes=20)
    short = CountingRunner()
    long = CountingRunner()

    for runner, interval in ((short, 15), (long, 60)):
        sleep = PacedSleep()
        scheduler = scheduler_over(
            SyncCoordinator(runner),
            sleep,
            interval_seconds=interval * SECONDS_PER_MINUTE,
            latest=age,
        )
        await scheduler.start()
        await sleep.reached()
        await scheduler.stop()

    assert short.triggers == [SyncTrigger.STARTUP], "20 minutes is older than a 15 minute interval"
    assert long.triggers == [], "20 minutes is younger than a 60 minute interval"


# --------------------------------------------------------------------------------------
# Criterion 5's half that belongs to the scheduler: it keeps going, and it stops
# --------------------------------------------------------------------------------------


async def test_a_failing_run_does_not_end_the_loop() -> None:
    """A tick that raises is a tick, not the end of the schedule.

    The sync service is written not to raise -- every chain returns an outcome -- so the
    exception that reaches here is the one nothing anticipated: the database is locked, the
    session cannot commit. A loop that let it out stops syncing until somebody notices the
    dashboard has been frozen for a day, and nothing in any log says the schedule died.

    The second tick succeeding is what proves the loop survived rather than merely that the
    exception was swallowed somewhere.
    """
    sleep = PacedSleep()
    runner = CountingRunner(raises=RuntimeError("the database is locked"), fail_times=1)
    coordinator = SyncCoordinator(runner)
    scheduler = scheduler_over(coordinator, sleep, latest=None)

    await scheduler.start()
    await sleep.step()
    await sleep.reached()
    await scheduler.stop()

    assert runner.triggers == [SyncTrigger.STARTUP, SyncTrigger.SCHEDULED]


async def test_stopping_ends_the_loop_and_a_second_stop_is_harmless() -> None:
    """Criterion 5 from the scheduler's side: `stop` returns with the task really gone.

    Idempotent because the lifespan is not the only caller: a failed startup unwinds
    through the same `finally`, and a `stop` that raised the second time would turn one
    failure into two with the second one on top of the traceback that mattered.
    """
    sleep = PacedSleep()
    runner = CountingRunner()
    coordinator = SyncCoordinator(runner)
    scheduler = scheduler_over(coordinator, sleep, latest=None)

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()
    await scheduler.stop()

    before = len(runner.triggers)
    await asyncio.sleep(0)
    assert len(runner.triggers) == before, "a stopped scheduler must not tick again"


async def test_stopping_a_scheduler_that_never_started_is_harmless() -> None:
    """The shutdown path a failed startup takes, where `start` may never have been reached."""
    scheduler = scheduler_over(SyncCoordinator(CountingRunner()), PacedSleep(), latest=None)

    await scheduler.stop()
