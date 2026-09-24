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
from dataclasses import dataclass, field
from functools import partial
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


@dataclass(slots=True)
class _RunInFlight:
    """One run's task and how many callers are still waiting to be handed its outcome.

    **Per run, not per coordinator**, and that is the point of the class. A task is `done()`
    the moment it finishes, before its done-callbacks run, so a new `sync()` can start the
    next run in the gap between the two. A single counter on the coordinator would then be
    read by the *old* run's callback after the *new* run had incremented it, and a failure
    nobody was waiting for would go unlogged because somebody was waiting for something
    else.

    Mutable, deliberately: `waiting` is the one thing here that changes, and it changes only
    on the event loop's thread.
    """

    task: asyncio.Task[SyncRunSummary]
    waiting: int = field(default=0)


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
        self._current: _RunInFlight | None = None

    @property
    def in_flight(self) -> bool:
        """Whether a run is happening right now. For the lifespan's shutdown, and for a test."""
        return self._current is not None and not self._current.task.done()

    async def sync(self, trigger: SyncTrigger) -> SyncOutcome:
        """Start a run, or attach to the one already going, and return its summary.

        `trigger` is recorded only when this call actually starts the run. A caller that
        joins gets the *running* run's trigger back on the summary, not its own -- a
        scheduled run that a manual click attached to is still a scheduled run, and
        relabelling it would make the run log say something that did not happen.

        **This method logs nothing about a failure**, and that is the division of labour
        rather than an omission: a caller that receives the exception reports it -- the
        scheduler's `_tick`, or the application's unhandled-exception handler for a request.
        The coordinator reports only a failure that no caller is left to receive; see
        `_report_unobserved_failure`.

        Raises:
            Whatever the runner raises. Both the starter and every joiner see it, which is
            correct: they were all waiting on the same piece of work.
        """
        async with self._lock:
            current = self._current
            if current is not None and not current.task.done():
                joined = True
            else:
                task = asyncio.create_task(self._runner(trigger), name="balance-sync")
                current = _RunInFlight(task=task)
                task.add_done_callback(partial(_report_unobserved_failure, current))
                self._current = current
                joined = False
            current.waiting += 1
        try:
            summary = await asyncio.shield(current.task)
        except asyncio.CancelledError:
            # This caller stopped waiting -- a browser disconnected, the scheduler was
            # stopped -- and the shield left the run going. It will not receive the outcome,
            # so it no longer counts towards the callers that will report a failure.
            current.waiting -= 1
            raise
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
        if self._current is None or self._current.task.done():
            return True
        task = self._current.task
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
            # The run ended by failing, which is still "not in flight any more". This wait
            # is not one of the callers that reports a failure -- by the time shutdown
            # drains, the scheduler has been stopped and its wait cancelled -- so if nobody
            # else was waiting, `_report_unobserved_failure` has logged it with the
            # traceback. Nothing to add here and nothing to sweep: the run's own row was
            # either closed out by the run or will be swept by the lifespan.
            _logger.debug("balance_sync_failed_before_shutdown")
            return True
        return True


def _report_unobserved_failure(
    run: _RunInFlight,
    task: asyncio.Task[SyncRunSummary],
) -> None:
    """Log a failed run **only if no caller is still waiting to receive the failure**.

    A done-callback runs on *every* completion, not only on an unobserved one, and the first
    version of this function logged unconditionally while its docstring claimed otherwise --
    so a failed scheduled run produced two tracebacks, one from here and one from the
    scheduler's `_tick`, which received the same exception a moment later. Now the callback
    reads the run's waiter count and stays silent when somebody is there to report it.

    What "nobody is waiting" covers, in practice: a manual sync whose browser disconnected,
    and a run that fails while shutdown drains it, after the scheduler's wait was cancelled.
    Either way the failure would otherwise reach no log at all -- `asyncio.shield` retrieves
    the task's exception when it resolves the outer future, so asyncio's own "exception was
    never retrieved" warning does not fire either.

    **One residual, stated rather than hidden.** If the last waiter is cancelled in the same
    event-loop iteration in which the run fails, this callback can see it still counted,
    stay silent, and the cancelled caller never receives the exception. Closing that would
    need the callback to defer its decision past the waiter's wake-up, which is ordering the
    event loop does not promise, and the window it would close is a single iteration.

    A cancelled task has no exception to retrieve and is not a failure worth a line: it is
    the shutdown path doing exactly what it says it does.
    """
    if task.cancelled() or run.waiting > 0:
        return
    error = task.exception()
    if error is not None:
        _logger.error("balance_sync_failed", error_type=type(error).__name__, exc_info=error)
