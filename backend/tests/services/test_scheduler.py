"""Criterion 1: the interval loop, its default, and the run it does or does not do at startup.

One `IntervalScheduler` serves both of #10's timers -- the balance sync and, since the spec
regained it, the price refresh -- so everything here is asserted once and both timers
inherit it. What differs between them is the work and the `name`, and `name` is not
decoration: it is what makes two `scheduler_tick_failed` lines in one process attributable
to the right timer.

A scheduler is five decisions and each one has a way of being wrong that says nothing at the
time:

| Decision | What a wrong answer looks like in production |
|---|---|
| how long between ticks | a loop with no sleep against a public index, and a ban |
| whether to run at startup | a dashboard blank for a quarter hour, or two API calls |
|  | on every crash-loop restart |
| what to do when a tick raises | the schedule stops, silently, until a restart |
| what to do when the startup check raises | a hammered vendor while the database is down |
| when to stop | a task still syncing while the engine is being disposed |

**Nothing here sleeps.** The sleep is injected and is an event a test releases by hand, so
every assertion is about how many ticks happened rather than about how long they took. The
alternative -- a real interval of a few milliseconds -- makes the verdict a measurement of
the host, which is the standard `tests/providers/harness.py` sets and the reason its clock
is a counter a test moves.

`test_a_running_row_from_a_dead_process_is_swept_to_interrupted` is named in the spec's test
plan against this module, and it lives in two better places instead: the sweep itself is
`tests/db/test_sync_runs_repository.py`, because `sweep_interrupted` is a repository method,
and the fact that startup performs it before the scheduler starts is
`tests/db/test_lifespan.py`, because that ordering is the lifespan's. Neither is this
module's: the scheduler never touches a table.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.config import Settings
from portfolio.domain.chains import ChainKey
from portfolio.repositories.sync_runs import SyncTrigger
from portfolio.services.balance_sync import build_balance_sync_service
from portfolio.services.scheduler import SECONDS_PER_MINUTE, IntervalScheduler
from portfolio.services.sync_coordinator import SyncCoordinator
from tests.balance_harness import (
    DEFAULT_BITCOIN_ADDRESS,
    PacedSleep,
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

    from portfolio.repositories.sync_runs import SyncRunSummary

NOW: Final = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

#: The issue's own default for the balance sync, in the unit the setting is written in.
DEFAULT_BALANCE_INTERVAL_MINUTES: Final = 15

#: The price refresh's, matching `services/prices.py::STALE_AFTER`.
DEFAULT_PRICE_INTERVAL_MINUTES: Final = 60

BALANCE_SYNC: Final = "balance-sync"
PRICE_REFRESH: Final = "price-refresh"

BTC_UNITS: Final = 123_456_789

#: A bound on a wait for something a test expects to happen at once. Reached only when the
#: behaviour is wrong, and then it turns a hang into a readable failure.
DEADLOCK_TIMEOUT: Final = 5


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A migrated file, for the one test that asserts the loop persists something."""
    async with migrated_sessionmaker(tmp_path) as factory:
        yield factory


class RecordingRun:
    """The work, as a stub that counts its ticks and can be told to fail.

    `at_startup` is recorded rather than discarded: it is the one argument the scheduler
    passes, and the balance timer turns it into the difference between a `startup` run and a
    `scheduled` one -- which is what lets an operator tell a container that had just booted
    from one that had been up for a week.
    """

    def __init__(self, *, raises: BaseException | None = None, fail_times: int = 0) -> None:
        self.ticks: list[bool] = []
        self.raises = raises
        self.fail_times = fail_times

    async def __call__(self, at_startup: bool) -> None:
        self.ticks.append(at_startup)
        if self.raises is not None and len(self.ticks) <= self.fail_times:
            raise self.raises


def last_run_at(
    moment: datetime | None,
    *,
    raises: BaseException | None = None,
) -> Callable[[], Awaitable[datetime | None]]:
    """The startup query, answering one fixed instant -- or failing, which is its own case."""

    async def answer() -> datetime | None:
        if raises is not None:
            raise raises
        return moment

    return answer


def scheduler_over(
    run: RecordingRun,
    sleep: PacedSleep,
    *,
    interval_minutes: int = DEFAULT_BALANCE_INTERVAL_MINUTES,
    last: datetime | None = None,
    name: str = BALANCE_SYNC,
    startup_check_raises: BaseException | None = None,
) -> IntervalScheduler:
    """The scheduler with its clock, its sleep and its startup query all injected."""
    return IntervalScheduler(
        name=name,
        interval_minutes=interval_minutes,
        last_run_at=last_run_at(last, raises=startup_check_raises),
        run=run,
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

    The only test in this module that wires a real service underneath, because criterion 1
    says "and persists snapshots" and a counting stub cannot answer that half. Two ticks
    rather than one: a scheduler that ran once and then fell out of its loop would satisfy a
    single-tick assertion, and that is the failure a `while` written as an `if` produces.

    The `at_startup` flag is turned into a trigger here exactly as the lifespan turns it into
    one, which is why the two rows differ in that column.
    """
    await plant_wallets(sessions, kaspa=())
    provider = StubChainProvider(ChainKey.BITCOIN, {DEFAULT_BITCOIN_ADDRESS: BTC_UNITS})

    async def run_a_sync(trigger: SyncTrigger) -> SyncRunSummary:
        async with sessions() as session:
            service = build_balance_sync_service(session, provider_for=lambda _key: provider)
            return await service.sync(trigger)

    coordinator = SyncCoordinator(run_a_sync)

    async def tick(at_startup: bool) -> None:
        await coordinator.sync(SyncTrigger.STARTUP if at_startup else SyncTrigger.SCHEDULED)

    sleep = PacedSleep()
    scheduler = IntervalScheduler(
        name=BALANCE_SYNC,
        interval_minutes=DEFAULT_BALANCE_INTERVAL_MINUTES,
        last_run_at=last_run_at(None),
        run=tick,
        clock=lambda: NOW,
        sleep=sleep,
    )

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


async def test_the_interval_it_sleeps_is_the_configured_minutes_in_seconds() -> None:
    """Minutes in, seconds to the sleep, converted in one place.

    Asserted on the argument rather than on elapsed time, which is the only way to say
    anything exact about a duration without waiting for it. Two sleeps, because a converter
    applied once at construction and then forgotten would still pass a single-delay check.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = scheduler_over(run, sleep, interval_minutes=15)

    assert scheduler.interval_seconds == 15 * SECONDS_PER_MINUTE
    await scheduler.start()
    await sleep.step()
    await sleep.reached()
    await scheduler.stop()

    assert sleep.delays == [900, 900]
    assert SECONDS_PER_MINUTE == 60


def test_the_interval_comes_from_settings() -> None:
    """Fifteen minutes for balances, sixty for prices, read off `Settings`.

    Read rather than written, so a changed default fails here instead of silently
    disagreeing with `docs/operations.md`. The two are asserted as *different* as well as as
    values: one interval serving both timers would tie the price refresh to whatever the
    balance sync was tuned to, and the price side is pinned to `STALE_AFTER` rather than to a
    vendor's patience.
    """
    settings = Settings()

    assert settings.balance_sync_interval_minutes == DEFAULT_BALANCE_INTERVAL_MINUTES
    assert settings.price_refresh_interval_minutes == DEFAULT_PRICE_INTERVAL_MINUTES
    assert settings.balance_sync_interval_minutes != settings.price_refresh_interval_minutes


async def test_the_name_reaches_the_property_and_the_task() -> None:
    """Two timers in one process are two attributable log streams, or they are noise.

    `scheduler_tick_failed` with no name is a line an operator cannot act on: it does not say
    whether a chain index or a price vendor is the problem, and those have different answers.
    The task name is asserted as well, because that is what shows up in a stack dump of a
    hung process, which is the other place the two have to be told apart.
    """
    sleep = PacedSleep()
    scheduler = scheduler_over(RecordingRun(), sleep, name=PRICE_REFRESH)

    assert scheduler.name == PRICE_REFRESH
    await scheduler.start()
    await sleep.reached()
    names = {task.get_name() for task in asyncio.all_tasks()}
    await scheduler.stop()

    assert any(PRICE_REFRESH in name for name in names)


# --------------------------------------------------------------------------------------
# Criterion 1: the run at startup, and the one it does not do
# --------------------------------------------------------------------------------------


async def test_a_fresh_database_syncs_at_startup() -> None:
    """Nothing has ever run, so the first tick is now rather than in fifteen minutes.

    Sleeping first is the obvious loop and it leaves a fresh deployment blank for a quarter
    of an hour with nothing to say why -- and since the price refresh joined #10's scope,
    with every holding reported unpriced for that whole window.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = scheduler_over(run, sleep, last=None)

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert run.ticks == [True], "the startup tick is flagged as one"


async def test_a_recent_run_is_not_repeated_on_restart() -> None:
    """A run one minute old and a fifteen-minute interval: the container restarts quietly.

    This is the half that protects a public API from a crash-looping container. Without it
    every restart is two vendor calls, and a container that restarts every thirty seconds is
    a small denial of service aimed at somebody who volunteered to run an index.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = scheduler_over(run, sleep, last=NOW - timedelta(minutes=1))

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert run.ticks == [], "a fresh run must not be repeated at startup"


async def test_a_run_older_than_one_interval_is_repeated_on_restart() -> None:
    """The control for the test above: the condition has to be able to say yes.

    Without this, a scheduler that never ran at startup at all would satisfy
    `test_a_recent_run_is_not_repeated_on_restart` perfectly, and a deployment left off for
    an hour would come back up and wait another interval before reading anything.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = scheduler_over(run, sleep, last=NOW - timedelta(minutes=30))

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert run.ticks == [True]


async def test_the_startup_condition_is_measured_against_the_interval_not_a_constant() -> None:
    """The same age, two intervals, two answers.

    A condition hard-coded to fifteen minutes would pass every test above and would ignore
    the setting entirely -- and it would be flatly wrong for the price timer, whose interval
    is sixty. That is not hypothetical since the two schedulers became one class.
    """
    short = RecordingRun()
    long = RecordingRun()
    age = NOW - timedelta(minutes=20)

    for run, interval in ((short, 15), (long, 60)):
        sleep = PacedSleep()
        scheduler = scheduler_over(run, sleep, interval_minutes=interval, last=age)
        await scheduler.start()
        await sleep.reached()
        await scheduler.stop()

    assert short.ticks == [True], "20 minutes is older than a 15 minute interval"
    assert long.ticks == [], "20 minutes is younger than a 60 minute interval"


async def test_a_startup_check_that_fails_is_not_treated_as_due() -> None:
    """A database that cannot answer is a reason to wait, not a reason to call a vendor.

    The failure this prevents is compound and it is the worst shape available: the process is
    already unhealthy, so it restarts; each restart cannot read the run history, concludes a
    sync is due, and hits two public indexes on the way down. Treating "I do not know" as
    "yes" turns a database problem into a rate-limit ban.

    The first interval still elapses normally afterwards, which is what the assertion says:
    the loop is not dead, it simply did not run *now*.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = scheduler_over(run, sleep, startup_check_raises=RuntimeError("no database"))

    await scheduler.start()
    await sleep.step()
    await sleep.reached()
    await scheduler.stop()

    assert run.ticks == [False], "the startup tick was skipped; the scheduled one was not"


# --------------------------------------------------------------------------------------
# Criterion 5's half that belongs to the scheduler: it keeps going, and it stops
# --------------------------------------------------------------------------------------


async def test_a_failing_tick_does_not_end_the_loop() -> None:
    """A tick that raises is a tick, not the end of the schedule.

    Both of this application's timers already return rather than raise for a vendor failure
    -- that is a line in a run summary or a refresh report -- so what reaches the loop is a
    database failure or a defect. A loop that let it out stops working until somebody notices
    the dashboard has been frozen for a day, and nothing in any log says the schedule died.

    The second tick succeeding is what proves the loop survived rather than merely that the
    exception was swallowed somewhere.
    """
    sleep = PacedSleep()
    run = RecordingRun(raises=RuntimeError("the database is locked"), fail_times=1)
    scheduler = scheduler_over(run, sleep, last=None)

    await scheduler.start()
    await sleep.step()
    await sleep.reached()
    await scheduler.stop()

    assert run.ticks == [True, False]


async def test_one_timer_failing_does_not_stop_the_other() -> None:
    """The price refresh and the balance sync are two tasks, and neither can kill the other.

    The spec says so in one sentence and this is what makes it checkable. They share a class
    and nothing else -- no lock, no state -- so the property is structural; the test is here
    because "structural" is a claim about code that a refactor can quietly withdraw, and the
    consequence would be a dashboard with stale prices because a chain index went down.
    """
    failing = RecordingRun(raises=RuntimeError("every tick"), fail_times=99)
    healthy = RecordingRun()
    failing_sleep, healthy_sleep = PacedSleep(), PacedSleep()
    prices = scheduler_over(failing, failing_sleep, name=PRICE_REFRESH, interval_minutes=60)
    balances = scheduler_over(healthy, healthy_sleep, name=BALANCE_SYNC)

    await prices.start()
    await balances.start()
    for _ in range(2):
        await failing_sleep.step()
        await healthy_sleep.step()
    await failing_sleep.reached()
    await healthy_sleep.reached()
    await prices.stop()
    await balances.stop()

    assert len(failing.ticks) == 3, "the failing timer kept ticking as well"
    assert len(healthy.ticks) == 3, "and the healthy one was never held up by it"
    assert prices.running is False
    assert balances.running is False


async def test_stopping_ends_the_loop_and_a_second_stop_is_harmless() -> None:
    """Criterion 5 from the scheduler's side: `stop` returns with the task really gone.

    Idempotent because the lifespan is not the only caller: a failed startup unwinds through
    the same `finally`, and a `stop` that raised the second time would turn one failure into
    two with the second one on top of the traceback that mattered.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = scheduler_over(run, sleep, last=None)

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()
    await scheduler.stop()

    assert scheduler.running is False
    before = len(run.ticks)
    await asyncio.sleep(0)
    assert len(run.ticks) == before, "a stopped scheduler must not tick again"


async def test_starting_twice_does_not_start_a_second_loop() -> None:
    """Idempotent by design, and the alternative is the duplication a timer must not cause.

    Two tasks on one interval would double every vendor's traffic, and the second would be
    invisible: nothing publishes the task, so the only symptom is a request count nobody in
    this process can account for.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = scheduler_over(run, sleep, last=None)

    await scheduler.start()
    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert run.ticks == [True], "one startup tick, not two"
    assert sleep.delays == [900]


async def test_stopping_a_scheduler_that_never_started_is_harmless() -> None:
    """The shutdown path a failed startup takes, where `start` may never have been reached."""
    scheduler = scheduler_over(RecordingRun(), PacedSleep(), last=None)

    await scheduler.stop()

    assert scheduler.running is False


@pytest.mark.parametrize(
    ("age", "due"),
    [(timedelta(minutes=1), False), (timedelta(minutes=30), True)],
    ids=["a minute old", "half an hour old"],
)
async def test_the_default_clock_is_the_real_one_and_is_timezone_aware(
    age: timedelta,
    due: bool,
) -> None:
    """With no clock injected, the startup condition is measured against the real UTC time.

    Every other test here injects a clock, which leaves the one production actually uses
    unexercised. What would go wrong with it is specific: a naive `datetime.now()` compared
    with the aware timestamp the database hands back raises `TypeError`, which
    `_due_at_startup` would catch and log as a failed startup check -- so the container would
    quietly never sync at startup, on every deploy, with a traceback nobody reads.

    Both answers are asserted, so this is a statement about the comparison rather than about
    the condition happening to come out one way.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = IntervalScheduler(
        name=BALANCE_SYNC,
        interval_minutes=DEFAULT_BALANCE_INTERVAL_MINUTES,
        last_run_at=last_run_at(datetime.now(UTC) - age),
        run=run,
        sleep=sleep,
    )

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert run.ticks == ([True] if due else [])


async def test_a_run_exactly_one_interval_old_is_due() -> None:
    """The boundary itself: at exactly one interval, the startup run happens.

    `>=` rather than `>`, and the difference is one tick an interval late on a restart that
    lands on the minute. Asserted with an injected clock, because no real clock lands on a
    boundary on purpose.
    """
    sleep = PacedSleep()
    run = RecordingRun()
    scheduler = scheduler_over(
        run, sleep, last=NOW - timedelta(minutes=DEFAULT_BALANCE_INTERVAL_MINUTES)
    )

    await scheduler.start()
    await sleep.reached()
    await scheduler.stop()

    assert run.ticks == [True]


async def test_stop_returns_only_once_the_task_has_really_finished() -> None:
    """`stop()` awaits the cancelled task, so when it returns the loop is gone, not going.

    `running` cannot show this: it reads the scheduler's own reference to the task, which
    `stop()` clears before cancelling, so it reports `False` whether or not the task has
    finished. The task is found by its public name instead and asked directly. The lifespan
    closes the HTTP client and disposes the engine straight after `stop()` returns, so a task
    still unwinding at that moment meets a closed pool.
    """
    sleep = PacedSleep()
    scheduler = scheduler_over(RecordingRun(), sleep, name=PRICE_REFRESH, last=None)
    await scheduler.start()
    await sleep.reached()
    task = next(
        candidate
        for candidate in asyncio.all_tasks()
        if candidate.get_name() == f"{PRICE_REFRESH}-scheduler"
    )

    await scheduler.stop()

    assert task.done(), "stop() returned while the loop's task was still unwinding"
    assert task.cancelled()
