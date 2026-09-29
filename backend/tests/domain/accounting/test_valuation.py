"""Criteria 4, 6 and 7 of #19, in the pure valuation: spec 021's table, row by row.

`value_position(position, price, price_reason)` and `value_portfolio(values)` are the only
place a position meets a price. Every figure the endpoint serves -- market value, unrealized
P&L, the percentage, and the portfolio totals -- is computed here, so every rule of the
spec's valuation table has a test here, named after it:

* `market_value` -- `quantize(Q x price, 18)` over the **total** quantity; `None` when the
  price is missing and `Q > 0`; zero when `Q == 0`, whatever the price.
* `unrealized_pnl` -- `quantize(Qk x price, 18) - C` over the **known** part; `None` when the
  price is missing and `Qk > 0`; zero when `Qk == 0`.
* `unrealized_return_pct` -- `divide(pnl x 100, C, 4)`; `None` when the P&L is `None` or
  `C <= 0`, and (R4) when the four-place quotient does not fit `MONEY_PRECISION`.
* `market_value_unavailable_reason` -- the price reason when `market_value` is `None`.

The totals cover only the **fully comparable** positions -- priced (or holding nothing) and
without `UNKNOWN_BASIS` -- and name the rest with a reason; realized P&L is summed over
every position.

Every expected figure is worked out by hand in the test's docstring or beside the literal,
never by calling the function under test or `money`: a test that computed its expectation
with the code it checks would agree with any bug in it.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from typing import Final

import pytest

from portfolio.domain.accounting import Position, PositionFlag
from portfolio.domain.accounting.valuation import (
    ExclusionReason,
    PortfolioTotals,
    PositionValue,
    value_portfolio,
    value_position,
)

NEVER_FETCHED: Final = "never_fetched"
UNSUPPORTED_PAIR: Final = "unsupported_pair"

ZERO: Final = Decimal(0)


def held(
    asset: str = "BTC",
    *,
    quantity: str = "1.5",
    unknown: str = "0",
    cost_basis: str = "52500",
    average_cost: str | None = "35000",
    realized: str = "0",
    unmatched: str = "0",
    flags: frozenset[PositionFlag] = frozenset(),
) -> Position:
    """A `Position` as `replay` returns one: every amount a `Decimal`."""
    return Position(
        asset=asset,
        quantity=Decimal(quantity),
        unknown_basis_quantity=Decimal(unknown),
        cost_basis=Decimal(cost_basis),
        average_cost=None if average_cost is None else Decimal(average_cost),
        realized_pnl=Decimal(realized),
        unmatched_proceeds=Decimal(unmatched),
        flags=flags,
    )


def priced(position: Position, price: str) -> PositionValue:
    return value_position(position, Decimal(price), None)


def unpriced(position: Position, reason: str = NEVER_FETCHED) -> PositionValue:
    return value_position(position, None, reason)


def assert_exact(found: Decimal | None, expected: str | None) -> None:
    """Equal **and** a `Decimal`: `0 == Decimal(0)` would let an `int` zero through."""
    if expected is None:
        assert found is None
        return
    assert isinstance(found, Decimal), f"{found!r} is not a Decimal"
    assert found == Decimal(expected), f"{found} != {expected}"


def excluded_of(totals: PortfolioTotals) -> list[tuple[str, str]]:
    return [(entry.asset, str(entry.reason)) for entry in totals.excluded]


# --------------------------------------------------------------------------------------
# The spec's own example, by hand
# --------------------------------------------------------------------------------------


def test_the_specs_example_position_by_hand() -> None:
    """1.5 BTC at an average of 35000 (C = 52500), priced at 60000.

    market value 1.5 x 60000 = 90000; P&L 90000 - 52500 = 37500; return
    37500 x 100 / 52500 = 71.428571... -> 71.4286 at four places.
    """
    value = priced(held(), "60000")

    assert_exact(value.market_value, "90000")
    assert_exact(value.unrealized_pnl, "37500")
    assert_exact(value.unrealized_return_pct, "71.4286")
    assert value.market_value_unavailable_reason is None


def test_a_loss_is_a_negative_pnl_and_a_negative_percentage() -> None:
    """2 ETH for 5000 (C = 5000), priced at 2000: worth 4000, P&L -1000, return -20%."""
    value = priced(held("ETH", quantity="2", cost_basis="5000", average_cost="2500"), "2000")

    assert_exact(value.market_value, "4000")
    assert_exact(value.unrealized_pnl, "-1000")
    assert_exact(value.unrealized_return_pct, "-20")


# --------------------------------------------------------------------------------------
# `market_value`: the total quantity, rounded once to eighteen places
# --------------------------------------------------------------------------------------


def test_market_value_counts_every_unit_including_the_unknown_basis_ones() -> None:
    """Criterion 6: 3 KAS held, 1 of them of unknown cost, at 0.10 -- the holding is worth 0.30.

    The P&L counts only the known 2: 2 x 0.10 - 0.16 = 0.04, a return of 25%.
    """
    kas = held(
        "KAS",
        quantity="3",
        unknown="1",
        cost_basis="0.16",
        average_cost="0.08",
        flags=frozenset({PositionFlag.UNKNOWN_BASIS}),
    )

    value = priced(kas, "0.10")

    assert_exact(value.market_value, "0.30")
    assert_exact(value.unrealized_pnl, "0.04")
    assert_exact(value.unrealized_return_pct, "25")


def test_market_value_is_zero_when_nothing_is_held_whatever_the_price() -> None:
    """Q == 0: zero with a price, and zero -- not `None`, no reason -- without one."""
    emptied = held(quantity="0", cost_basis="0", average_cost=None, realized="12.5")

    with_price = priced(emptied, "60000")
    without_price = unpriced(emptied, NEVER_FETCHED)

    for value in (with_price, without_price):
        assert_exact(value.market_value, "0")
        assert_exact(value.unrealized_pnl, "0")
        assert value.unrealized_return_pct is None, "C == 0 has no meaningful percentage"
        assert value.market_value_unavailable_reason is None


@pytest.mark.parametrize("reason", [NEVER_FETCHED, UNSUPPORTED_PAIR])
def test_a_missing_price_is_a_null_market_value_with_its_reason(reason: str) -> None:
    """Criterion 7: `None`, never a zero, and the reason travels with it."""
    value = unpriced(held(), reason)

    assert value.market_value is None
    assert value.unrealized_pnl is None
    assert value.unrealized_return_pct is None
    assert str(value.market_value_unavailable_reason) == reason


def test_market_value_rounds_once_half_to_even_at_eighteen_places() -> None:
    """1E-18 x 0.5 = 5E-19, a tie: half-even gives 0. 3E-18 x 0.5 = 1.5E-18 gives 2E-18.

    A half-up rounding would give 1E-18 for the first, and a truncation 1E-18 for the second.
    """
    tie_down = priced(
        held(quantity="0.000000000000000001", cost_basis="0", average_cost="0"), "0.5"
    )
    tie_up = priced(held(quantity="0.000000000000000003", cost_basis="0", average_cost="0"), "0.5")

    assert_exact(tie_down.market_value, "0")
    assert_exact(tie_up.market_value, "0.000000000000000002")
    assert tie_up.market_value is not None
    assert tie_up.market_value.as_tuple().exponent == -18


def test_market_value_is_exact_far_past_a_doubles_precision() -> None:
    """20 integer digits times a 12-place price: the product needs 38 digits, and gets them.

    12345678901234567890.123456789012345678 x 2 = 24691357802469135780.246913578024691356.
    """
    value = priced(
        held(
            quantity="12345678901234567890.123456789012345678",
            cost_basis="0",
            average_cost="0",
        ),
        "2",
    )

    assert_exact(value.market_value, "24691357802469135780.246913578024691356")


# --------------------------------------------------------------------------------------
# `unrealized_pnl`: the known part only
# --------------------------------------------------------------------------------------


def test_pnl_is_zero_when_no_known_quantity_is_held_even_with_a_price() -> None:
    """Qk == 0 and Qu > 0: 5 DOGE of unknown cost at 0.2 is worth 1, with no P&L to report."""
    doge = held(
        "DOGE",
        quantity="5",
        unknown="5",
        cost_basis="0",
        average_cost=None,
        flags=frozenset({PositionFlag.UNKNOWN_BASIS}),
    )

    value = priced(doge, "0.2")

    assert_exact(value.market_value, "1")
    assert_exact(value.unrealized_pnl, "0")
    assert value.unrealized_return_pct is None


def test_pnl_is_zero_without_a_price_when_no_known_quantity_is_held() -> None:
    """Qk == 0, Qu > 0, no price: the value is unknown, the P&L of nothing known is zero."""
    doge = held(
        "DOGE",
        quantity="5",
        unknown="5",
        cost_basis="0",
        average_cost=None,
        flags=frozenset({PositionFlag.UNKNOWN_BASIS}),
    )

    value = unpriced(doge, UNSUPPORTED_PAIR)

    assert value.market_value is None
    assert str(value.market_value_unavailable_reason) == UNSUPPORTED_PAIR
    assert_exact(value.unrealized_pnl, "0")
    assert value.unrealized_return_pct is None


def test_pnl_rounds_the_known_value_before_subtracting_the_basis() -> None:
    """`quantize(Qk x price, 18) - C`: 1E-18 x 0.5 rounds to 0, so the P&L is 0 - 0.

    Subtracting before rounding would leave 5E-19, which no 18-place column can hold.
    """
    value = priced(held(quantity="0.000000000000000001", cost_basis="0", average_cost="0"), "0.5")

    assert_exact(value.unrealized_pnl, "0")


# --------------------------------------------------------------------------------------
# `unrealized_return_pct`: four places, half to even, and only over a positive basis
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        # P&L 0.000001 over C = 2: 0.0001 / 2 = 0.00005, a tie, and 0 is even.
        ("2.000001", "0"),
        # P&L 0.000003 over C = 2: 0.0003 / 2 = 0.00015, a tie, and 2 is even.
        ("2.000003", "0.0002"),
        # P&L 0.000005 over C = 2: 0.00025, a tie, and 2 is even again -- half-up says 0.0003.
        ("2.000005", "0.0002"),
    ],
    ids=["tie to zero", "tie up to even", "tie down to even"],
)
def test_the_percentage_rounds_once_half_to_even_at_four_places(price: str, expected: str) -> None:
    value = priced(held(quantity="1", cost_basis="2", average_cost="2"), price)

    assert_exact(value.unrealized_return_pct, expected)
    assert value.unrealized_return_pct is not None
    assert value.unrealized_return_pct.as_tuple().exponent == -4


@pytest.mark.parametrize(
    "cost_basis",
    ["0", "-3"],
    ids=["a zero basis (units bought at no cost)", "a negative basis (spec 019, R7)"],
)
def test_no_percentage_over_a_basis_that_is_not_positive(cost_basis: str) -> None:
    """C <= 0 with known units held: the P&L is reported, the percentage is not."""
    value = priced(held(quantity="2", cost_basis=cost_basis, average_cost=None), "10")

    # 2 x 10 = 20, minus 0 or minus -3.
    assert_exact(value.unrealized_pnl, {"0": "20", "-3": "23"}[cost_basis])
    assert value.unrealized_return_pct is None


def test_no_percentage_when_the_pnl_is_unknown() -> None:
    value = unpriced(held(), NEVER_FETCHED)

    assert value.unrealized_pnl is None
    assert value.unrealized_return_pct is None


# --------------------------------------------------------------------------------------
# The reason field
# --------------------------------------------------------------------------------------


def test_the_reason_is_only_carried_when_the_market_value_is_missing() -> None:
    """Nothing held and no price: the value is a zero, so there is no reason to report."""
    emptied = value_position(
        held(quantity="0", cost_basis="0", average_cost=None), None, UNSUPPORTED_PAIR
    )

    assert_exact(emptied.market_value, "0")
    assert emptied.market_value_unavailable_reason is None


@pytest.mark.parametrize(
    ("price", "reason"),
    [(Decimal(60000), NEVER_FETCHED), (None, None)],
    ids=["a price and a reason", "neither"],
)
def test_a_price_and_its_reason_are_exclusive(price: Decimal | None, reason: str | None) -> None:
    """Either a price or the reason there is none: both, or neither, is a caller's defect."""
    with pytest.raises(ValueError, match="either a price or the reason"):
        value_position(held(), price, reason)


# --------------------------------------------------------------------------------------
# The portfolio totals
# --------------------------------------------------------------------------------------


def four_positions() -> list[PositionValue]:
    """BTC and ETH comparable, KAS of partly unknown basis, DOGE unpriced.

    BTC: C 52500, worth 90000, P&L 37500, realized 7500.
    ETH: C 5000, worth 4000, P&L -1000, realized -200.
    KAS: C 0.16, worth 0.30, P&L 0.04, realized 1.25 -- excluded, `unknown_basis`.
    DOGE: C 10, no price, realized 3 -- excluded, `unpriced`.
    """
    return [
        priced(held(realized="7500"), "60000"),
        priced(
            held("ETH", quantity="2", cost_basis="5000", average_cost="2500", realized="-200"),
            "2000",
        ),
        priced(
            held(
                "KAS",
                quantity="3",
                unknown="1",
                cost_basis="0.16",
                average_cost="0.08",
                realized="1.25",
                flags=frozenset({PositionFlag.UNKNOWN_BASIS}),
            ),
            "0.10",
        ),
        unpriced(
            held("DOGE", quantity="100", cost_basis="10", average_cost="0.1", realized="3"),
            NEVER_FETCHED,
        ),
    ]


def test_the_totals_cover_only_the_fully_comparable_positions() -> None:
    """BTC + ETH: invested 57500, worth 94000, P&L 36500; 36500 x 100 / 57500 = 63.4782608...

    -> 63.4783. KAS and DOGE are left out of all four, with their reasons.
    """
    totals = value_portfolio(four_positions())

    assert_exact(totals.total_invested, "57500")
    assert_exact(totals.market_value, "94000")
    assert_exact(totals.unrealized_pnl, "36500")
    assert_exact(totals.unrealized_return_pct, "63.4783")
    assert excluded_of(totals) == [("KAS", "unknown_basis"), ("DOGE", "unpriced")]


def test_realized_pnl_is_summed_over_every_position() -> None:
    """7500 - 200 + 1.25 + 3 = 7304.25: excluded positions' realized figures are known."""
    totals = value_portfolio(four_positions())

    assert_exact(totals.realized_pnl, "7304.25")


def test_a_position_holding_nothing_is_comparable_without_a_price() -> None:
    """Sold out, never priced: it contributes zeros and its realized P&L, and is not excluded."""
    emptied = unpriced(
        held("ADA", quantity="0", cost_basis="0", average_cost=None, realized="29.9"),
        UNSUPPORTED_PAIR,
    )
    btc = priced(held(), "60000")

    totals = value_portfolio([emptied, btc])

    assert excluded_of(totals) == []
    assert_exact(totals.total_invested, "52500")
    assert_exact(totals.market_value, "90000")
    assert_exact(totals.realized_pnl, "29.9")


def test_history_incomplete_alone_does_not_exclude_a_position() -> None:
    """Only `UNKNOWN_BASIS` and a missing price exclude; the other flags qualify the figure."""
    flagged = priced(
        held(flags=frozenset({PositionFlag.HISTORY_INCOMPLETE, PositionFlag.UNATTRIBUTED_FEE})),
        "60000",
    )

    totals = value_portfolio([flagged])

    assert excluded_of(totals) == []
    assert_exact(totals.market_value, "90000")


def test_an_unknown_basis_position_is_excluded_even_when_priced() -> None:
    kas = four_positions()[2]

    totals = value_portfolio([kas])

    assert excluded_of(totals) == [("KAS", "unknown_basis")]
    assert_exact(totals.total_invested, "0")
    assert_exact(totals.market_value, "0")
    assert_exact(totals.unrealized_pnl, "0")
    assert totals.unrealized_return_pct is None


def test_no_positions_are_zero_totals_and_no_percentage() -> None:
    """The endpoint's "no snapshot" state: zeros, not `None`, and a `Decimal` each."""
    totals = value_portfolio([])

    for figure in (
        totals.total_invested,
        totals.market_value,
        totals.unrealized_pnl,
        totals.realized_pnl,
    ):
        assert_exact(figure, "0")
    assert totals.unrealized_return_pct is None
    assert list(totals.excluded) == []


def test_no_total_percentage_over_a_basis_that_is_not_positive() -> None:
    """Comparable positions whose basis sums to zero or less have no total percentage."""
    free = priced(held(quantity="2", cost_basis="0", average_cost="0"), "10")

    totals = value_portfolio([free])

    assert_exact(totals.unrealized_pnl, "20")
    assert totals.unrealized_return_pct is None


def test_an_asset_both_of_unknown_basis_and_unpriced_is_excluded_once_as_unknown_basis() -> None:
    """Spec 021, R4(b): one entry per excluded position, `unknown_basis` checked first."""
    kas = unpriced(
        held(
            "KAS",
            quantity="3",
            unknown="1",
            cost_basis="0.16",
            average_cost="0.08",
            flags=frozenset({PositionFlag.UNKNOWN_BASIS}),
        ),
        NEVER_FETCHED,
    )

    totals = value_portfolio([kas])

    assert excluded_of(totals) == [("KAS", "unknown_basis")]
    assert [entry.reason for entry in totals.excluded] == [ExclusionReason.UNKNOWN_BASIS]


def test_the_exclusion_reasons_are_the_specs_two() -> None:
    assert {str(reason) for reason in ExclusionReason} == {"unknown_basis", "unpriced"}


def test_a_percentage_that_does_not_fit_is_none_rather_than_an_error() -> None:
    """Spec 021, R4(a): C = 1E-18 and a P&L near 1E19 make a quotient of about 1E39.

    At four places that needs 43 significant digits, past `MONEY_PRECISION` (38): no column
    and no wire form holds it, so the percentage is absent -- never a 500 on the endpoint.
    The P&L itself is still reported: 1 x 1E19 - 1E-18.
    """
    tiny_basis = held(
        quantity="1", cost_basis="0.000000000000000001", average_cost="0.000000000000000001"
    )

    value = priced(tiny_basis, "10000000000000000000")
    totals = value_portfolio([value])

    assert_exact(value.unrealized_pnl, "9999999999999999999.999999999999999999")
    assert value.unrealized_return_pct is None
    assert_exact(totals.unrealized_pnl, "9999999999999999999.999999999999999999")
    assert totals.unrealized_return_pct is None


def test_the_largest_percentage_that_fits_is_still_reported() -> None:
    """The control for the test above: a large but representable quotient comes back.

    C = 1, P&L = 1E19 - 1 over one unit priced at 1E19: 999999999999999999900% exactly,
    21 integer digits and 4 places, 25 significant digits in all.
    """
    value = priced(held(quantity="1", cost_basis="1", average_cost="1"), "10000000000000000000")

    assert_exact(value.unrealized_return_pct, "999999999999999999900")


VALUE_OUT_OF_RANGE: Final = "value_out_of_range"


def test_a_value_past_the_range_is_none_with_its_reason_rather_than_an_error() -> None:
    """Spec 021, R6: 1E19 units at 10 is 1E20, which needs 21 digits before the point.

    No `NumericText(18)` column and no 18-place wire amount holds it, so the value and the
    P&L are absent, the reason says why, and the totals leave the position out as `unpriced`
    -- where an unguarded `quantize` would raise, and the endpoint answer 500.
    """
    huge = held(quantity="10000000000000000000", cost_basis="1", average_cost="0", realized="3")

    value = priced(huge, "10")
    totals = value_portfolio([value])

    assert value.market_value is None
    assert value.unrealized_pnl is None
    assert value.unrealized_return_pct is None
    assert str(value.market_value_unavailable_reason) == VALUE_OUT_OF_RANGE
    assert excluded_of(totals) == [("BTC", "unpriced")]
    assert_exact(totals.market_value, "0")
    assert_exact(totals.realized_pnl, "3"), "realized is still summed"


def test_the_largest_value_that_fits_is_still_reported() -> None:
    """The control: 99999999999999999999.999999999999999999 x 1 has 20 integer digits."""
    at_the_edge = held(
        quantity="99999999999999999999.999999999999999999", cost_basis="1", average_cost="0"
    )

    value = priced(at_the_edge, "1")

    assert_exact(value.market_value, "99999999999999999999.999999999999999999")
    assert value.market_value_unavailable_reason is None
    assert_exact(value.unrealized_pnl, "99999999999999999998.999999999999999999")


def test_a_value_that_only_rounding_takes_past_the_range_is_out_of_range() -> None:
    """Below 1E20 before rounding, 1E20 after it: the rounded value is what must fit.

    99999999999900000000.0000999999999999 x 1.000000000001 = 1E20 - 1E-28 exactly (checked
    with `fractions.Fraction`), which rounds at 18 places to 1E20 -- 21 integer digits. A
    guard that compared the unrounded product with 1E20 would let it through to `quantize`.
    """
    rounds_up = held(
        quantity="99999999999900000000.000099999999999900", cost_basis="1", average_cost="0"
    )

    value = priced(rounds_up, "1.000000000001")

    assert value.market_value is None
    assert str(value.market_value_unavailable_reason) == VALUE_OUT_OF_RANGE


# --------------------------------------------------------------------------------------
# Exactness: the ambient decimal context plays no part
# --------------------------------------------------------------------------------------


def hostile_inputs() -> list[PositionValue]:
    """Values whose every figure needs more than six significant digits."""
    return [
        priced(
            held(
                quantity="12345678901234567890.123456789012345678",
                cost_basis="11111111111111111111.111111111111111111",
                average_cost="0.9",
                realized="-7777777777.777777777777777777",
            ),
            "1.000000000001",
        ),
        priced(
            held(
                "ETH",
                quantity="0.333333333333333333",
                cost_basis="777.777777777777777777",
                average_cost="2333.333333333333333336",
                realized="0.000000000000000001",
            ),
            "3456.789012345678",
        ),
    ]


def test_the_valuation_ignores_a_hostile_ambient_context() -> None:
    """`prec=6, ROUND_UP`, then the same with `Inexact` and `Rounded` trapped.

    The first changes the answer of anything computed in the ambient context; the second
    turns any such computation into an exception even when its answer happened to survive.
    """
    default = hostile_inputs()
    default_totals = value_portfolio(default)

    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_UP
        narrowed = hostile_inputs()
        narrowed_totals = value_portfolio(narrowed)
    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_UP
        context.traps[decimal.Inexact] = True
        context.traps[decimal.Rounded] = True
        trapped = hostile_inputs()
        trapped_totals = value_portfolio(trapped)

    assert narrowed == default
    assert trapped == default
    assert narrowed_totals == default_totals
    assert trapped_totals == default_totals


def test_the_hostile_inputs_are_worked_by_hand() -> None:
    """The first input's figures, so the context test above compares correct answers.

    Q x price = 12345678901234567890.123456789012345678 x 1.000000000001
              = 12345678901234567890.123456789012345678
              + 12345678.901234567890123456789012345678   (Q x 1E-12)
              = 12345678901246913569.024691356902469134789012345678
    which rounds at 18 places to ...469135 (the 19th digit is 7). The P&L is that minus
    C = 11111111111111111111.111111111111111111, which is
    1234567790135802457.913580245791358024 -- both checked with `fractions.Fraction`.
    """
    value = hostile_inputs()[0]

    assert_exact(value.market_value, "12345678901246913569.024691356902469135")
    # The known part is all of it, so the P&L is the value minus C.
    assert_exact(value.unrealized_pnl, "1234567790135802457.913580245791358024")
