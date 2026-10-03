"""Spec 030 (#23): each timer remembers its ticks, and `status(now)` reports them.

`IntervalScheduler` records, through its injected clock, when its loop started and when its
last tick began and finished, and whether that tick raised. `status(now)` hands those to
`domain.health.scheduler_state` and serves the last finish and its outcome. These tests drive
the real loop with a gated sleep, so a tick happens exactly when the test lets it, and a clock
the test moves by hand, so every recorded instant is one the test chose -- distinct from every
other, so an instant recorded in the wrong field cannot pass for the right one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final

from portfolio.domain.health import SchedulerState
from portfolio.services.scheduler import IntervalScheduler, SchedulerStatus

T0: Final = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
INTERVAL_MINUTES: Final = 15
INTERVAL: Final = timedelta(minutes=INTERVAL_MINUTES)
TICK_TAKES: Final = timedelta(minutes=4)
DEADLOCK_TIMEOUT: Final = 5


class GatedSleep:
    """The injected sleep: parks the loop until the test releases it.

    `parked()` waits until the loop has finished a tick and is asleep again; `release()` lets
    it out for the next one. Two separate steps, unlike `tests.balance_harness.PacedSleep`,
    because these tests read the timer's record *while* it is parked.
    """

    def __init__(self) -> None:
        self._arrived = asyncio.Event()
        self._go = asyncio.Event()

    async def __call__(self, delay: int) -> None:
        del delay
        self._arrived.set()
        await self._go.wait()
        self._go.clear()

    async def parked(self) -> None:
        await asyncio.wait_for(self._arrived.wait(), timeout=DEADLOCK_TIMEOUT)
        self._arrived.clear()

    def release(self) -> None:
        self._go.set()


class Clock:
    """A clock the test sets. The work moves it forward to model a tick that takes time."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Work:
    """The timer's work: moves the clock by `TICK_TAKES`, and raises while told to."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.ticks = 0
        self.raises = False
        self.block: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def __call__(self, at_startup: bool) -> None:
        del at_startup
        self.ticks += 1
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        self.clock.now += TICK_TAKES
        if self.raises:
            message = "a tick that failed"
            raise RuntimeError(message)


def outcome_of(scheduler: IntervalScheduler) -> bool | None:
    """The last outcome, read afresh: an `assert ... is False` narrows the property for mypy."""
    return scheduler.last_tick_succeeded


async def never_ran() -> datetime | None:
    return None


def build(clock: Clock, work: Work, sleep: GatedSleep) -> IntervalScheduler:
    return IntervalScheduler(
        name="balance-sync",
        interval_minutes=INTERVAL_MINUTES,
        last_run_at=never_ran,
        run=work,
        clock=clock,
        sleep=sleep,
    )


async def test_a_timer_that_was_never_started_is_stopped_with_nothing_recorded() -> None:
    clock = Clock()
    scheduler = build(clock, Work(clock), GatedSleep())

    assert scheduler.status(T0) == SchedulerStatus(
        state=SchedulerState.STOPPED, last_tick_at=None, last_tick_succeeded=None
    )
    assert scheduler.started_at is None
    assert scheduler.last_tick_started_at is None
    assert scheduler.last_tick_finished_at is None
    assert scheduler.last_tick_succeeded is None


async def test_a_started_timer_is_ok_before_its_first_tick_has_finished() -> None:
    """The start is recorded before the task exists, so a running loop always has one."""
    clock = Clock()
    work = Work(clock)
    work.block = asyncio.Event()
    sleep = GatedSleep()
    scheduler = build(clock, work, sleep)

    await scheduler.start()
    await asyncio.wait_for(work.entered.wait(), timeout=DEADLOCK_TIMEOUT)
    try:
        assert scheduler.started_at == T0
        assert scheduler.last_tick_started_at == T0
        assert scheduler.status(T0 + 2 * INTERVAL) == SchedulerStatus(
            state=SchedulerState.OK, last_tick_at=None, last_tick_succeeded=None
        )
        # Judged from the start while no tick has finished: a tick in flight for longer than
        # two intervals is late, which is the point.
        assert scheduler.status(T0 + 2 * INTERVAL + timedelta(microseconds=1)).state is (
            SchedulerState.LATE
        )
    finally:
        await scheduler.stop()


async def test_a_finished_tick_records_its_start_its_finish_and_its_success() -> None:
    clock = Clock()
    work = Work(clock)
    sleep = GatedSleep()
    scheduler = build(clock, work, sleep)

    await scheduler.start()
    await sleep.parked()
    finished = T0 + TICK_TAKES
    try:
        assert work.ticks == 1
        assert scheduler.started_at == T0
        assert scheduler.last_tick_started_at == T0
        assert scheduler.last_tick_finished_at == finished
        assert scheduler.last_tick_succeeded is True
        assert scheduler.status(finished) == SchedulerStatus(
            state=SchedulerState.OK, last_tick_at=finished, last_tick_succeeded=True
        )
    finally:
        await scheduler.stop()


async def test_the_reference_is_the_last_finish_and_the_boundary_is_two_intervals() -> None:
    clock = Clock()
    scheduler = build(clock, Work(clock), sleep := GatedSleep())

    await scheduler.start()
    await sleep.parked()
    finished = T0 + TICK_TAKES
    try:
        assert scheduler.status(finished + 2 * INTERVAL).state is SchedulerState.OK
        assert scheduler.status(finished + 2 * INTERVAL + timedelta(microseconds=1)).state is (
            SchedulerState.LATE
        )
    finally:
        await scheduler.stop()


async def test_a_second_tick_in_flight_is_judged_from_its_own_start() -> None:
    """R13, S4: the loop sleeps an interval after a finish, so the second tick starts one
    interval after the first finished. Judged from that finish it would be late one interval
    into its run; the timer hands over the tick's start, so it is late two intervals into it.
    """
    clock = Clock()
    work = Work(clock)
    sleep = GatedSleep()
    scheduler = build(clock, work, sleep)

    await scheduler.start()
    await sleep.parked()
    finished = T0 + TICK_TAKES
    second_start = finished + INTERVAL
    clock.now = second_start
    work.block = asyncio.Event()
    work.entered.clear()
    sleep.release()
    await asyncio.wait_for(work.entered.wait(), timeout=DEADLOCK_TIMEOUT)
    try:
        assert work.ticks == 2
        assert scheduler.last_tick_started_at == second_start
        assert scheduler.last_tick_finished_at == finished
        assert scheduler.status(second_start + 2 * INTERVAL) == SchedulerStatus(
            state=SchedulerState.OK, last_tick_at=finished, last_tick_succeeded=True
        )
        assert scheduler.status(second_start + 2 * INTERVAL + timedelta(microseconds=1)).state is (
            SchedulerState.LATE
        )
    finally:
        await scheduler.stop()


async def test_the_interval_judged_is_the_timers_own_in_minutes() -> None:
    """Thirty-one minutes after a tick: late at fifteen minutes, ok at an hour."""
    clock = Clock()
    fifteen = build(clock, Work(clock), first_sleep := GatedSleep())
    hourly = IntervalScheduler(
        name="price-refresh",
        interval_minutes=60,
        last_run_at=never_ran,
        run=Work(clock),
        clock=clock,
        sleep=(second_sleep := GatedSleep()),
    )

    await fifteen.start()
    await first_sleep.parked()
    clock.now = T0
    await hourly.start()
    await second_sleep.parked()
    try:
        later = T0 + TICK_TAKES + timedelta(minutes=31)
        assert fifteen.status(later).state is SchedulerState.LATE
        assert hourly.status(later).state is SchedulerState.OK
    finally:
        await fifteen.stop()
        await hourly.stop()


async def test_a_tick_that_raised_is_recorded_as_failed_and_the_next_success_clears_it() -> None:
    clock = Clock()
    work = Work(clock)
    work.raises = True
    sleep = GatedSleep()
    scheduler = build(clock, work, sleep)

    await scheduler.start()
    await sleep.parked()
    try:
        failed_at = T0 + TICK_TAKES
        assert scheduler.last_tick_succeeded is False
        assert scheduler.status(failed_at) == SchedulerStatus(
            state=SchedulerState.OK, last_tick_at=failed_at, last_tick_succeeded=False
        )

        work.raises = False
        sleep.release()
        await sleep.parked()

        assert work.ticks == 2
        assert outcome_of(scheduler) is True
        assert scheduler.last_tick_started_at == failed_at
        assert scheduler.last_tick_finished_at == failed_at + TICK_TAKES
    finally:
        await scheduler.stop()


async def test_a_tick_cut_short_by_stop_records_no_finish() -> None:
    """It did not finish, so the last finish and its outcome stay what they were."""
    clock = Clock()
    work = Work(clock)
    sleep = GatedSleep()
    scheduler = build(clock, work, sleep)
    await scheduler.start()
    await sleep.parked()
    first_finish = T0 + TICK_TAKES

    work.block = asyncio.Event()
    work.entered.clear()
    clock.now = T0 + timedelta(hours=1)
    sleep.release()
    await asyncio.wait_for(work.entered.wait(), timeout=DEADLOCK_TIMEOUT)
    await scheduler.stop()

    assert scheduler.last_tick_started_at == T0 + timedelta(hours=1)
    assert scheduler.last_tick_finished_at == first_finish
    assert scheduler.last_tick_succeeded is True
    assert scheduler.status(clock.now) == SchedulerStatus(
        state=SchedulerState.STOPPED, last_tick_at=first_finish, last_tick_succeeded=True
    )


async def test_a_stopped_timer_keeps_its_record_and_a_restart_records_a_new_start() -> None:
    clock = Clock()
    work = Work(clock)
    sleep = GatedSleep()
    scheduler = build(clock, work, sleep)
    await scheduler.start()
    await sleep.parked()
    await scheduler.stop()

    assert scheduler.status(T0).state is SchedulerState.STOPPED
    assert scheduler.started_at == T0

    clock.now = T0 + timedelta(days=1)
    await scheduler.start()
    await sleep.parked()
    try:
        assert scheduler.started_at == T0 + timedelta(days=1)
        assert scheduler.last_tick_finished_at == T0 + timedelta(days=1) + TICK_TAKES
    finally:
        await scheduler.stop()


async def test_starting_a_running_timer_again_keeps_its_first_start() -> None:
    clock = Clock()
    sleep = GatedSleep()
    scheduler = build(clock, Work(clock), sleep)
    await scheduler.start()
    await sleep.parked()

    clock.now = T0 + timedelta(hours=3)
    await scheduler.start()
    try:
        assert scheduler.started_at == T0
    finally:
        await scheduler.stop()


def test_the_status_carries_no_interval() -> None:
    """No configuration value is served: the status has three fields and none is a duration."""
    assert SchedulerStatus.__slots__ == ("state", "last_tick_at", "last_tick_succeeded")
