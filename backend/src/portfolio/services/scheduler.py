"""The interval loop that keeps balances fresh, started and stopped by the lifespan.

One `asyncio.Task` holding a sleep and a tick. It asks the coordinator for a sync rather
than running one itself, which is what makes criterion 6 hold from both directions: a tick
that arrives while a manual refresh is still in flight joins it instead of piling a second
run on top of a public index.

## The first run is conditional, and both halves of the condition are real deployments

**Sleep first** and a fresh deployment shows an empty dashboard for a whole interval, which
is exactly the moment somebody is watching. **Run unconditionally** and a container that is
crash-looping hits two public indexes on every restart, which is how a free index bans you.

So: sync at startup only if the newest *finished* run is older than one interval. One query
answers both, and the query is handed in as a callable rather than a repository, so this
module needs no session and no `sqlalchemy` import.

## Nothing here raises past the loop

A tick that fails -- a database that is locked, a bug in the sync -- is logged and the loop
continues. A scheduler that dies on the first bad night is worse than no scheduler, because
nothing says it stopped. `CancelledError` is a `BaseException` rather than an `Exception`,
so `stop()` still ends the loop rather than being caught and logged as a failed tick.

## Durations are whole seconds

`float` is banned in `services/`, and every interval this product will ever want is a whole
number of minutes. `asyncio.sleep` takes an `int` perfectly well, so nothing here has to
divide anything and no conversion introduces one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from portfolio.repositories.sync_runs import SyncTrigger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from portfolio.config import Settings
    from portfolio.services.sync_coordinator import SyncCoordinator

__all__ = [
    "SECONDS_PER_MINUTE",
    "BalanceSyncScheduler",
    "LatestFinishedAt",
    "build_balance_scheduler",
    "sleep_seconds",
    "utc_now",
]

SECONDS_PER_MINUTE: Final = 60

_logger = structlog.get_logger(__name__)

type LatestFinishedAt = Callable[[], Awaitable[datetime | None]]
"""When the newest finished run ended, or `None` if none ever has.

A callable rather than a `SyncRunRepository`, so this module needs neither a session nor a
`sqlalchemy` import, and so a test can answer the startup question with a literal instead of
a database.
"""

type Sleeper = Callable[[int], Awaitable[None]]
"""How the loop waits, in whole seconds. Injected so no test of this module waits."""


def utc_now() -> datetime:
    """The wall clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


async def sleep_seconds(duration: int) -> None:
    """Wait `duration` whole seconds."""
    await asyncio.sleep(duration)


class BalanceSyncScheduler:
    """Runs a balance sync every `interval_seconds`, until it is stopped.

    One instance per application. `start` and `stop` are the lifespan's; nothing else owns
    the task, and the task is not published anywhere a request could reach it.
    """

    def __init__(
        self,
        *,
        coordinator: SyncCoordinator,
        interval_seconds: int,
        latest_finished_at: LatestFinishedAt,
        clock: Callable[[], datetime] = utc_now,
        sleep: Sleeper = sleep_seconds,
    ) -> None:
        self._coordinator = coordinator
        self._interval_seconds = interval_seconds
        self._latest_finished_at = latest_finished_at
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def interval_seconds(self) -> int:
        """How long the loop waits between ticks. Read by a test, and by nothing else."""
        return self._interval_seconds

    @property
    def running(self) -> bool:
        """Whether the loop's task exists and has not finished."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the loop. Idempotent: starting a running scheduler does nothing.

        Idempotent rather than an error, because the alternative -- a second task on the same
        coordinator -- is exactly the duplicated work the coordinator exists to prevent, and
        raising would turn a harmless double call in a lifespan into a failed startup.
        """
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="balance-sync-scheduler")
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
        """Sync at startup if one is due, then once per interval, forever."""
        if await self._due_at_startup():
            await self._tick(SyncTrigger.STARTUP)
        while True:
            await self._sleep(self._interval_seconds)
            await self._tick(SyncTrigger.SCHEDULED)

    async def _due_at_startup(self) -> bool:
        """Whether the newest finished run is old enough to justify syncing right now.

        A database that has never synced is due. A failure to answer the question is **not**
        treated as due: the case that produces it is a database that is not answering at all,
        and the right response to that is to wait for the first interval rather than to
        hammer two public indexes while the process is already unhealthy.
        """
        try:
            last = await self._latest_finished_at()
        except Exception:
            _logger.exception("balance_sync_startup_check_failed")
            return False
        if last is None:
            return True
        return self._clock() - last >= timedelta(seconds=self._interval_seconds)

    async def _tick(self, trigger: SyncTrigger) -> None:
        """Ask for one sync and swallow anything that is not a cancellation.

        The coordinator's `sync` already returns rather than raises for a vendor failure --
        that is a line in the run summary -- so what reaches here is a database failure or a
        defect. Both are worth a traceback and neither is worth killing the loop for.
        """
        try:
            await self._coordinator.sync(trigger)
        except Exception:
            _logger.exception("balance_sync_tick_failed", trigger=trigger.value)


def build_balance_scheduler(
    settings: Settings,
    *,
    coordinator: SyncCoordinator,
    latest_finished_at: LatestFinishedAt,
    clock: Callable[[], datetime] = utc_now,
    sleep: Sleeper = sleep_seconds,
) -> BalanceSyncScheduler:
    """Build the scheduler from settings, converting the configured minutes to seconds.

    The conversion lives here rather than in the lifespan so that "the interval comes from
    `PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES`" has one place to be true and one place to be
    tested. `Settings` refuses an interval below one minute at construction, so nothing here
    has to defend against a loop with no sleep in it.
    """
    return BalanceSyncScheduler(
        coordinator=coordinator,
        interval_seconds=settings.balance_sync_interval_minutes * SECONDS_PER_MINUTE,
        latest_finished_at=latest_finished_at,
        clock=clock,
        sleep=sleep,
    )
