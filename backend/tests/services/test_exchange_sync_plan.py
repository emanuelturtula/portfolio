"""The pure arithmetic of #15's planning: clamp, newest first, overlap, halves and waits.

`services/exchange_sync_plan.py` has no I/O and no clock, so everything here is exact to the
millisecond and nothing is a fixture. Every expected value is written out from the spec's
own numbers -- five minutes, one day, three steps, sixty seconds -- rather than read back off
the module's constants, because an expectation built from the constant under test agrees
with any value the constant is given. The constants are pinned separately, by hand.

The properties that must hold for *any* input -- windows tile a range with no gap and no
instant twice, newest first -- are asserted over generated inputs with `hypothesis`, the
treatment `tests/providers/exchanges/test_base.py` gives the fill arithmetic.

`plan_account` takes a `max_window` the spec's signature does not have: the plan is
persisted before any fetch, so its windows are already the query windows. The tests pass a
thirty-day limit, Bitget's, so a first sync of eighty-five days is three windows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.providers.exchanges.base import (
    EPOCH,
    FillWindow,
    RetentionClamp,
    floor_to_millisecond,
)
from portfolio.services.exchange_sync_plan import (
    HISTORY_GENESIS,
    MAX_RATE_LIMIT_WAIT_SECONDS,
    MAX_RETENTION_STEPS,
    OVERLAP,
    RATE_LIMIT_RETRIES,
    RETENTION_STEP,
    PendingWindow,
    Replacement,
    normalise_pending,
    plan_account,
    seconds_to_wait,
    split_in_half,
    split_newest_first,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The instant most tests plan at: a whole millisecond, so a plan built from it is exact.
NOW: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
ONE_MS: Final = timedelta(milliseconds=1)
#: The spec's overlap, written out rather than read off `OVERLAP`.
FIVE_MINUTES: Final = timedelta(minutes=5)
#: Bitget's `max_query_window`, the limit every plan here is split to.
MAX_WINDOW: Final = timedelta(days=30)


def ms(value: int) -> datetime:
    """An epoch-millisecond count as an aware instant."""
    return EPOCH + timedelta(milliseconds=value)


def clamp(requested: datetime, effective: datetime | None = None) -> RetentionClamp:
    return RetentionClamp(
        requested_since=requested,
        effective_since=effective if effective is not None else requested,
    )


def covered(windows: Sequence[FillWindow]) -> list[tuple[datetime, datetime]]:
    """The instants `windows` cover, as sorted, merged half-open intervals.

    Merging touching and overlapping windows, so the answer is a statement about *what is
    read* and not about how it was cut.
    """
    merged: list[tuple[datetime, datetime]] = []
    for window in sorted(windows, key=lambda each: each.since):
        if merged and window.since <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], window.until))
        else:
            merged.append((window.since, window.until))
    return merged


def assert_newest_first(windows: Sequence[FillWindow]) -> None:
    """Each window ends before the one listed ahead of it ends. Strictly, by `until`."""
    untils = [window.until for window in windows]
    assert untils == sorted(untils, reverse=True), "the windows are not newest first"
    assert len(set(untils)) == len(untils), "two windows end at the same instant"


def assert_within_limit(windows: Sequence[FillWindow], limit: timedelta = MAX_WINDOW) -> None:
    assert all(window.duration <= limit for window in windows), "a window exceeds the limit"


def pending(window_id: int, since: datetime, until: datetime, **fields: str) -> PendingWindow:
    return PendingWindow(
        window_id=window_id,
        window=FillWindow(since=since, until=until),
        symbol=fields.get("symbol"),
        cursor=fields.get("cursor"),
    )


# --------------------------------------------------------------------------------------
# The spec's numbers, pinned by hand
# --------------------------------------------------------------------------------------


def test_the_planning_constants_are_the_specs() -> None:
    """Each number is a decision the spec made, and each has a failure behind it.

    `OVERLAP` at zero re-reads nothing, so a fill a venue indexes a moment late -- settled
    after the previous run's top window closed -- is never imported. The retry count off by
    one is a fourth or a fifth attempt against a venue that is already refusing us.
    """
    assert datetime(2009, 1, 3, tzinfo=UTC) == HISTORY_GENESIS
    assert timedelta(minutes=5) == OVERLAP
    assert timedelta(days=1) == RETENTION_STEP
    assert MAX_RETENTION_STEPS == 3
    assert RATE_LIMIT_RETRIES == 3
    assert MAX_RATE_LIMIT_WAIT_SECONDS == 60


# --------------------------------------------------------------------------------------
# split_newest_first
# --------------------------------------------------------------------------------------


def test_a_range_is_split_newest_first_with_the_oldest_window_shorter() -> None:
    """Twenty days in seven-day windows: 7, 7 and a 6-day remainder at the old end."""
    since = NOW - timedelta(days=20)

    windows = split_newest_first(since, NOW, max_window=timedelta(days=7))

    assert windows == (
        FillWindow(since=NOW - timedelta(days=7), until=NOW),
        FillWindow(since=NOW - timedelta(days=14), until=NOW - timedelta(days=7)),
        FillWindow(since=since, until=NOW - timedelta(days=14)),
    )


def test_an_exact_multiple_leaves_no_empty_remainder() -> None:
    windows = split_newest_first(NOW - timedelta(days=14), NOW, max_window=timedelta(days=7))

    assert [window.duration for window in windows] == [timedelta(days=7)] * 2


def test_a_range_shorter_than_the_limit_is_one_window() -> None:
    since = NOW - timedelta(hours=3)

    assert split_newest_first(since, NOW, max_window=timedelta(days=7)) == (
        FillWindow(since=since, until=NOW),
    )


def test_a_range_of_one_millisecond_is_one_window() -> None:
    assert split_newest_first(NOW - ONE_MS, NOW, max_window=timedelta(days=7)) == (
        FillWindow(since=NOW - ONE_MS, until=NOW),
    )


@pytest.mark.parametrize("gap", [timedelta(0), -ONE_MS, -timedelta(days=1)])
def test_an_empty_or_inverted_range_is_no_windows(gap: timedelta) -> None:
    """`()` rather than a `ValueError`: a clock stepped back is a plan with nothing in it."""
    assert split_newest_first(NOW, NOW + gap, max_window=timedelta(days=7)) == ()


@settings(max_examples=300, deadline=None)
@given(
    since_ms=st.integers(min_value=0, max_value=4_000_000_000_000),
    window_ms=st.integers(min_value=1, max_value=90 * 86_400_000),
    whole_windows=st.integers(min_value=0, max_value=300),
    data=st.data(),
)
def test_windows_tile_the_range_exactly_for_any_input(
    since_ms: int, window_ms: int, whole_windows: int, data: st.DataObject
) -> None:
    """End to end, newest first, none longer than the limit, only the oldest shorter.

    The range is drawn as a number of whole windows plus a tail of one millisecond up to a
    whole window, so exact multiples are drawn as often as ragged ones and no example asks
    for billions of windows. Checked with the windows' own `FillWindow` type, so every
    bound is also a whole millisecond: `FillWindow` refuses anything else at construction.
    """
    tail_ms = data.draw(st.integers(min_value=1, max_value=window_ms), label="tail_ms")
    length_ms = whole_windows * window_ms + tail_ms
    since, until = ms(since_ms), ms(since_ms + length_ms)
    limit = timedelta(milliseconds=window_ms)

    windows = split_newest_first(since, until, max_window=limit)

    assert windows, "a non-empty range produced no window"
    assert windows[0].until == until
    assert windows[-1].since == since
    for newer, older in pairwise(windows):
        assert older.until == newer.since, "a gap or an overlap between two windows"
    assert_within_limit(windows, limit)
    assert all(window.duration == limit for window in windows[:-1]), (
        "only the oldest window may be shorter than the limit"
    )
    assert len(windows) == -(-length_ms // window_ms)


# --------------------------------------------------------------------------------------
# plan_account
# --------------------------------------------------------------------------------------


def test_a_first_sync_plans_from_the_clamp_to_now_in_query_windows() -> None:
    """No history planned yet: `[clamp.effective_since, now)`, split newest first.

    Eighty-five days at thirty is 30, 30 and 25, the short one at the old end.
    """
    effective = NOW - timedelta(days=85)

    plan = plan_account(
        requested_since=None,
        effective_since=None,
        planned_until=None,
        clamp=clamp(HISTORY_GENESIS, effective),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == (
        FillWindow(since=NOW - timedelta(days=30), until=NOW),
        FillWindow(since=NOW - timedelta(days=60), until=NOW - timedelta(days=30)),
        FillWindow(since=effective, until=NOW - timedelta(days=60)),
    )
    assert plan.effective_since == effective, "the clamp, never the 2009 request"
    assert plan.planned_until == NOW


@pytest.mark.parametrize(
    ("effective_since", "planned_until"),
    [(None, NOW - timedelta(days=1)), (NOW - timedelta(days=2), None)],
    ids=["no floor", "no ceiling"],
)
def test_either_bound_missing_is_a_first_sync(
    effective_since: datetime | None, planned_until: datetime | None
) -> None:
    """A half-recorded plan is re-planned from the clamp rather than trusted."""
    effective = NOW - timedelta(days=10)

    plan = plan_account(
        requested_since=None,
        effective_since=effective_since,
        planned_until=planned_until,
        clamp=clamp(effective),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert covered(plan.windows) == [(effective, NOW)]
    assert (plan.effective_since, plan.planned_until) == (effective, NOW)


def test_now_is_floored_to_a_whole_millisecond() -> None:
    """Spec 012's handed-on rule: every bound is built from a floored `now`.

    A `now` carrying microseconds would produce a `FillWindow` that refuses itself -- or,
    worse, a `planned_until` off the grid that the next run's top window starts from.
    """
    ragged = NOW + timedelta(microseconds=123_456)
    effective = NOW - timedelta(days=1)

    plan = plan_account(
        requested_since=None,
        effective_since=None,
        planned_until=None,
        clamp=clamp(effective),
        now=ragged,
        max_window=MAX_WINDOW,
    )

    assert plan.planned_until == NOW + timedelta(milliseconds=123)
    assert plan.planned_until == floor_to_millisecond(ragged)
    assert covered(plan.windows) == [(effective, NOW + timedelta(milliseconds=123))]


def test_a_later_sync_reaches_back_exactly_five_minutes_into_planned_history() -> None:
    """The top window starts at `planned_until - 5 minutes`, not at `planned_until`.

    The overlap is what catches a fill the venue indexed a moment after the previous run
    read past it. The constraint makes re-reading it free.
    """
    effective = NOW - timedelta(days=30)
    planned_until = NOW - timedelta(hours=1)

    plan = plan_account(
        requested_since=HISTORY_GENESIS,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, NOW - timedelta(days=85)),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == (FillWindow(since=planned_until - FIVE_MINUTES, until=NOW),)
    assert plan.effective_since == effective, "a later sync does not move the floor"
    assert plan.planned_until == NOW


def test_a_long_gap_since_the_last_sync_is_split_into_query_windows() -> None:
    """Forty days since the last plan: the top is split too, newest first."""
    effective = NOW - timedelta(days=60)
    planned_until = NOW - timedelta(days=40)

    plan = plan_account(
        requested_since=HISTORY_GENESIS,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, NOW - timedelta(days=85)),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == (
        FillWindow(since=NOW - timedelta(days=30), until=NOW),
        FillWindow(since=planned_until - FIVE_MINUTES, until=NOW - timedelta(days=30)),
    )


def test_retention_passing_the_floor_but_not_the_ceiling_leaves_the_floor() -> None:
    """History already held stays held when the rolling retention passes it.

    The edge is inside the planned range, so there is no hole: the floor is what the
    database holds, not what the venue still has.
    """
    effective = NOW - timedelta(days=200)
    planned_until = NOW - timedelta(days=1)
    edge = NOW - timedelta(days=85)

    plan = plan_account(
        requested_since=HISTORY_GENESIS,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, edge),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == (FillWindow(since=planned_until - FIVE_MINUTES, until=NOW),)
    assert plan.effective_since == effective


def test_the_top_window_starts_no_earlier_than_retention_allows() -> None:
    """`max(planned_until - OVERLAP, clamp.effective_since)`: the overlap is not asked of a
    venue that no longer holds it."""
    effective = NOW - timedelta(days=200)
    edge = NOW - timedelta(days=85)
    planned_until = edge + timedelta(minutes=2)

    plan = plan_account(
        requested_since=HISTORY_GENESIS,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, edge),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert covered(plan.windows) == [(edge, NOW)]
    assert plan.effective_since == effective, "no hole: the edge is below the ceiling"


def test_a_stall_longer_than_retention_moves_the_floor_past_the_hole() -> None:
    """The departure backend-dev flagged: a hole in the history is not claimed as held.

    An account that did not sync for longer than the venue keeps -- a key that was
    `auth_failed` for a hundred days -- has a gap between its old ceiling and the retention
    edge that no request can fill. Leaving the floor where it was would make
    `[effective_since, planned_until)` claim a complete history across that gap; the floor
    moves to the edge instead, and `history_truncated` says so.
    """
    effective = NOW - timedelta(days=200)
    planned_until = NOW - timedelta(days=100)
    edge = NOW - timedelta(days=85)

    plan = plan_account(
        requested_since=HISTORY_GENESIS,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, edge),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert covered(plan.windows) == [(edge, NOW)]
    assert plan.effective_since == edge
    assert plan.planned_until == NOW


def test_an_edge_exactly_at_the_ceiling_is_not_a_hole() -> None:
    """The top then abuts the planned range: nothing is lost, and the floor stays."""
    effective = NOW - timedelta(days=200)
    planned_until = NOW - timedelta(days=85)

    plan = plan_account(
        requested_since=HISTORY_GENESIS,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, planned_until),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert covered(plan.windows) == [(planned_until, NOW)]
    assert plan.effective_since == effective


@pytest.mark.parametrize(
    "stepped_back",
    [timedelta(0), timedelta(milliseconds=1), timedelta(hours=2)],
    ids=["equal", "a millisecond back", "two hours back"],
)
def test_a_clock_stepped_back_plans_nothing_new_at_the_top(stepped_back: timedelta) -> None:
    """`now <= planned_until` is a clock that went backwards, not a range to read.

    Nothing is planned and `planned_until` is not moved back with it: the history up to it
    is planned already, and pulling the ceiling down would re-plan it on the next run.
    """
    planned_until = NOW
    effective = NOW - timedelta(days=30)

    plan = plan_account(
        requested_since=HISTORY_GENESIS,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, NOW - timedelta(days=85)),
        now=NOW - stepped_back,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == ()
    assert plan.planned_until == planned_until
    assert plan.effective_since == effective


def test_moving_the_history_start_earlier_plans_the_bottom_range() -> None:
    """The owner asks for more history than was planned, and retention still has it.

    The bottom is `[clamp.effective_since, effective_since)` and it abuts the planned range;
    the floor moves down to the clamp. Forty days of bottom is split at thirty, newest first,
    and all of it comes after the top.
    """
    effective = NOW - timedelta(days=10)
    planned_until = NOW - timedelta(hours=1)
    earlier = NOW - timedelta(days=50)

    plan = plan_account(
        requested_since=effective,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(earlier),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == (
        FillWindow(since=planned_until - FIVE_MINUTES, until=NOW),
        FillWindow(since=NOW - timedelta(days=10) - MAX_WINDOW, until=effective),
        FillWindow(since=earlier, until=NOW - timedelta(days=10) - MAX_WINDOW),
    )
    assert plan.effective_since == earlier
    assert plan.planned_until == NOW


def test_a_bottom_range_alone_is_planned_when_the_clock_has_not_moved() -> None:
    """The history start moved earlier and no time has passed: bottom only."""
    effective = NOW - timedelta(days=10)
    earlier = NOW - timedelta(days=20)

    plan = plan_account(
        requested_since=effective,
        effective_since=effective,
        planned_until=NOW,
        clamp=clamp(earlier),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == (FillWindow(since=earlier, until=effective),)
    assert plan.effective_since == earlier
    assert plan.planned_until == NOW


def test_a_clamp_equal_to_the_floor_plans_no_bottom() -> None:
    """`clamp.effective_since == effective_since` is nothing new below: the `<` is strict."""
    effective = NOW - timedelta(days=10)
    planned_until = NOW - timedelta(hours=1)

    plan = plan_account(
        requested_since=effective + timedelta(days=1),
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(effective),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == (FillWindow(since=planned_until - FIVE_MINUTES, until=NOW),)
    assert plan.effective_since == effective


def test_a_floor_above_the_clamp_is_not_re_planned_unless_the_owner_asked_for_more() -> None:
    """The case a retention step leaves: the floor was stepped above the declared edge.

    The owner asked for nothing new -- the same history start as last time -- so the history
    between the edge and the floor is history the venue refused, and asking again would be
    refused again, every run.
    """
    effective = NOW - timedelta(days=88)
    planned_until = NOW - timedelta(minutes=15)

    plan = plan_account(
        requested_since=HISTORY_GENESIS,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, NOW - timedelta(days=90) + FIVE_MINUTES),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == (FillWindow(since=planned_until - FIVE_MINUTES, until=NOW),)
    assert plan.effective_since == effective


def test_an_unrecorded_request_beside_a_recorded_floor_is_not_a_request_for_more() -> None:
    """A state this application never writes, read conservatively: no bottom."""
    effective = NOW - timedelta(days=10)

    plan = plan_account(
        requested_since=None,
        effective_since=effective,
        planned_until=NOW,
        clamp=clamp(NOW - timedelta(days=40)),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == ()
    assert plan.effective_since == effective


def test_a_later_request_than_recorded_plans_no_bottom() -> None:
    """The owner moved the start *later*: nothing held is dropped, and nothing below planned."""
    effective = NOW - timedelta(days=10)

    plan = plan_account(
        requested_since=NOW - timedelta(days=40),
        effective_since=effective,
        planned_until=NOW,
        clamp=clamp(NOW - timedelta(days=5)),
        now=NOW,
        max_window=MAX_WINDOW,
    )

    assert plan.windows == ()
    assert plan.effective_since == effective


@settings(max_examples=300, deadline=None)
@given(
    floor_offset=st.integers(min_value=1, max_value=400 * 86_400_000),
    planned_length=st.integers(min_value=FIVE_MINUTES // ONE_MS + 1, max_value=90 * 86_400_000),
    clamp_offset=st.integers(min_value=-400 * 86_400_000, max_value=400 * 86_400_000),
    elapsed=st.integers(min_value=-86_400_000, max_value=120 * 86_400_000),
    asked_for_more=st.booleans(),
)
def test_every_planned_range_touches_the_planned_history(
    floor_offset: int, planned_length: int, clamp_offset: int, elapsed: int, asked_for_more: bool
) -> None:
    """Contiguity by construction, for any state the clamp still reaches.

    Whenever the retention edge is no later than the planned ceiling, the planned history
    and everything newly planned form one interval: the top overlaps it, the bottom abuts
    it. The floor falls only when the owner asked for more, never rises here, the ceiling
    never falls, every window is within the limit, and none asks for history the clamp says
    is gone.
    """
    effective = NOW - timedelta(milliseconds=floor_offset)
    planned_until = effective + timedelta(milliseconds=planned_length)
    now = planned_until + timedelta(milliseconds=elapsed)
    edge = min(effective + timedelta(milliseconds=clamp_offset), planned_until, now)
    recorded_request = HISTORY_GENESIS + (ONE_MS if asked_for_more else timedelta(0))

    plan = plan_account(
        requested_since=recorded_request,
        effective_since=effective,
        planned_until=planned_until,
        clamp=clamp(HISTORY_GENESIS, edge),
        now=now,
        max_window=MAX_WINDOW,
    )

    assert plan.effective_since == (min(effective, edge) if asked_for_more else effective)
    assert plan.planned_until == max(planned_until, now)
    assert_newest_first(plan.windows)
    assert_within_limit(plan.windows)
    union = covered([FillWindow(since=effective, until=planned_until), *plan.windows])
    assert union == [(plan.effective_since, plan.planned_until)]
    for window in plan.windows:
        assert window.since >= edge, "a window asks for history retention does not hold"


# --------------------------------------------------------------------------------------
# normalise_pending
# --------------------------------------------------------------------------------------

FLOOR: Final = NOW - timedelta(days=85)


def test_windows_inside_the_floor_and_the_limit_are_kept_as_they_are() -> None:
    """Cursor and symbol included: a window nothing touches resumes where it stopped."""
    windows = [
        pending(7, NOW - timedelta(days=10), NOW, cursor="c-7", symbol="BTCUSDT"),
        pending(8, FLOOR, FLOOR + timedelta(days=1)),
    ]

    queue = normalise_pending(windows, floor=FLOOR, max_window=MAX_WINDOW)

    assert queue.kept == tuple(windows)
    assert queue.replaced == ()
    assert queue.truncated is False


def test_a_window_ending_exactly_at_the_floor_is_dropped() -> None:
    """`until <= floor` is wholly below: the window holds no instant the venue still has.

    The boundary that a `<` would keep -- and then have to shrink to an empty window, which
    `FillWindow` refuses.
    """
    at_floor = pending(3, FLOOR - timedelta(days=1), FLOOR, cursor="c-3")

    queue = normalise_pending([at_floor], floor=FLOOR, max_window=MAX_WINDOW)

    assert queue.kept == ()
    assert queue.replaced == (Replacement(original=at_floor, windows=()),)
    assert queue.truncated is True


def test_a_window_wholly_below_the_floor_is_dropped() -> None:
    below = pending(4, FLOOR - timedelta(days=5), FLOOR - timedelta(days=4))

    queue = normalise_pending([below], floor=FLOOR, max_window=MAX_WINDOW)

    assert queue.replaced == (Replacement(original=below, windows=()),)
    assert queue.truncated is True


def test_a_window_straddling_the_floor_has_its_since_moved_up() -> None:
    straddling = pending(5, FLOOR - timedelta(days=2), FLOOR + timedelta(days=3), cursor="c-5")

    queue = normalise_pending([straddling], floor=FLOOR, max_window=MAX_WINDOW)

    assert queue.kept == ()
    assert queue.replaced == (
        Replacement(
            original=straddling,
            windows=(FillWindow(since=FLOOR, until=FLOOR + timedelta(days=3)),),
        ),
    )
    assert queue.truncated is True


def test_a_window_starting_exactly_at_the_floor_is_kept() -> None:
    at_floor = pending(6, FLOOR, FLOOR + timedelta(days=3))

    queue = normalise_pending([at_floor], floor=FLOOR, max_window=MAX_WINDOW)

    assert queue.kept == (at_floor,)
    assert queue.truncated is False


def test_a_window_longer_than_the_limit_is_re_split_without_truncation() -> None:
    """A code change that shrank `max_query_window`: nothing is lost, only re-cut."""
    long = pending(9, NOW - timedelta(days=50), NOW, cursor="c-9")

    queue = normalise_pending([long], floor=FLOOR, max_window=MAX_WINDOW)

    assert queue.kept == ()
    assert queue.replaced == (
        Replacement(
            original=long,
            windows=(
                FillWindow(since=NOW - timedelta(days=30), until=NOW),
                FillWindow(since=NOW - timedelta(days=50), until=NOW - timedelta(days=30)),
            ),
        ),
    )
    assert queue.truncated is False


def test_a_window_both_straddling_and_too_long_is_moved_then_split() -> None:
    both = pending(10, FLOOR - timedelta(days=5), FLOOR + timedelta(days=40))

    queue = normalise_pending([both], floor=FLOOR, max_window=MAX_WINDOW)

    assert queue.replaced == (
        Replacement(
            original=both,
            windows=(
                FillWindow(since=FLOOR + timedelta(days=10), until=FLOOR + timedelta(days=40)),
                FillWindow(since=FLOOR, until=FLOOR + timedelta(days=10)),
            ),
        ),
    )
    assert queue.truncated is True


def test_a_mixed_queue_keeps_what_it_can_and_reports_truncation_once() -> None:
    fine = pending(1, NOW - timedelta(days=3), NOW)
    gone = pending(2, FLOOR - timedelta(days=9), FLOOR - timedelta(days=8))
    long = pending(3, NOW - timedelta(days=70), NOW - timedelta(days=3))

    queue = normalise_pending([fine, gone, long], floor=FLOOR, max_window=MAX_WINDOW)

    assert queue.kept == (fine,)
    assert {replacement.original for replacement in queue.replaced} == {gone, long}
    assert queue.truncated is True


def test_an_empty_queue_is_nothing_to_do() -> None:
    queue = normalise_pending([], floor=FLOOR, max_window=MAX_WINDOW)

    assert (queue.kept, queue.replaced, queue.truncated) == ((), (), False)


# --------------------------------------------------------------------------------------
# split_in_half
# --------------------------------------------------------------------------------------


def test_a_window_is_split_into_newer_then_older_halves() -> None:
    window = FillWindow(since=NOW - timedelta(hours=2), until=NOW)

    newer, older = split_in_half(window)

    assert newer == FillWindow(since=NOW - timedelta(hours=1), until=NOW)
    assert older == FillWindow(since=NOW - timedelta(hours=2), until=NOW - timedelta(hours=1))


def test_an_odd_window_floors_its_midpoint_to_a_whole_millisecond() -> None:
    """Three milliseconds: the midpoint is `since + 1 ms`, so the older half is the shorter."""
    window = FillWindow(since=NOW, until=NOW + 3 * ONE_MS)

    newer, older = split_in_half(window)

    assert older == FillWindow(since=NOW, until=NOW + ONE_MS)
    assert newer == FillWindow(since=NOW + ONE_MS, until=NOW + 3 * ONE_MS)


def test_the_smallest_splittable_window_is_two_milliseconds() -> None:
    newer, older = split_in_half(FillWindow(since=NOW, until=NOW + 2 * ONE_MS))

    assert newer == FillWindow(since=NOW + ONE_MS, until=NOW + 2 * ONE_MS)
    assert older == FillWindow(since=NOW, until=NOW + ONE_MS)


def test_a_one_millisecond_window_cannot_be_split() -> None:
    with pytest.raises(ValueError, match="two milliseconds"):
        split_in_half(FillWindow(since=NOW, until=NOW + ONE_MS))


@settings(max_examples=300, deadline=None)
@given(
    since_ms=st.integers(min_value=0, max_value=4_000_000_000_000),
    length_ms=st.integers(min_value=2, max_value=90 * 86_400_000),
)
def test_the_halves_tile_the_window_for_any_length(since_ms: int, length_ms: int) -> None:
    window = FillWindow(since=ms(since_ms), until=ms(since_ms + length_ms))

    newer, older = split_in_half(window)

    assert older.since == window.since
    assert older.until == newer.since
    assert newer.until == window.until
    assert older.duration == timedelta(milliseconds=length_ms // 2)
    assert newer.duration >= older.duration


# --------------------------------------------------------------------------------------
# seconds_to_wait
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_after_ms", "expected"),
    [
        (0, 0),
        (1, 1),
        (999, 1),
        (1000, 1),
        (1001, 2),
        (2500, 3),
        (59_001, 60),
        (60_000, 60),
    ],
)
def test_the_venues_wait_is_rounded_up_to_whole_seconds(retry_after_ms: int, expected: int) -> None:
    """Up, never down: a wait a millisecond short is a request the venue refuses again."""
    assert seconds_to_wait(retry_after_ms, attempt=1) == expected


@pytest.mark.parametrize("retry_after_ms", [60_001, 61_000, 3_600_000])
def test_a_wait_beyond_the_cap_is_not_a_wait(retry_after_ms: int) -> None:
    """`None` means stop this account for this run rather than hold the coordinator.

    Sixty seconds is sixty *after* rounding up, so 60 001 ms -- sixty-one seconds -- is over.
    """
    assert seconds_to_wait(retry_after_ms, attempt=1) is None


@pytest.mark.parametrize(("attempt", "expected"), [(0, 1), (1, 2), (2, 4), (3, 8), (5, 32)])
def test_without_a_retry_after_the_wait_doubles_with_the_attempt(
    attempt: int, expected: int
) -> None:
    """`2 ** attempt` seconds; the service counts attempts from 1, so it waits 2, 4, 8."""
    assert seconds_to_wait(None, attempt=attempt) == expected


def test_a_doubling_wait_past_the_cap_is_not_a_wait() -> None:
    """Sixty-four seconds is over sixty."""
    assert seconds_to_wait(None, attempt=6) is None


def test_a_negative_attempt_is_a_caller_mistake() -> None:
    with pytest.raises(ValueError, match="attempt"):
        seconds_to_wait(None, attempt=-1)


def test_a_negative_venue_wait_is_a_caller_mistake() -> None:
    with pytest.raises(ValueError, match="retry_after_ms"):
        seconds_to_wait(-1, attempt=1)


@pytest.mark.parametrize(
    "limit",
    [timedelta(0), -timedelta(days=1), timedelta(microseconds=500)],
    ids=["zero", "negative", "under a millisecond"],
)
def test_a_window_limit_that_is_not_a_positive_whole_millisecond_is_refused(
    limit: timedelta,
) -> None:
    """A zero limit would loop forever; a sub-millisecond one would leave the grid."""
    with pytest.raises(ValueError, match="max_window"):
        split_newest_first(NOW - timedelta(days=1), NOW, max_window=limit)
    with pytest.raises(ValueError, match="max_window"):
        normalise_pending([], floor=NOW, max_window=limit)


def test_the_venues_wait_wins_over_the_doubling() -> None:
    """A venue that says one second is waited one second, not `2 ** attempt`."""
    assert seconds_to_wait(1000, attempt=5) == 1
    assert seconds_to_wait(10_000, attempt=0) == 10
