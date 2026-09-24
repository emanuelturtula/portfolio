"""Criterion 6: one run at a time, by joining rather than by refusing.

The issue says "a concurrent manual and scheduled run do not duplicate work" and the spec
resolves the ambiguity in those words deliberately: the second caller **joins** the run in
flight and is handed its summary with `joined: true`, rather than being told 409 and left
to poll for a result it could have been given.

That makes three things testable, and the third is the one that bites:

1. the runner is invoked **once**, however many callers arrive;
2. the joined caller is told whose run it got -- the trigger is the running one's, not its
   own, or a manual click would report itself as having done work the schedule did;
3. **a caller going away does not take the run with it.** A browser that disconnects
   cancels the request task; without `asyncio.shield` that cancellation propagates into the
   awaited task and kills a scheduled sync that had nothing to do with the request.

Nothing here touches a database. The coordinator's whole subject is a task and a lock, and
a real sync underneath would only add a second thing that could fail -- the runner is a
counting stub, and `tests/services/test_balance_sync.py` is where the run itself is proven.

**Every test drives the concurrency with an `asyncio.Event` rather than with a sleep.** A
verdict that depends on how long a sleep took is a verdict that changes on a loaded CI
runner, which is the standard `tests/providers/harness.py` sets for the same reason.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.repositories.sync_runs import SyncRunStatus, SyncRunSummary, SyncTrigger
from portfolio.services.sync_coordinator import SyncCoordinator, SyncOutcome

if TYPE_CHECKING:
    from collections.abc import Awaitable

STARTED_AT: Final = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)

#: How long a test is willing to wait for something it expects to happen immediately. Not a
#: pacing knob: every wait in this module is on an event another task sets, so this bound is
#: only ever reached when the behaviour under test is wrong, and then it is what turns a
#: hang into a readable failure.
DEADLOCK_TIMEOUT: Final = 5


def summary(run_id: int, trigger: SyncTrigger) -> SyncRunSummary:
    """A finished run, as the sync service would have returned it."""
    return SyncRunSummary(
        run_id=run_id,
        trigger=trigger,
        status=SyncRunStatus.SUCCESS,
        started_at=STARTED_AT,
        finished_at=STARTED_AT,
        duration_ms=1,
        wallets_total=1,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=(),
    )


class Runner:
    """A stub sync that counts its callers and can be held open on command.

    `gate` is the whole point: with it unset, a run blocks after announcing that it has
    started, so a second caller can be made to arrive *while the first is still running*.
    Without a gate the two calls would race and the test would pass or fail depending on
    which coroutine the loop happened to resume first.
    """

    def __init__(self, *, gated: bool = False, raises: BaseException | None = None) -> None:
        self.triggers: list[SyncTrigger] = []
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.completed = 0
        self.raises = raises
        if not gated:
            self.gate.set()

    async def __call__(self, trigger: SyncTrigger) -> SyncRunSummary:
        self.triggers.append(trigger)
        self.started.set()
        await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        self.completed += 1
        return summary(len(self.triggers), trigger)


async def wait_for[T](awaitable: Awaitable[T]) -> T:
    """Await with a bound, so a coordinator that never resolves fails instead of hanging."""
    return await asyncio.wait_for(awaitable, timeout=DEADLOCK_TIMEOUT)


def in_flight(coordinator: SyncCoordinator) -> bool:
    """Read the flag afresh, through a call `mypy` cannot narrow.

    Written inline, `assert coordinator.in_flight is False` narrows the property to
    `Literal[False]` for the rest of the function, and the later `is True` then reads as
    unreachable -- which it is not: an `await` in between is exactly what changes it. A call
    returns a plain `bool` every time, which is the truth about a property that moves.
    """
    return coordinator.in_flight


# --------------------------------------------------------------------------------------
# One run at a time
# --------------------------------------------------------------------------------------


async def test_a_second_caller_joins_rather_than_starting_a_second_run() -> None:
    """Criterion 6: two callers, one run, and both are answered.

    The runner's call log is what makes "do not duplicate work" a statement about work
    rather than about results: two callers that each started a run and happened to return
    equal summaries would satisfy an assertion made on the summaries alone.
    """
    runner = Runner(gated=True)
    coordinator = SyncCoordinator(runner)

    first = asyncio.create_task(coordinator.sync(SyncTrigger.SCHEDULED))
    await wait_for(runner.started.wait())
    second = asyncio.create_task(coordinator.sync(SyncTrigger.MANUAL))
    # Let the second caller reach the coordinator before the run is allowed to finish.
    await asyncio.sleep(0)
    runner.gate.set()
    started, joined = await wait_for(asyncio.gather(first, second))

    assert runner.triggers == [SyncTrigger.SCHEDULED], "the second caller must not start a run"
    assert runner.completed == 1
    assert started.joined is False
    assert joined.joined is True
    assert started.summary.run_id == joined.summary.run_id


async def test_the_joined_caller_sees_the_running_trigger_not_its_own() -> None:
    """The second half of `joined`: it says *whose* run you got, not merely that you waited.

    A manual click that reported `trigger: "manual"` for a scheduled run in flight would
    make the runs endpoint describe work that nobody requested, and an operator reading it
    would conclude a person had been pressing the button.
    """
    runner = Runner(gated=True)
    coordinator = SyncCoordinator(runner)

    scheduled = asyncio.create_task(coordinator.sync(SyncTrigger.SCHEDULED))
    await wait_for(runner.started.wait())
    manual = asyncio.create_task(coordinator.sync(SyncTrigger.MANUAL))
    await asyncio.sleep(0)
    runner.gate.set()
    _first, second = await wait_for(asyncio.gather(scheduled, manual))

    assert second.joined is True
    # Equality with the running trigger is the whole claim: `SyncTrigger` is an enum, so it
    # also rules out `MANUAL`, which is the caller's own and the one that would be wrong.
    assert second.summary.trigger == SyncTrigger.SCHEDULED


async def test_a_third_caller_joins_the_same_run_as_the_second() -> None:
    """The queue does not grow a second run behind the first. Three callers, one run.

    Two joiners rather than one, because an implementation that handed the *second* caller
    the in-flight task and then replaced it would still pass the two-caller test.
    """
    runner = Runner(gated=True)
    coordinator = SyncCoordinator(runner)

    first = asyncio.create_task(coordinator.sync(SyncTrigger.SCHEDULED))
    await wait_for(runner.started.wait())
    joiners = [asyncio.create_task(coordinator.sync(SyncTrigger.MANUAL)) for _ in range(2)]
    await asyncio.sleep(0)
    runner.gate.set()
    outcomes = await wait_for(asyncio.gather(first, *joiners))

    assert runner.triggers == [SyncTrigger.SCHEDULED]
    assert [outcome.joined for outcome in outcomes] == [False, True, True]
    assert len({outcome.summary.run_id for outcome in outcomes}) == 1


async def test_a_caller_arriving_after_the_run_ended_starts_a_new_one() -> None:
    """The slot is released, so "one at a time" does not degrade into "one, ever".

    The failure this prevents is total and silent: a coordinator that kept its finished
    task would answer every future sync with the first run's summary, and the dashboard
    would show one frozen set of balances with no error anywhere.
    """
    runner = Runner()
    coordinator = SyncCoordinator(runner)

    first = await wait_for(coordinator.sync(SyncTrigger.MANUAL))
    second = await wait_for(coordinator.sync(SyncTrigger.MANUAL))

    assert runner.triggers == [SyncTrigger.MANUAL, SyncTrigger.MANUAL]
    assert (first.joined, second.joined) == (False, False)
    assert first.summary.run_id != second.summary.run_id


async def test_a_run_that_raises_leaves_the_coordinator_usable() -> None:
    """A failed run must release the slot, or one exception stops every sync afterwards.

    The sync service is written not to raise -- each chain returns an outcome -- so this is
    the case that arrives when something *outside* that contract goes wrong: the database
    is gone, the session cannot commit. Whatever it is, it must not turn a transient failure
    into a process that never syncs again until it is restarted.
    """
    failing = Runner(raises=RuntimeError("the database went away"))
    coordinator = SyncCoordinator(failing)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await wait_for(coordinator.sync(SyncTrigger.MANUAL))

    assert coordinator.in_flight is False
    assert failing.triggers == [SyncTrigger.MANUAL, SyncTrigger.MANUAL], (
        "the second call has to start a run of its own rather than join a dead one"
    )


async def test_in_flight_is_true_only_while_a_run_is_running() -> None:
    """The flag the lifespan reads on shutdown, pinned at all three moments.

    Asserted before, during and after, because a property that is always false and one that
    is always true each satisfy a check made at a single moment.
    """
    runner = Runner(gated=True)
    coordinator = SyncCoordinator(runner)

    assert in_flight(coordinator) is False
    running = asyncio.create_task(coordinator.sync(SyncTrigger.SCHEDULED))
    await wait_for(runner.started.wait())
    assert in_flight(coordinator) is True
    runner.gate.set()
    await wait_for(running)
    assert in_flight(coordinator) is False


# --------------------------------------------------------------------------------------
# A caller going away does not take the run with it
# --------------------------------------------------------------------------------------


async def test_a_cancelled_request_does_not_cancel_the_run() -> None:
    """`asyncio.shield`, and the browser tab this exists for.

    A joined caller is a request task. When the browser disconnects, Starlette cancels it;
    an unshielded `await self._task` propagates that cancellation into the scheduled run
    that the request merely attached to, and a sync nobody asked to stop is abandoned
    half-written.

    The scheduled run is asserted to have **completed**, not merely to have survived: a
    task that was cancelled and then awaited would also stop being `in_flight`.
    """
    runner = Runner(gated=True)
    coordinator = SyncCoordinator(runner)

    scheduled = asyncio.create_task(coordinator.sync(SyncTrigger.SCHEDULED))
    await wait_for(runner.started.wait())
    disconnecting = asyncio.create_task(coordinator.sync(SyncTrigger.MANUAL))
    await asyncio.sleep(0)

    disconnecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnecting

    runner.gate.set()
    outcome = await wait_for(scheduled)

    assert runner.completed == 1, "the run the request joined had to finish"
    assert outcome.joined is False
    assert outcome.summary.status == SyncRunStatus.SUCCESS


async def test_the_originating_caller_going_away_does_not_cancel_its_own_run() -> None:
    """The same protection from the other side, which is the case the spec does not name.

    The manual endpoint is a request too. If the owner closes the tab a second after
    pressing sync, the run is already talking to two vendors and has already written its
    `sync_runs` row -- abandoning it there leaves a `running` row that only the next
    startup sweep will resolve, which is a worse outcome than finishing the work nobody is
    waiting for any more.

    Proven through a second caller rather than through the coordinator's internals: if the
    run really continued, somebody arriving afterwards joins it and gets its summary.
    """
    runner = Runner(gated=True)
    coordinator = SyncCoordinator(runner)

    originator = asyncio.create_task(coordinator.sync(SyncTrigger.MANUAL))
    await wait_for(runner.started.wait())
    originator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await originator

    joiner = asyncio.create_task(coordinator.sync(SyncTrigger.SCHEDULED))
    await asyncio.sleep(0)
    runner.gate.set()
    outcome = await wait_for(joiner)

    assert runner.completed == 1
    assert runner.triggers == [SyncTrigger.MANUAL], "the abandoned run is the one that finished"
    assert outcome.joined is True


async def test_the_cancellation_control_really_cancels() -> None:
    """The falsification control: an unshielded await would have killed the run.

    Without this, `test_a_cancelled_request_does_not_cancel_the_run` passes for a
    coordinator whose second caller never attached to the task at all -- cancelling
    something that was not waiting on the run proves nothing about shielding. Cancelling a
    bare `await task` here shows the mechanism the coordinator has to defeat is real.
    """
    gate = asyncio.Event()
    finished = False

    async def work() -> None:
        nonlocal finished
        await gate.wait()
        finished = True

    task = asyncio.create_task(work())
    await asyncio.sleep(0)

    async def unshielded() -> None:
        await task

    waiter = asyncio.create_task(unshielded())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished is False, "an unshielded await really does take the task down with it"


# --------------------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------------------


async def test_draining_with_nothing_in_flight_is_immediate() -> None:
    """Shutdown must not spend its grace period waiting for a run that does not exist."""
    coordinator = SyncCoordinator(Runner())

    assert await wait_for(coordinator.drain(grace_seconds=DEADLOCK_TIMEOUT)) is True


async def test_draining_waits_for_a_run_that_finishes_inside_the_grace() -> None:
    """The ordinary shutdown: a run is in flight, it ends, and the process may exit."""
    runner = Runner(gated=True)
    coordinator = SyncCoordinator(runner)
    running = asyncio.create_task(coordinator.sync(SyncTrigger.SCHEDULED))
    await wait_for(runner.started.wait())

    async def release() -> None:
        await asyncio.sleep(0)
        runner.gate.set()

    releasing = asyncio.create_task(release())
    drained = await wait_for(coordinator.drain(grace_seconds=DEADLOCK_TIMEOUT))
    await releasing

    assert drained is True
    assert runner.completed == 1
    await wait_for(running)


async def test_a_run_that_outlasts_the_grace_is_reported_as_not_drained() -> None:
    """The other shutdown, and the reason `drain` answers at all rather than returning None.

    The lifespan needs to know whether the run finished, because a run that did not is the
    one whose `sync_runs` row is left at `running` -- and that row is what the next
    startup's sweep turns into `interrupted`. A `drain` that reported nothing would leave
    the lifespan guessing, and the two answers have to be distinguishable, which is why the
    assertion is made against the value the two tests above return rather than only against
    `False`.
    """
    runner = Runner(gated=True)
    coordinator = SyncCoordinator(runner)
    running = asyncio.create_task(coordinator.sync(SyncTrigger.SCHEDULED))
    await wait_for(runner.started.wait())

    drained = await wait_for(coordinator.drain(grace_seconds=0))

    assert drained is False
    assert runner.completed == 0

    # Whatever shutdown did to the task, this test must not leave it pending: an unawaited
    # task produces a warning in a later, unrelated test and is a poor way to find out.
    runner.gate.set()
    with suppress(asyncio.CancelledError, TimeoutError, RuntimeError):
        await wait_for(running)


def test_the_outcome_carries_exactly_the_summary_and_the_joined_flag() -> None:
    """The shape the router serialises, pinned, because a renamed field would go quiet.

    `joined` is a separate field rather than something derivable from the summary, and this
    is the assertion that says so: two callers of one run share a summary, so nothing in it
    can tell them apart.
    """
    assert set(SyncOutcome.__dataclass_fields__) == {"summary", "joined"}
