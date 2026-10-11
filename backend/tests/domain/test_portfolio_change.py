"""`domain.portfolio_change`: spec 041's rules R3 to R6. Pure, so no fixtures.

Every expected figure is a literal, never recomputed with the arithmetic under test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final

import pytest

from portfolio.domain.portfolio_change import (
    PERIODS,
    Change,
    ChangePeriod,
    Unavailable,
    WalletThen,
    change_over,
    quantity_at,
    value_at,
)
from portfolio.domain.portfolio_history import DailyReading

AT: Final = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
"""Noon and a half on 2026-10-09: the day before it is 2026-10-08."""

REBUILT: Final = (
    DailyReading(date(2026, 10, 7), Decimal("0.1")),
    DailyReading(date(2026, 10, 8), Decimal("0.3")),
    DailyReading(date(2026, 10, 9), Decimal("0.7")),
)


def test_the_periods_are_a_day_and_a_week_in_that_order() -> None:
    assert [member.value for member in ChangePeriod] == ["24h", "7d"]
    assert list(PERIODS.items()) == [
        (ChangePeriod.DAY, timedelta(hours=24)),
        (ChangePeriod.WEEK, timedelta(days=7)),
    ]
    assert [member.value for member in Unavailable] == [
        "value_unknown_now",
        "no_reading_then",
        "no_price_then",
    ]


# --------------------------------------------------------------------------------------
# A wallet's quantity at an instant (R3)
# --------------------------------------------------------------------------------------


def test_a_snapshot_wins_over_a_rebuilt_day() -> None:
    assert quantity_at(AT, Decimal("0.5"), REBUILT) == Decimal("0.5")


def test_without_a_snapshot_it_is_the_rebuilt_balance_at_the_end_of_the_day_before() -> None:
    """Never the instant's own day: a rebuilt day ends after the instant."""
    assert quantity_at(AT, None, REBUILT) == Decimal("0.3")


def test_an_instant_in_another_zone_is_placed_on_its_utc_day() -> None:
    """01:00 at UTC+3 on the 10th is 22:00 UTC on the 9th: the day before is the 8th."""
    east = datetime(2026, 10, 10, 1, 0, tzinfo=timezone(timedelta(hours=3)))

    assert quantity_at(east, None, REBUILT) == Decimal("0.3")


def test_before_the_first_rebuilt_day_the_wallet_held_nothing() -> None:
    """A rebuilt history is proven to start from zero (spec 038, R4)."""
    assert quantity_at(datetime(2026, 10, 7, 9, tzinfo=UTC), None, REBUILT) == Decimal(0)


def test_with_neither_the_quantity_is_unknown() -> None:
    assert quantity_at(AT, None, ()) is None


# --------------------------------------------------------------------------------------
# The value at an instant (R4)
# --------------------------------------------------------------------------------------


def test_the_value_is_the_exact_sum_of_every_holding() -> None:
    wallets = [
        WalletThen("BTC", Decimal("0.4")),
        WalletThen("BTC", Decimal("0.1")),
        WalletThen("KAS", Decimal(1000)),
    ]
    prices = {"BTC": Decimal(60000), "KAS": Decimal("0.05")}

    assert value_at(wallets, prices) == Decimal(30050)


def test_an_unknown_wallet_makes_the_value_unknown_whatever_the_prices() -> None:
    wallets = [WalletThen("BTC", Decimal(1)), WalletThen("KAS", None)]

    assert value_at(wallets, {}) is Unavailable.NO_READING_THEN


def test_a_holding_with_no_price_makes_the_value_unknown() -> None:
    wallets = [WalletThen("BTC", Decimal(1)), WalletThen("KAS", Decimal(5))]

    assert value_at(wallets, {"BTC": Decimal(60000)}) is Unavailable.NO_PRICE_THEN


def test_a_wallet_holding_nothing_needs_no_price() -> None:
    wallets = [WalletThen("BTC", Decimal(1)), WalletThen("KAS", Decimal(0))]

    assert value_at(wallets, {"BTC": Decimal(60000)}) == Decimal(60000)


def test_no_wallets_are_worth_zero() -> None:
    assert value_at([], {}) == Decimal(0)


# --------------------------------------------------------------------------------------
# The change (R5, R6)
# --------------------------------------------------------------------------------------

SINCE: Final = AT - timedelta(hours=24)


def test_a_rise_is_exact_with_its_percentage_of_the_value_then() -> None:
    change = change_over(ChangePeriod.DAY, since=SINCE, now=Decimal("1100.50"), then=Decimal(1000))

    assert change == Change(
        period=ChangePeriod.DAY,
        since=SINCE,
        value_then=Decimal(1000),
        change=Decimal("100.50"),
        change_pct=Decimal("10.0500"),
        unavailable=None,
    )


def test_a_fall_is_negative_and_its_percentage_is_rounded_half_even_once() -> None:
    change = change_over(ChangePeriod.WEEK, since=SINCE, now=Decimal(2), then=Decimal(3))

    assert change.change == Decimal(-1)
    assert change.change_pct == Decimal("-33.3333")


def test_nothing_held_then_has_a_change_and_no_percentage() -> None:
    change = change_over(ChangePeriod.DAY, since=SINCE, now=Decimal(500), then=Decimal(0))

    assert change.change == Decimal(500)
    assert change.change_pct is None
    assert change.unavailable is None


@pytest.mark.parametrize(
    ("now", "then", "reason", "value_then"),
    [
        pytest.param(
            None, Decimal(1000), Unavailable.VALUE_UNKNOWN_NOW, Decimal(1000), id="unknown now"
        ),
        pytest.param(
            Decimal(1000), Unavailable.NO_PRICE_THEN, Unavailable.NO_PRICE_THEN, None, id="then"
        ),
        pytest.param(
            None, Unavailable.NO_READING_THEN, Unavailable.NO_READING_THEN, None, id="both"
        ),
    ],
)
def test_an_unknown_value_is_an_unavailable_change_never_a_zero(
    now: Decimal | None,
    then: Decimal | Unavailable,
    reason: Unavailable,
    value_then: Decimal | None,
) -> None:
    change = change_over(ChangePeriod.DAY, since=SINCE, now=now, then=then)

    assert change.change is None
    assert change.change_pct is None
    assert change.unavailable is reason
    assert change.value_then == value_then
