"""Spec 030 (#23), criterion 8: the state rules behind `GET /api/health/detail`, with literals.

`domain/health.py` is pure, so every rule here is driven with instants and values the test
writes down: no fixture, no clock, no database. Each threshold is pinned at its boundary --
the instant it is exactly reached, which is still the earlier state, and one microsecond past
it -- because `>` written as `>=` is the mutation a boundary-free test cannot see. Each
precedence is pinned with an input that satisfies both rules, so a reordered `if` chain
answers the wrong one.

The properties over any input are in `test_health_property.py`, kept short for the reason
`test_backups_rotation_property.py` gives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from portfolio.domain.accounting import ReconciliationStatus
from portfolio.domain.health import (
    LATE_AFTER_INTERVALS,
    PriceHealthState,
    ReconciliationHealthState,
    ReconciliationSummary,
    SchedulerName,
    SchedulerState,
    SectionState,
    SourceState,
    balances_state,
    price_state,
    scheduler_state,
    source_state,
    summarize_reconciliation,
)

NOW: Final = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
INTERVAL: Final = timedelta(minutes=15)
TICK: Final = timedelta(microseconds=1)
STALE_AFTER: Final = timedelta(hours=2)


# --------------------------------------------------------------------------------------
# The wire forms
# --------------------------------------------------------------------------------------


def test_every_state_is_its_wire_form() -> None:
    """The generated TypeScript unions are built from these strings, so they are pinned.

    Written out rather than read off the enums: a member renamed on the backend has to fail
    here, beside the frontend's wording tables, rather than change the wire silently.
    """
    assert [member.value for member in SchedulerName] == [
        "balance-sync",
        "price-refresh",
        "exchange-sync",
        "backup",
    ]
    assert [member.value for member in SchedulerState] == ["ok", "late", "stopped", "disabled"]
    assert [member.value for member in SectionState] == ["ok", "unavailable"]
    assert [member.value for member in SourceState] == ["ok", "failing", "never"]
    assert [member.value for member in PriceHealthState] == [
        "fresh",
        "stale",
        "never",
        "unavailable",
    ]
    assert [member.value for member in ReconciliationHealthState] == [
        "match",
        "mismatch",
        "incomplete",
        "not_computed",
        "unavailable",
    ]


def test_a_timer_is_late_after_two_intervals() -> None:
    """The spec's number. One would flag every first tick after a restart as late."""
    assert LATE_AFTER_INTERVALS == 2


# --------------------------------------------------------------------------------------
# The timers
# --------------------------------------------------------------------------------------


def timer(
    *,
    running: bool = True,
    started_at: datetime | None = NOW - timedelta(days=1),
    tick_started: datetime | None = None,
    finished: datetime | None = None,
    now: datetime = NOW,
    interval: timedelta = INTERVAL,
) -> SchedulerState:
    """The rule for one timer. `tick_started` defaults to none recorded, so a test that names
    only `finished` is judging an idle timer, and every in-flight case names its tick."""
    return scheduler_state(
        running=running,
        interval=interval,
        started_at=started_at,
        last_tick_started_at=tick_started,
        last_tick_finished_at=finished,
        now=now,
    )


def test_a_timer_whose_task_is_not_running_is_stopped() -> None:
    """Even with a tick a moment ago: the task is gone, so nothing will tick again."""
    assert timer(running=False, finished=NOW) is SchedulerState.STOPPED


def test_a_timer_that_never_started_is_stopped_even_if_called_running() -> None:
    """No start instant means there is no loop to judge, whatever `running` claims."""
    assert timer(running=True, started_at=None, finished=None) is SchedulerState.STOPPED


def test_a_stopped_timer_is_stopped_before_it_is_late() -> None:
    """Precedence: a stopped timer whose last tick is a week old is `stopped`, not `late`."""
    assert timer(running=False, finished=NOW - timedelta(days=7)) is SchedulerState.STOPPED


def test_a_running_timer_with_a_recent_tick_is_ok() -> None:
    assert timer(finished=NOW - timedelta(minutes=1)) is SchedulerState.OK


def test_exactly_two_intervals_after_the_last_tick_is_still_ok() -> None:
    """The boundary: `more than` two intervals. Equal is not late."""
    assert timer(finished=NOW - 2 * INTERVAL) is SchedulerState.OK


def test_one_microsecond_past_two_intervals_is_late() -> None:
    assert timer(finished=NOW - 2 * INTERVAL - TICK) is SchedulerState.LATE


def test_between_one_and_two_intervals_is_ok() -> None:
    """One interval and a half: the multiplier is two, not one."""
    assert timer(finished=NOW - INTERVAL - INTERVAL / 2) is SchedulerState.OK


def test_before_the_first_tick_the_start_is_the_reference() -> None:
    """A loop that started and has not finished a tick is judged from its start."""
    assert timer(started_at=NOW - 2 * INTERVAL, finished=None) is SchedulerState.OK
    assert timer(started_at=NOW - 2 * INTERVAL - TICK, finished=None) is SchedulerState.LATE


def test_a_finished_tick_is_preferred_to_the_start() -> None:
    """Started a day ago and ticked a minute ago is `ok`: the reference is the tick."""
    assert timer(started_at=NOW - timedelta(days=1), finished=NOW - timedelta(minutes=1)) is (
        SchedulerState.OK
    )


def test_an_old_tick_is_late_even_after_a_recent_start() -> None:
    """The other direction: the tick, not the start, is what is compared once there is one."""
    assert timer(started_at=NOW - TICK, finished=NOW - timedelta(days=1)) is SchedulerState.LATE


def test_the_interval_is_the_timers_own() -> None:
    """Forty minutes since a tick: late for a fifteen-minute timer, ok for an hourly one."""
    finished = NOW - timedelta(minutes=40)

    assert timer(finished=finished, interval=timedelta(minutes=15)) is SchedulerState.LATE
    assert timer(finished=finished, interval=timedelta(hours=1)) is SchedulerState.OK


def test_a_tick_after_now_is_not_late() -> None:
    """The clock stepped back: a reference in the future is not a late timer."""
    assert timer(finished=NOW + timedelta(hours=1)) is SchedulerState.OK


# A tick in flight (R13, S4). The loop sleeps an interval *after* a tick finishes, so the
# next tick starts one interval after the last finish: judged from that finish, every tick
# would be late one interval into its run rather than two.


def test_a_tick_in_flight_is_judged_from_its_own_start() -> None:
    """The last finish is three intervals old, but the tick that started after it is exactly
    two intervals into its run: `ok`. Judged from the finish, this would be `late`."""
    finished = NOW - 3 * INTERVAL

    assert timer(tick_started=NOW - 2 * INTERVAL, finished=finished) is SchedulerState.OK
    assert timer(tick_started=NOW - 2 * INTERVAL - TICK, finished=finished) is (SchedulerState.LATE)


def test_a_first_tick_in_flight_is_judged_from_its_own_start_not_the_loops() -> None:
    """No tick has finished: the loop started a day ago, its first tick two intervals ago.
    Judged from the loop's start, this would be `late`."""
    started_at = NOW - timedelta(days=1)

    assert timer(started_at=started_at, tick_started=NOW - 2 * INTERVAL) is SchedulerState.OK
    assert timer(started_at=started_at, tick_started=NOW - 2 * INTERVAL - TICK) is (
        SchedulerState.LATE
    )


def test_a_tick_that_started_before_the_last_finish_is_not_in_flight() -> None:
    """Idle: the last tick began a day ago and finished. It is judged from its finish, so a
    start a day old does not make it `late`, and a finish over two intervals old does."""
    tick_started = NOW - timedelta(days=1)

    assert timer(tick_started=tick_started, finished=NOW - 2 * INTERVAL) is SchedulerState.OK
    assert timer(tick_started=tick_started, finished=NOW - 2 * INTERVAL - TICK) is (
        SchedulerState.LATE
    )


def test_an_idle_timer_with_a_recent_tick_start_is_still_judged_from_its_finish() -> None:
    """The other direction: an idle timer whose tick started and finished long ago is `late`
    even though the loop itself was started a moment ago."""
    assert timer(
        started_at=NOW - TICK,
        tick_started=NOW - timedelta(days=1, minutes=5),
        finished=NOW - timedelta(days=1),
    ) is (SchedulerState.LATE)


def test_a_stopped_timer_is_stopped_with_a_tick_in_flight_however_old() -> None:
    """Precedence again: a cancelled loop leaves a start with no finish behind it."""
    assert timer(running=False, tick_started=NOW - timedelta(days=7)) is SchedulerState.STOPPED


def test_a_tick_in_flight_that_started_after_now_is_not_late() -> None:
    """The clock stepped back during the tick: not `late`, as for a finish after `now`."""
    assert timer(tick_started=NOW + timedelta(hours=1), finished=NOW - timedelta(days=1)) is (
        SchedulerState.OK
    )


def test_a_timer_rule_never_answers_disabled() -> None:
    """`disabled` is the service's answer for a timer that was never built."""
    answers = {
        timer(running=running, started_at=started, tick_started=tick, finished=finished)
        for running in (True, False)
        for started in (None, NOW - timedelta(days=1))
        for tick in (None, NOW, NOW - timedelta(days=1))
        for finished in (None, NOW, NOW - timedelta(days=1))
    }

    assert answers == {SchedulerState.OK, SchedulerState.LATE, SchedulerState.STOPPED}


# --------------------------------------------------------------------------------------
# A source's last attempt
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("succeeded", "state"),
    [(None, SourceState.NEVER), (True, SourceState.OK), (False, SourceState.FAILING)],
)
def test_a_source_is_what_its_last_attempt_says(succeeded: bool | None, state: SourceState) -> None:
    assert source_state(succeeded) is state


def test_a_failed_balance_read_is_failing_even_with_an_older_reading() -> None:
    """The reading has stopped being refreshed, so it is not `ok` because one exists."""
    assert balances_state(read_at=NOW, failed=True) is SourceState.FAILING
    assert balances_state(read_at=None, failed=True) is SourceState.FAILING


def test_a_reading_with_no_failure_is_ok() -> None:
    assert balances_state(read_at=NOW, failed=False) is SourceState.OK


def test_no_reading_and_no_failure_is_never() -> None:
    assert balances_state(read_at=None, failed=False) is SourceState.NEVER


# --------------------------------------------------------------------------------------
# The prices
# --------------------------------------------------------------------------------------


def test_no_price_row_is_never() -> None:
    assert price_state(None, now=NOW, stale_after=STALE_AFTER) is PriceHealthState.NEVER


def test_a_price_exactly_at_the_age_limit_is_still_fresh() -> None:
    """The dashboard flags a price by `>`; the section turns stale at the same instant."""
    latest = NOW - STALE_AFTER

    assert price_state(latest, now=NOW, stale_after=STALE_AFTER) is PriceHealthState.FRESH


def test_a_price_one_microsecond_past_the_limit_is_stale() -> None:
    latest = NOW - STALE_AFTER - TICK

    assert price_state(latest, now=NOW, stale_after=STALE_AFTER) is PriceHealthState.STALE


def test_the_age_limit_is_the_callers() -> None:
    """Ninety minutes old: stale against an hour, fresh against two."""
    latest = NOW - timedelta(minutes=90)

    assert price_state(latest, now=NOW, stale_after=timedelta(hours=1)) is (PriceHealthState.STALE)
    assert price_state(latest, now=NOW, stale_after=timedelta(hours=2)) is (PriceHealthState.FRESH)


def test_a_price_newer_than_now_is_fresh() -> None:
    latest = NOW + timedelta(minutes=5)

    assert price_state(latest, now=NOW, stale_after=STALE_AFTER) is PriceHealthState.FRESH


# --------------------------------------------------------------------------------------
# The holdings check, reduced
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    status: ReconciliationStatus


@dataclass(frozen=True)
class Venue:
    not_compared_reason: object | None = None


@dataclass(frozen=True)
class Wallets:
    stale: int = 0
    unread: int = 0
    chain_failed: int = 0


@dataclass(frozen=True)
class View:
    """A `ReconciliationLike` with literals: what `services.reconciliation` would serve."""

    computed_at: datetime | None = NOW
    assets: tuple[Asset, ...] = ()
    exchanges: tuple[Venue, ...] = ()
    wallets: Wallets = field(default_factory=Wallets)


MATCH: Final = Asset(ReconciliationStatus.MATCH)
SHORT: Final = Asset(ReconciliationStatus.HISTORY_SHORT)
OVER: Final = Asset(ReconciliationStatus.HISTORY_OVER)
LEFT_OUT: Final = Venue(not_compared_reason="read_failed")


def test_every_asset_matching_and_every_source_compared_is_match() -> None:
    summary = summarize_reconciliation(View(assets=(MATCH, MATCH), exchanges=(Venue(),)))

    assert summary == ReconciliationSummary(
        state=ReconciliationHealthState.MATCH,
        computed_at=NOW,
        assets_compared=2,
        assets_mismatched=0,
        sources_not_compared=0,
    )


def test_a_reconciliation_with_nothing_to_compare_is_match() -> None:
    """R8: an owner with no history and nothing held has nothing that disagrees."""
    summary = summarize_reconciliation(View())

    assert summary.state is ReconciliationHealthState.MATCH
    assert (summary.assets_compared, summary.assets_mismatched, summary.sources_not_compared) == (
        0,
        0,
        0,
    )


@pytest.mark.parametrize("asset", [SHORT, OVER], ids=["history_short", "history_over"])
def test_any_asset_that_is_not_match_is_mismatch(asset: Asset) -> None:
    """Both non-match statuses count, `history_over` included, though it is not a finding."""
    summary = summarize_reconciliation(View(assets=(MATCH, asset, MATCH)))

    assert summary.state is ReconciliationHealthState.MISMATCH
    assert summary.assets_compared == 3
    assert summary.assets_mismatched == 1


def test_the_mismatched_count_is_every_asset_not_matching() -> None:
    summary = summarize_reconciliation(View(assets=(SHORT, OVER, MATCH, SHORT)))

    assert summary.assets_mismatched == 3
    assert summary.assets_compared == 4


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        pytest.param(View(exchanges=(Venue(), LEFT_OUT)), 1, id="an exchange not compared"),
        pytest.param(View(wallets=Wallets(stale=2)), 2, id="stale wallets"),
        pytest.param(View(wallets=Wallets(unread=3)), 3, id="unread wallets"),
        pytest.param(View(wallets=Wallets(chain_failed=1)), 1, id="a wallet on a failed chain"),
    ],
)
def test_each_kind_of_source_left_out_makes_it_incomplete(view: View, expected: int) -> None:
    summary = summarize_reconciliation(
        View(assets=(MATCH,), exchanges=view.exchanges, wallets=view.wallets)
    )

    assert summary.state is ReconciliationHealthState.INCOMPLETE
    assert summary.sources_not_compared == expected


def test_the_sources_not_compared_add_up_across_every_kind() -> None:
    view = View(
        exchanges=(LEFT_OUT, Venue(), Venue(not_compared_reason="out_of_date")),
        wallets=Wallets(stale=1, unread=2, chain_failed=3),
    )

    assert summarize_reconciliation(view).sources_not_compared == 2 + 1 + 2 + 3


def test_mismatch_comes_before_incomplete() -> None:
    """Precedence: a disagreeing asset is the finding, even with a source left out."""
    view = View(assets=(SHORT,), exchanges=(LEFT_OUT,), wallets=Wallets(unread=1))

    summary = summarize_reconciliation(view)

    assert summary.state is ReconciliationHealthState.MISMATCH
    assert summary.sources_not_compared == 2


def test_not_computed_comes_first_of_all() -> None:
    """Precedence: with no snapshot, nothing else is judged -- but the sources are counted."""
    view = View(
        computed_at=None,
        assets=(SHORT,),
        exchanges=(LEFT_OUT,),
        wallets=Wallets(stale=1),
    )

    summary = summarize_reconciliation(view)

    assert summary.state is ReconciliationHealthState.NOT_COMPUTED
    assert summary.computed_at is None
    assert summary.sources_not_compared == 2


def test_not_computed_with_nothing_else_is_not_computed() -> None:
    summary = summarize_reconciliation(View(computed_at=None))

    assert summary == ReconciliationSummary(
        state=ReconciliationHealthState.NOT_COMPUTED,
        computed_at=None,
        assets_compared=0,
        assets_mismatched=0,
        sources_not_compared=0,
    )


def test_the_summary_carries_the_snapshots_instant() -> None:
    computed_at = datetime(2026, 9, 30, 8, 15, 30, tzinfo=UTC)

    assert summarize_reconciliation(View(computed_at=computed_at)).computed_at == computed_at
