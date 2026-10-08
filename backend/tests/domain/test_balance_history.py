"""`domain.balance_history.rebuild_daily`: spec 038's R3 and R4. Pure, so no fixtures.

Every expected balance is a literal in base units, never recomputed with the walk under test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Final

from portfolio.domain.balance_history import (
    DailyBalance,
    Effect,
    RebuildRefused,
    RebuiltHistory,
    rebuild_daily,
)

TODAY: Final = date(2026, 10, 8)


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def test_the_refusals_are_their_wire_forms() -> None:
    assert [member.value for member in RebuildRefused] == ["goes_negative", "does_not_reach_zero"]


def test_a_walk_back_reaches_zero_and_carries_quiet_days() -> None:
    """Received 100 on the 5th, 50 more on the 7th, spent 30 today: 120 now."""
    effects = [
        Effect(at(date(2026, 10, 5)), 100),
        Effect(at(date(2026, 10, 7)), 50),
        Effect(at(TODAY), -30),
    ]

    result = rebuild_daily(effects, balance=120, today=TODAY)

    assert result == RebuiltHistory(
        days=(
            DailyBalance(date(2026, 10, 5), 100),
            DailyBalance(date(2026, 10, 6), 100),
            DailyBalance(date(2026, 10, 7), 150),
            DailyBalance(TODAY, 120),
        ),
        refused=None,
    )


def test_effects_in_any_order_and_on_one_day_add_up() -> None:
    effects = [Effect(at(TODAY, 18), -40), Effect(at(TODAY, 9), 100), Effect(at(TODAY, 10), 5)]

    result = rebuild_daily(effects, balance=65, today=TODAY)

    assert result.days == (DailyBalance(TODAY, 65),)
    assert result.refused is None


def test_a_day_is_the_utc_date_of_the_block_time() -> None:
    """23:30 at UTC-3 on the 6th is 02:30 UTC on the 7th."""
    late = datetime(2026, 10, 6, 23, 30, tzinfo=timezone(timedelta(hours=-3)))

    result = rebuild_daily([Effect(late, 10)], balance=10, today=date(2026, 10, 7))

    assert result.days == (DailyBalance(date(2026, 10, 7), 10),)


def test_an_effect_dated_after_today_counts_on_today() -> None:
    """A clock behind the chain's: the transaction is not lost, it lands on today."""
    result = rebuild_daily([Effect(at(TODAY + timedelta(days=1)), 7)], balance=7, today=TODAY)

    assert result.days == (DailyBalance(TODAY, 7),)


def test_a_history_that_does_not_reach_zero_is_refused() -> None:
    """A missing first deposit: the walk ends at 40, not 0."""
    result = rebuild_daily([Effect(at(TODAY), 60)], balance=100, today=TODAY)

    assert result == RebuiltHistory(days=(), refused=RebuildRefused.DOES_NOT_REACH_ZERO)


def test_a_walk_below_zero_is_refused() -> None:
    """A missing deposit in the middle: yesterday would have been -50."""
    effects = [Effect(at(TODAY - timedelta(days=2)), 10), Effect(at(TODAY), 100)]

    result = rebuild_daily(effects, balance=50, today=TODAY)

    assert result == RebuiltHistory(days=(), refused=RebuildRefused.GOES_NEGATIVE)


def test_no_transaction_and_nothing_held_is_an_empty_history() -> None:
    assert rebuild_daily([], balance=0, today=TODAY) == RebuiltHistory(days=(), refused=None)


def test_no_transaction_but_a_balance_is_refused() -> None:
    assert rebuild_daily([], balance=1, today=TODAY) == RebuiltHistory(
        days=(), refused=RebuildRefused.DOES_NOT_REACH_ZERO
    )


def test_a_wallet_emptied_long_ago_keeps_its_zero_days() -> None:
    """Received and spent everything a week ago: every day since is a known zero."""
    first = TODAY - timedelta(days=7)
    effects = [Effect(at(first, 1), 500), Effect(at(first, 2), -500)]

    result = rebuild_daily(effects, balance=0, today=TODAY)

    assert len(result.days) == 8
    assert all(day.confirmed == 0 for day in result.days)
    assert result.days[0].day == first
