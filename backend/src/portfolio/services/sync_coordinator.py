"""One balance sync at a time in this process, by **joining** rather than refusing.

A second caller -- the scheduler's tick arriving while a manual sync is still running, or a
second click on a refresh button -- does not start a second run and does not get a 409. It
attaches to the run already in flight and gets that run's summary back, with `joined: true`
so the client can tell what happened.

Two alternatives were on the table and both are worse:

* **A 409.** It satisfies the words of criterion 6 and makes every client poll for a result
  it could simply have been handed. The owner clicking twice deserves the answer, not a
  status code telling them to try again.
* **An in-memory per-address cache** in front of the providers, which is what #7's deferral
  originally imagined. A cache answers a repeated refresh by returning a stale number that
  looks exactly like a fresh one -- the failure `ProviderUnavailableError` exists to prevent
  -- where joining answers it with the real read that is already happening.

## Why the run is awaited under `asyncio.shield`

A browser that disconnects mid-request causes the ASGI server to cancel the request task.
Without the shield that cancellation propagates into the run itself, so closing a tab would
abort a sync the scheduler started -- and would leave a `running` row for the sweep to
clean up after a completely ordinary user action. The shield means a disconnect cancels the
*waiting*, never the work.

## Why the lock is here at all

It covers the check-and-set and nothing else. There is no `await` between reading `_task`
and assigning it today, so under a single event loop the lock is strictly redundant. It is
here because that argument is invisible to whoever adds an `await` to the line between
them, and the failure it would cause -- two runs, two `sync_runs` rows, two sets of requests
at a public index -- is the one this class exists to prevent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

# Re-exported at runtime, not imported for an annotation only: `SyncTrigger` is the
# vocabulary of this module's own `sync()` parameter, and `api/routers/balances.py` has
# to name a member to call it. A router may not import `portfolio.repositories` directly
# -- `thin-routers` says so and it is right -- so the service that takes the value is
# where a caller should get it from.
from portfolio.repositories.sync_runs import SyncTrigger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from portfolio.repositories.sync_runs import SyncRunSummary

__all__ = ["SyncCoordinator", "SyncOutcome", "SyncRunner", "SyncTrigger"]

_logger = structlog.get_logger(__name__)

type SyncRunner = Callable[[SyncTrigger], Coroutine[Any, Any, SyncRunSummary]]
"""What actually performs a run, given a trigger. An `async def`, which is what
`Coroutine` rather than the looser `Awaitable` says: `asyncio.create_task` takes a
coroutine, and a named task is what makes this run legible in a debugger.

A callable rather than a `BalanceSyncService`, because a run must **not** share the session
of the request that started it: a joined run outlives its requester, and a session closed by
a dependency on the way out of that request would be pulled out from under the work. The
lifespan passes a closure that opens its own session per run.
"""


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """A run's summary, and whether this caller started it or attached to it.

    `joined` is a fact about *this call*, not about the run, which is why it is here rather
    than on `SyncRunSummary`: the same run is `joined=False` for the caller that started it
    and `joined=True` for everyone who arrived afterwards, and a field on the summary could
    only record one of those.
    """

    summary: SyncRunSummary
    joined: bool


class SyncCoordinator:
    """Holds the run in flight, if there is one, and hands it to whoever asks next.

    One instance per application, installed on `app.state` by the lifespan. Not safe across
    processes and it does not need to be: there is one instance of this application, and the
    thing being protected is a burst of requests at a public index rather than a database
    invariant.
    """

    def __init__(self, runner: SyncRunner) -> None:
        self._runner = runner
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[SyncRunSummary] | None = None

    @property
    def in_flight(self) -> bool:
        """Whether a run is happening right now. For the lifespan's shutdown, and for a test."""
        return self._task is not None and not self._task.done()

    async def sync(self, trigger: SyncTrigger) -> SyncOutcome:
        """Start a run, or attach to the one already going, and return its summary.

        `trigger` is recorded only when this call actually starts the run. A caller that
        joins gets the *running* run's trigger back on the summary, not its own -- a
        scheduled run that a manual click attached to is still a scheduled run, and
        relabelling it would make the run log say something that did not happen.

        Raises:
            Whatever the runner raises. Both the starter and every joiner see it, which is
            correct: they were all waiting on the same piece of work.
        """
        async with self._lock:
            existing = self._task
            if existing is not None and not existing.done():
                task, joined = existing, True
            else:
                task = asyncio.create_task(self._runner(trigger), name="balance-sync")
                # Retrieving the result is normally the awaiting caller's job. This is for
                # the case where there is no such caller any more: a browser that
                # disconnected leaves the shield intact and the task unobserved, and an
                # unobserved failure would otherwise surface as asyncio's own "exception was
                # never retrieved" warning at garbage-collection time, detached from the run.
                task.add_done_callback(_report_unobserved_failure)
                self._task = task
                joined = False
        summary = await asyncio.shield(task)
        return SyncOutcome(summary=summary, joined=joined)

    async def drain(self, *, grace_seconds: int) -> bool:
        """Wait for a run in flight, and cancel it if it outlives the grace period.

        What the lifespan calls on the way down. **It does not itself record the run as
        interrupted**: the lifespan sweeps every surviving `running` row afterwards, which is
        the same operation startup performs and is reliable precisely because it is not
        running inside the task that was just cancelled. Writing to the database from a
        cancelled coroutine is the arrangement that looks tidier and fails under the one
        condition it exists for.

        Returns:
            `True` if there was nothing to wait for or the run finished on its own -- however
            it finished, a failed run included. `False` if the grace period expired and the
            run was cancelled, which is what leaves a row for the sweep.
        """
        task = self._task
        if task is None or task.done():
            return True
        try:
            # Shielded again: `wait_for` cancels what it is waiting on when it times out,
            # and what it must cancel is this wait, not the run -- the run is cancelled
            # below, deliberately and after the grace period has actually elapsed.
            await asyncio.wait_for(asyncio.shield(task), grace_seconds)
        except TimeoutError:
            _logger.warning("balance_sync_cancelled_at_shutdown", grace_seconds=grace_seconds)
            task.cancel()
            # `asyncio.wait` rather than `await task`: it returns when the task is done and
            # never re-raises, so neither the `CancelledError` nor a failure needs
            # suppressing here.
            await asyncio.wait({task})
            return False
        except Exception:
            # The run ended by failing, which is still "not in flight any more". The runner's
            # own caller logged it; there is nothing for shutdown to add and nothing to
            # sweep, because the run recorded its own outcome.
            _logger.debug("balance_sync_failed_before_shutdown")
            return True
        return True


def _report_unobserved_failure(task: asyncio.Task[SyncRunSummary]) -> None:
    """Retrieve a failed run's exception so that it is logged here rather than by asyncio.

    Only reached when nothing awaited the task -- a disconnected browser, in practice. A
    cancelled task has no exception to retrieve and is not a failure worth a line: it is the
    shutdown path doing exactly what it says it does.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _logger.error("balance_sync_failed", error_type=type(error).__name__, exc_info=error)
