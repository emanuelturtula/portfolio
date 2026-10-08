"""`domain.portfolio`: the dashboard's summary and the value of one holding. Pure, so no
fixtures.

Every expected figure is written out as a literal, never recomputed with the arithmetic under
test, so a test cannot agree with a bug by repeating it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

import pytest

from portfolio.domain.portfolio import (
    MAX_READING_AGE,
    RETURN_PCT_SCALE,
    VALUE_SCALE,
    HoldingSummary,
    PricedQuantity,
    summarize,
    value_of,
)

# --------------------------------------------------------------------------------------
# The scales and the age limit
# --------------------------------------------------------------------------------------


def test_the_scales_and_the_age_limit() -> None:
    """Eighteen places for a value, four for a percentage, a day for a reading."""
    assert VALUE_SCALE == 18
    assert RETURN_PCT_SCALE == 4
    assert timedelta(hours=24) == MAX_READING_AGE


# --------------------------------------------------------------------------------------
# value_of
# --------------------------------------------------------------------------------------


def test_a_value_is_price_times_quantity_at_eighteen_places() -> None:
    value = value_of(Decimal("0.1"), Decimal(60000))

    assert value == Decimal(6000)
    assert str(value) == "6000.000000000000000000"


def test_a_value_is_rounded_once_half_to_even() -> None:
    """3 x 0.0000000000000000005 is 1.5E-18, which rounds to 2E-18 at eighteen places."""
    assert value_of(Decimal(3), Decimal("0.0000000000000000005")) == Decimal("2E-18")
    assert value_of(Decimal(5), Decimal("0.0000000000000000005")) == Decimal("2E-18")


def test_a_value_past_the_range_is_refused_rather_than_rounded() -> None:
    """10**20 units leaves no room for eighteen places inside the money precision."""
    with pytest.raises(InvalidOperation):
        value_of(Decimal(10) ** 20, Decimal(1))


# --------------------------------------------------------------------------------------
# summarize
# --------------------------------------------------------------------------------------


def test_values_and_shares() -> None:
    """0.1 BTC at 60000 is 6000; 50000 KAS at 0.08 is 4000; 10000 in all."""
    summary = summarize(
        [
            PricedQuantity("KAS", Decimal(50000), Decimal("0.08")),
            PricedQuantity("BTC", Decimal("0.1"), Decimal(60000)),
        ]
    )

    assert summary.total_value == Decimal(10000)
    assert summary.holdings == (
        HoldingSummary("BTC", Decimal("0.1"), Decimal(60000), Decimal(6000), Decimal("60.0000")),
        HoldingSummary("KAS", Decimal(50000), Decimal("0.08"), Decimal(4000), Decimal("40.0000")),
    )


def test_every_value_is_carried_at_eighteen_places() -> None:
    summary = summarize([PricedQuantity("BTC", Decimal("0.1"), Decimal(60000))])

    assert str(summary.total_value) == "6000.000000000000000000"
    assert str(summary.holdings[0].value) == "6000.000000000000000000"


def test_an_unpriced_holding_adds_nothing_and_is_listed_last() -> None:
    summary = summarize(
        [
            PricedQuantity("AAA", Decimal(5), None),
            PricedQuantity("KAS", Decimal(100), Decimal(1)),
        ]
    )

    assert summary.total_value == Decimal(100)
    assert [holding.asset for holding in summary.holdings] == ["KAS", "AAA"]
    assert summary.holdings[1] == HoldingSummary("AAA", Decimal(5), None, None, None)
    assert summary.holdings[0].share_pct == Decimal("100.0000")


def test_no_share_of_a_total_of_zero() -> None:
    summary = summarize([PricedQuantity("KAS", Decimal(10), Decimal(0))])

    assert summary.total_value == 0
    assert summary.holdings[0].value == 0
    assert summary.holdings[0].share_pct is None


def test_nothing_held_is_zero_at_eighteen_places() -> None:
    summary = summarize([])

    assert summary.total_value == 0
    assert str(summary.total_value) == "0E-18"
    assert summary.holdings == ()


def test_only_unpriced_holdings_total_zero_and_have_no_share() -> None:
    """Zero because nothing could be valued, which the caller names in `missing`."""
    summary = summarize([PricedQuantity("KAS", Decimal(10), None)])

    assert str(summary.total_value) == "0E-18"
    assert summary.holdings == (HoldingSummary("KAS", Decimal(10), None, None, None),)


def test_equal_values_are_ordered_by_asset() -> None:
    summary = summarize(
        [
            PricedQuantity("ZZZ", Decimal(1), Decimal(5)),
            PricedQuantity("AAA", Decimal(5), Decimal(1)),
            PricedQuantity("MMM", Decimal(1), None),
            PricedQuantity("BBB", Decimal(1), None),
        ]
    )

    assert [holding.asset for holding in summary.holdings] == ["AAA", "ZZZ", "BBB", "MMM"]


def test_a_share_is_rounded_to_four_places() -> None:
    """1 of 3 is 33.3333...%, and 2 of 3 is 66.6666...%: each rounded once."""
    summary = summarize(
        [
            PricedQuantity("BTC", Decimal(2), Decimal(1)),
            PricedQuantity("KAS", Decimal(1), Decimal(1)),
        ]
    )

    assert [holding.share_pct for holding in summary.holdings] == [
        Decimal("66.6667"),
        Decimal("33.3333"),
    ]
