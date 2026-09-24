"""Run a coroutine every N minutes, started and stopped by the lifespan.

One `asyncio.Task` holding a sleep and a tick, and **nothing in this module knows what it is
running**. It takes two callables -- "when did this last happen" and "do it" -- so the
application's two timers, the balance sync and the price refresh, are two instances rather
than two loops.

That generalisation was a choice worth stating. The differences between the two are three
injected values; the similarities are the whole class: start, stop, the cancellation
handshake, the first-run condition, and the rule that a failed tick must not kill the loop.
Two copies of that would have been eighty lines of subtle cancellation handling written
twice, and the second copy is the one that would have drifted.

## Each scheduler is its own task, which is what isolates them

A price refresh that raises does not stop the balance sync and a balance sync that raises
does not stop the price refresh, because they share no task, no lock and no state -- only a
type. `_tick` swallowing everything below `BaseException` is the second half of that: within
one scheduler, a bad night must not end the loop, because a scheduler that dies silently is
worse than one that never started.

## The first run is conditional, and both halves of the condition are real deployments

**Sleep first** and a fresh deployment shows an empty dashboard for a whole interval, which
is exactly the moment somebody is watching. **Run unconditionally** and a container that is
crash-looping hits two public APIs on every restart, which is how a free index bans you.

So: run at startup only if the last one is older than one interval. `last_run_at` is handed
in as a callable rather than a repository, so this module needs no session and no
`sqlalchemy` import, and a test can answer the question with a literal.

## When it is not due, the first sleep is what is left of the interval

**Not a whole interval**, which is what the first version slept and which made every deploy
cost an interval of staleness. Prices refresh at 12:00, a deploy lands at 12:50, nothing is
due at startup -- and sleeping sixty minutes from there put the next refresh at 13:50, so
every price read `stale` from 13:00 to 13:50 after every single deploy. Sleeping the ten
minutes that remained keeps the schedule the process had before it restarted.

The remainder is rounded **up** to a whole second. Rounding down would wake a fraction of a
second early, find the last run still younger than the interval by that fraction, and --
because the loop does not re-check, it simply ticks -- run slightly early. Early by less than
a second is harmless; the point is that "at most once per interval" should be true as
written, and up is the direction in which it is.

## Durations are whole minutes in and whole seconds out

`float` is banned in `services/`, and every interval this product will ever want is a whole
number of minutes. `asyncio.sleep` takes an `int` perfectly well, so nothing here divides
anything and no conversion introduces one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

__all__ = [
    "SECONDS_PER_MINUTE",
    "IntervalScheduler",
    "LastRunAt",
    "ScheduledRun",
    "sleep_seconds",
    "utc_now",
]

SECONDS_PER_MINUTE: Final = 60
_MICROSECONDS_PER_SECOND: Final = 1_000_000
_ONE_MICROSECOND: Final = timedelta(microseconds=1)

_logger = structlog.get_logger(__name__)

type LastRunAt = Callable[[], Awaitable[datetime | None]]
"""When the scheduled work last completed, or `None` if it never has.

A callable rather than a repository, so this module needs neither a session nor a
`sqlalchemy` import, and so a test can answer the startup question with a literal instead of
a database. The balance sync answers it from `sync_runs.finished_at`; the price refresh
answers it from `prices.fetched_at`.
"""

type ScheduledRun = Callable[[bool], Coroutine[Any, Any, None]]
"""The work, taking one flag: whether this is the run that happened at startup.

**A `Coroutine` rather than the looser `Awaitable`**, because `asyncio.create_task` takes a
coroutine and a named task is what makes a timer legible in a debugger.

**The flag is there for exactly one caller and the other ignores it**, which is a smell worth
answering rather than hiding. The balance sync records *what started a run* in a column an
operator reads, and `startup` is a different answer from `scheduled`: the run that happens
when the process comes up is the one somebody is looking at when they ask whether the deploy
worked, and folding it into the interval's own ticks would make it invisible. A price refresh
has no such column and no such question, so its implementation takes the flag and drops it.

The alternative -- a second enum of tick reasons in this module, mapped onto `SyncTrigger` by
the caller -- would be two spellings of the same three words, which is the duplication this
codebase spends most of its comments avoiding.
"""

type Sleeper = Callable[[int], Awaitable[None]]
"""How the loop waits, in whole seconds. Injected so no test of this module waits."""


def utc_now() -> datetime:
    """The wall clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


async def sleep_seconds(duration: int) -> None:
    """Wait `duration` whole seconds."""
    await asyncio.sleep(duration)


class IntervalScheduler:
    """Runs one coroutine every `interval_minutes`, until it is stopped.

    `start` and `stop` belong to the lifespan; nothing else owns the task, and the task is
    not published anywhere a request could reach it.

    `name` is not decoration: it goes into the task's name and into every log line this class
    writes, so that two timers in one process are two distinguishable streams rather than one
    `scheduler_tick_failed` nobody can attribute.
    """

    def __init__(
        self,
        *,
        name: str,
        interval_minutes: int,
        last_run_at: LastRunAt,
        run: ScheduledRun,
        clock: Callable[[], datetime] = utc_now,
        sleep: Sleeper = sleep_seconds,
    ) -> None:
        self._name = name
        self._interval_seconds = interval_minutes * SECONDS_PER_MINUTE
        self._last_run_at = last_run_at
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        """What this timer is called in a log and in a task name."""
        return self._name

    @property
    def interval_seconds(self) -> int:
        """How long the loop waits between ticks. The configured minutes, converted once."""
        return self._interval_seconds

    @property
    def running(self) -> bool:
        """Whether the loop's task exists and has not finished."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the loop. Idempotent: starting a running scheduler does nothing.

        Idempotent rather than an error, because the alternative -- a second task doing the
        same work on the same interval -- is precisely the duplication a timer must not
        cause, and raising would turn a harmless double call in a lifespan into a failed
        startup.
        """
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name=f"{self._name}-scheduler")
        # Hand control to the loop once, so that `await start()` means the task has begun
        # rather than merely been created. It does **not** mean the first tick has finished:
        # the loop suspends again on its first real await, which is the startup query.
        await asyncio.sleep(0)

    async def stop(self) -> None:
        """Cancel the loop and wait for it to actually stop. Idempotent.

        Awaiting the cancelled task rather than only requesting the cancellation is what
        makes shutdown ordered: without it the lifespan would go on to close the HTTP client
        and dispose the engine while a tick was still mid-flight, and the failure would
        surface as a closed pool rather than as a cancelled scheduler.

        `asyncio.wait` rather than `await task`, so the `CancelledError` does not need
        suppressing in the caller.
        """
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait({task})

    async def _loop(self) -> None:
        """Run now if one is due, otherwise when it would have been; then once per interval.

        The tick after a partial sleep is `at_startup=False`. It is the run that was
        scheduled before the process restarted, resumed rather than repeated, and recording
        it as a startup run would claim the deploy caused it.
        """
        wait = await self._seconds_until_due()
        if wait == 0:
            await self._tick(at_startup=True)
        else:
            await self._sleep(wait)
            await self._tick(at_startup=False)
        while True:
            await self._sleep(self._interval_seconds)
            await self._tick(at_startup=False)

    async def _seconds_until_due(self) -> int:
        """How long until the next run is due, in whole seconds, rounded up. Zero means now.

        Work that has never run is due now. So is work whose last run is at least one
        interval old.

        Otherwise it is the rest of the interval, which is what keeps a restart from pushing
        the schedule back by a whole interval -- see the module docstring for the deploy that
        made every price stale for fifty minutes.

        Two edges, both resolved towards "wait":

        * **A last run in the future** -- the clock was stepped back since it was recorded --
          would make the remainder longer than an interval. It is clamped to one interval:
          the loop must never sleep longer than it was configured to, whatever the clock did.
        * **A failure to answer the question** waits a full interval rather than running now.
          What produces it is a database that is not answering at all, and the right
          response is not to hammer a public API while the process is already unhealthy.

        Integer arithmetic throughout. `timedelta // timedelta` is an `int`, and the ceiling is
        the negated floor of the negation -- `float` is banned here, and `total_seconds()`
        returns one.
        """
        try:
            last = await self._last_run_at()
        except Exception:
            _logger.exception("scheduler_startup_check_failed", scheduler=self._name)
            return self._interval_seconds
        if last is None:
            return 0
        interval = timedelta(seconds=self._interval_seconds)
        remaining = interval - (self._clock() - last)
        if remaining <= timedelta(0):
            return 0
        microseconds = min(remaining, interval) // _ONE_MICROSECOND
        return -(-microseconds // _MICROSECONDS_PER_SECOND)

    async def _tick(self, *, at_startup: bool) -> None:
        """Run the work once and swallow anything that is not a cancellation.

        Both of this application's tasks already return rather than raise for a vendor
        failure -- that is a line in a run summary or a refresh report -- so what reaches
        here is a database failure or a defect. Both are worth a traceback and neither is
        worth killing the loop for.

        `CancelledError` is a `BaseException` rather than an `Exception`, so `stop()` still
        ends the loop instead of being caught and logged as a failed tick.
        """
        try:
            await self._run(at_startup)
        except Exception:
            _logger.exception(
                "scheduler_tick_failed",
                scheduler=self._name,
                at_startup=at_startup,
            )
