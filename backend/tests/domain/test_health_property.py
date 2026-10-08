"""Spec 030 (#23), criterion 8: the health rules hold for any input, not only the examples.

Each property restates a rule independently of the code -- as a comparison of whole
microseconds -- and asks Hypothesis for inputs that make the two disagree. Short on purpose,
for the reason `test_backups_rotation_property` gives: a broken property shrinks inside this
module's traceback, not a long one's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.health import (
    PriceHealthState,
    SchedulerState,
    price_state,
    scheduler_state,
)

NOW: Final = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
MICROSECONDS_PER_MINUTE: Final = 60_000_000

#: Ages around the boundaries that matter, in microseconds before `NOW`. Negative is a
#: reference in the future.
AGES: Final = st.integers(
    min_value=-MICROSECONDS_PER_MINUTE, max_value=400 * MICROSECONDS_PER_MINUTE
)
INTERVAL_MINUTES: Final = st.integers(min_value=1, max_value=120)


def ago(age: int | None) -> datetime | None:
    """The instant `age` microseconds before `NOW`, or `None` for none recorded."""
    return None if age is None else NOW - timedelta(microseconds=age)


@settings(report_multiple_bugs=False)
@given(
    interval_minutes=INTERVAL_MINUTES,
    started_age=AGES,
    tick_started_age=st.none() | AGES,
    finished_age=st.none() | AGES,
    running=st.booleans(),
)
def test_a_running_timer_is_late_exactly_when_its_reference_is_over_two_intervals_old(
    interval_minutes: int,
    started_age: int,
    tick_started_age: int | None,
    finished_age: int | None,
    running: bool,
) -> None:
    """In whole microseconds: late iff `age > 2 * interval`, of a reference with two cases.

    In ages, a later instant is a smaller age. A tick is in flight when one started and none
    finished after it -- no finish, or a finish older than the start -- and is judged from its
    start (R13, S4); otherwise the reference is the last finish, or the loop's start.
    """
    in_flight = tick_started_age is not None and (
        finished_age is None or tick_started_age < finished_age
    )
    if in_flight:
        assert tick_started_age is not None
        reference_age = tick_started_age
    else:
        reference_age = finished_age if finished_age is not None else started_age
    expected = (
        SchedulerState.STOPPED
        if not running
        else SchedulerState.LATE
        if reference_age > 2 * interval_minutes * MICROSECONDS_PER_MINUTE
        else SchedulerState.OK
    )

    state = scheduler_state(
        running=running,
        interval=timedelta(minutes=interval_minutes),
        started_at=NOW - timedelta(microseconds=started_age),
        last_tick_started_at=ago(tick_started_age),
        last_tick_finished_at=ago(finished_age),
        now=NOW,
    )

    assert state is expected


@settings(report_multiple_bugs=False)
@given(age=st.none() | AGES, limit_minutes=INTERVAL_MINUTES)
def test_prices_are_stale_exactly_when_older_than_the_limit(
    age: int | None, limit_minutes: int
) -> None:
    expected = (
        PriceHealthState.NEVER
        if age is None
        else PriceHealthState.STALE
        if age > limit_minutes * MICROSECONDS_PER_MINUTE
        else PriceHealthState.FRESH
    )
    latest = None if age is None else NOW - timedelta(microseconds=age)

    assert price_state(latest, now=NOW, stale_after=timedelta(minutes=limit_minutes)) is expected
