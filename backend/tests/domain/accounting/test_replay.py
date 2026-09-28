"""Spec 019 by example: every trade shape, every fee position, and the edges between them.

Each expected figure is worked out by hand in the test's docstring or comment, on numbers
chosen to be checkable in one's head. The property tests in `test_invariants.py` cover what
no example can -- arbitrary sequences -- and the oracle agreement there covers the digits no
hand computes; these exist so that a failure names the rule it broke.

Positions are asserted field by field through `expect`, which also checks the one invariant
every example should satisfy for free: the position's quantity is its known quantity plus
its unknown quantity, and neither is negative.
"""

from __future__ import annotations

import decimal
import re
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest

from portfolio.domain.accounting import (
    ENGINE_VERSION,
    METHOD,
    AccountingConfig,
    ConflictingEventError,
    NegativeInventory,
    UnattributedFee,
    replay,
)
from tests.domain.accounting.oracle import round_half_even
from tests.domain.accounting.support import (
    CONFIG,
    adjust,
    at,
    buy,
    flag_names,
    key,
    move,
    position,
    sell,
)

if TYPE_CHECKING:
    from portfolio.domain.accounting import AccountingResult
    from portfolio.domain.accounting.events import AccountingEvent


def run(*events: AccountingEvent) -> AccountingResult:
    return replay(list(events), CONFIG)


def expect(
    result: AccountingResult,
    asset: str,
    *,
    quantity: str,
    cost_basis: str,
    unknown: str = "0",
    average: str | None = "derive",
    realized: str = "0",
    unmatched: str = "0",
    flags: frozenset[str] = frozenset(),
) -> None:
    """Assert every field of `asset`'s position. `average="derive"` means `cost / known`."""
    found = position(result, asset)
    assert found.quantity == Decimal(quantity), ("quantity", found)
    assert found.unknown_basis_quantity == Decimal(unknown), ("unknown", found)
    assert found.cost_basis == Decimal(cost_basis), ("cost_basis", found)
    assert found.realized_pnl == Decimal(realized), ("realized", found)
    assert found.unmatched_proceeds == Decimal(unmatched), ("unmatched", found)
    assert flag_names(found) == set(flags), ("flags", found)
    known = Decimal(quantity) - Decimal(unknown)
    assert known >= 0
    if average == "derive":
        if known == 0:
            assert found.average_cost is None, ("average", found)
        else:
            assert found.average_cost is not None, ("average", found)
            exact = Fraction(Decimal(cost_basis)) / Fraction(known)
            assert Fraction(found.average_cost) == round_half_even(exact), ("average", found)
    elif average is None:
        assert found.average_cost is None, ("average", found)
    else:
        assert found.average_cost == Decimal(average), ("average", found)


def assert_no_position(result: AccountingResult, asset: str) -> None:
    assert asset not in [found.asset for found in result.positions], result.positions


UNKNOWN = frozenset({"UNKNOWN_BASIS"})
INCOMPLETE = frozenset({"HISTORY_INCOMPLETE"})
UNATTRIBUTED = frozenset({"UNATTRIBUTED_FEE"})


# --------------------------------------------------------------------------------------
# The result's identity
# --------------------------------------------------------------------------------------


def test_an_empty_history_is_an_empty_result() -> None:
    result = run()

    assert result.method == METHOD == "weighted_average"
    assert result.engine_version == ENGINE_VERSION == 1
    assert result.positions == ()
    assert result.warnings == ()
    assert result.lots == ()
    assert result.unallocated_costs == 0
    assert result.event_count == 0
    assert re.fullmatch(r"[0-9a-f]{64}", result.input_fingerprint), result.input_fingerprint


def test_positions_are_one_per_non_cash_asset_sorted_by_symbol() -> None:
    result = run(
        buy(key(10, "e1"), "KAS", "USDT", "100", "10"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "30000"),
        buy(key(12, "e3"), "ADA", "USDC", "10", "5"),
    )

    assert [found.asset for found in result.positions] == ["ADA", "BTC", "KAS"]


# --------------------------------------------------------------------------------------
# Buy: cash given, non-cash received
# --------------------------------------------------------------------------------------


def test_buy_without_a_fee() -> None:
    result = run(buy(key(10, "e1"), "BTC", "USDT", "2", "60000"))

    expect(result, "BTC", quantity="2", cost_basis="60000", average="30000")
    assert result.lots[0].cost_basis == Decimal("60000")


def test_fee_in_the_quote_of_a_buy_raises_the_basis_by_the_fee() -> None:
    """Fee in the asset given: 30000 + 30 goes out, and all of it is basis."""
    result = run(buy(key(10, "e1"), "BTC", "USDT", "1", "30000", "30", "USDT"))

    expect(result, "BTC", quantity="1", cost_basis="30030", average="30030")


def test_fee_in_the_base_of_a_buy_shrinks_the_quantity_and_keeps_the_basis() -> None:
    """Fee in the asset received: 1000 - 1 KAS arrive for 100, so the unit cost rises."""
    result = run(buy(key(10, "e1"), "KAS", "USDT", "1000", "100", "1", "KAS"))

    expect(result, "KAS", quantity="999", cost_basis="100", average="0.100100100100100100")
    assert result.lots[0].quantity == Decimal("999")


def test_fee_in_a_third_cash_asset_of_a_buy_is_basis() -> None:
    """USDC is cash but neither leg: worth its amount, added to the value given."""
    result = run(buy(key(10, "e1"), "BTC", "USDT", "1", "30000", "12.5", "USDC"))

    expect(result, "BTC", quantity="1", cost_basis="30012.5")
    assert result.warnings == ()


def test_fee_in_a_third_asset_is_carried_at_its_average_cost() -> None:
    """docs example 3: 2 of 10 BGB leave with 10 x 2 / 10 = 2 of basis, which joins BTC's."""
    result = run(
        buy(key(10, "e1"), "BGB", "USDT", "10", "10"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "30000", "2", "BGB"),
    )

    expect(result, "BTC", quantity="1", cost_basis="30002")
    expect(result, "BGB", quantity="8", cost_basis="8", average="1")
    assert result.warnings == ()


def test_fee_in_a_third_asset_nobody_bought_is_unattributed() -> None:
    """docs example 4: two warnings about two facts, and a flag on each position."""
    result = run(buy(key(11, "e2"), "BTC", "USDT", "1", "30000", "2", "BGB"))

    expect(result, "BTC", quantity="1", cost_basis="30000", flags=UNATTRIBUTED)
    expect(result, "BGB", quantity="0", cost_basis="0", flags=INCOMPLETE)
    assert result.warnings == (
        NegativeInventory(key(11, "e2"), "BGB", Decimal("2")),
        UnattributedFee(key(11, "e2"), "BGB", Decimal("2"), "BTC"),
    )


def test_fee_in_a_third_asset_partly_held_is_split_between_carried_and_unattributed() -> None:
    """1 BGB held at cost 1.5; a fee of 3 BGB takes that 1.5 and leaves 2 BGB unattributed."""
    result = run(
        buy(key(10, "e1"), "BGB", "USDT", "1", "1.5"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "30000", "3", "BGB"),
    )

    expect(result, "BTC", quantity="1", cost_basis="30001.5", flags=UNATTRIBUTED)
    expect(result, "BGB", quantity="0", cost_basis="0", flags=INCOMPLETE)
    assert result.warnings == (
        NegativeInventory(key(11, "e2"), "BGB", Decimal("2")),
        UnattributedFee(key(11, "e2"), "BGB", Decimal("2"), "BTC"),
    )


def test_fee_in_a_third_asset_of_unknown_cost_is_unattributed_without_a_shortfall() -> None:
    """10 BGB held at unknown cost cover the fee in quantity, but not in value.

    Nothing is short, so there is no `NegativeInventory` -- only the `UnattributedFee` for
    the 2 BGB whose cost nobody knows. BGB keeps 8 unknown-cost units.
    """
    result = run(
        adjust(key(9, "m1", "manual"), "BGB", "10", None),
        buy(key(11, "e2"), "BTC", "USDT", "1", "30000", "2", "BGB"),
    )

    expect(result, "BTC", quantity="1", cost_basis="30000", flags=UNATTRIBUTED)
    expect(result, "BGB", quantity="8", unknown="8", cost_basis="0", flags=UNKNOWN)
    assert result.warnings == (UnattributedFee(key(11, "e2"), "BGB", Decimal("2"), "BTC"),)


def test_a_cash_rebate_in_the_quote_of_a_buy_lowers_the_basis() -> None:
    result = run(buy(key(10, "e1"), "ETH", "USDT", "1", "3000", "-0.3", "USDT"))

    expect(result, "ETH", quantity="1", cost_basis="2999.7")


def test_a_rebate_in_the_base_of_a_buy_raises_the_quantity() -> None:
    result = run(buy(key(10, "e1"), "KAS", "USDT", "1000", "100", "-10", "KAS"))

    expect(result, "KAS", quantity="1010", cost_basis="100")


def test_a_third_cash_asset_rebate_lowers_the_basis() -> None:
    result = run(buy(key(10, "e1"), "ETH", "USDT", "1", "3100", "-0.5", "USDC"))

    expect(result, "ETH", quantity="1", cost_basis="3099.5")


def test_r7_a_third_cash_rebate_larger_than_the_trade_drives_the_basis_negative() -> None:
    """R7: accepted, and the arithmetic is simply followed: 10 - 20 = -10."""
    result = run(buy(key(10, "e1"), "BTC", "USDT", "1", "10", "-20", "USDC"))

    expect(result, "BTC", quantity="1", cost_basis="-10", average="-10")


def test_a_non_cash_third_asset_rebate_is_acquired_at_unknown_cost() -> None:
    """0.2 BGB arrive with no known cost: a lot, an unknown-basis quantity, no value moved.

    R4 fixes the order of the two lots one trade emits: the fee leg before the received leg.
    """
    result = run(buy(key(10, "e1"), "ETH", "USDT", "0.5", "1750", "-0.2", "BGB"))

    expect(result, "ETH", quantity="0.5", cost_basis="1750")
    expect(result, "BGB", quantity="0.2", unknown="0.2", cost_basis="0", flags=UNKNOWN)
    assert [
        (lot.asset, lot.quantity, lot.cost_basis, lot.unknown_basis_quantity) for lot in result.lots
    ] == [
        ("BGB", Decimal("0.2"), Decimal("0"), Decimal("0.2")),
        ("ETH", Decimal("0.5"), Decimal("1750"), Decimal("0")),
    ]


# --------------------------------------------------------------------------------------
# Sale: non-cash given, cash received
# --------------------------------------------------------------------------------------


def test_a_partial_sale_gives_up_the_proportional_basis() -> None:
    """docs example 1: 70000 x 0.5 / 2 = 17500 of basis against 25000 of proceeds."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "40000"),
        sell(key(12, "e3"), "BTC", "USDT", "0.5", "25000"),
    )

    expect(result, "BTC", quantity="1.5", cost_basis="52500", average="35000", realized="7500")


def test_fee_in_the_quote_of_a_sale_reduces_the_proceeds_by_the_fee() -> None:
    """Fee in the asset received: 33000 - 33 = 32967 of proceeds against 30000."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e3"), "BTC", "USDT", "1", "33000", "33", "USDT"),
    )

    expect(result, "BTC", quantity="0", cost_basis="0", realized="2967")


def test_fee_in_the_base_of_a_sale_disposes_of_more() -> None:
    """Fee in the asset given: 0.5 + 0.1 BTC leave, taking 0.6 x 30000 = 18000 of basis."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e3"), "BTC", "USDT", "0.5", "20000", "0.1", "BTC"),
    )

    expect(result, "BTC", quantity="0.4", cost_basis="12000", realized="2000")


def test_fee_in_a_third_cash_asset_of_a_sale_reduces_the_proceeds() -> None:
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e3"), "BTC", "USDT", "1", "33000", "7", "USDC"),
    )

    expect(result, "BTC", quantity="0", cost_basis="0", realized="2993")


def test_fee_in_a_third_asset_of_a_sale_reduces_the_proceeds_by_its_carried_cost() -> None:
    """2 BGB at 1.5 each: proceeds are 33000 - 3, and BGB gives up 3 of basis."""
    result = run(
        buy(key(9, "e0"), "BGB", "USDT", "10", "15"),
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e3"), "BTC", "USDT", "1", "33000", "2", "BGB"),
    )

    expect(result, "BTC", quantity="0", cost_basis="0", realized="2997")
    expect(result, "BGB", quantity="8", cost_basis="12", average="1.5")


def test_an_unattributed_fee_on_a_sale_is_charged_to_the_asset_sold() -> None:
    """R3: for a sale, `charged_to` is the given asset. Proceeds keep the fee's full value."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e3"), "BTC", "USDT", "1", "33000", "2", "BGB"),
    )

    expect(result, "BTC", quantity="0", cost_basis="0", realized="3000", flags=UNATTRIBUTED)
    assert result.warnings[-1] == UnattributedFee(key(12, "e3"), "BGB", Decimal("2"), "BTC")


def test_sell_more_than_held_takes_everything_and_warns() -> None:
    """docs example 5: 60000 x 1 / 1.5 = 40000 matched, 20000 unmatched, 0.5 short."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e2"), "BTC", "USDT", "1.5", "60000"),
    )

    expect(
        result,
        "BTC",
        quantity="0",
        cost_basis="0",
        realized="10000",
        unmatched="20000",
        flags=INCOMPLETE,
    )
    assert result.warnings == (NegativeInventory(key(12, "e2"), "BTC", Decimal("0.5")),)


def test_sell_more_than_held_with_nothing_held_never_raises() -> None:
    """I1 at its extreme: a sale with no history at all. Everything is unmatched."""
    result = run(sell(key(12, "e2"), "BTC", "USDT", "0.25", "10000", "10", "USDT"))

    expect(
        result,
        "BTC",
        quantity="0",
        cost_basis="0",
        realized="0",
        unmatched="9990",
        flags=INCOMPLETE,
    )
    warning = result.warnings[0]
    assert isinstance(warning, NegativeInventory)
    assert warning.asset == "BTC"
    assert warning.key.occurred_at == at(12)
    assert warning.shortfall == Decimal("0.25")


def test_sell_more_than_held_names_the_fee_folded_quantity() -> None:
    """A base fee on a sale is part of what is given: 1 + 0.01 of 1 held is 0.01 short."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e2"), "BTC", "USDT", "1", "33000", "0.01", "BTC"),
    )

    assert result.warnings == (NegativeInventory(key(12, "e2"), "BTC", Decimal("0.01")),)


def test_history_incomplete_is_sticky_after_the_pool_refills() -> None:
    result = run(
        sell(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "31000"),
    )

    expect(result, "BTC", quantity="1", cost_basis="31000", unmatched="30000", flags=INCOMPLETE)


def test_a_sale_of_partly_unknown_cost_splits_the_proceeds() -> None:
    """docs example 8: known 0.5 BTC take 15000 of basis against 20000; 40000 unmatched."""
    result = run(
        adjust(key(9, "m1", "manual"), "BTC", "2", None),
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e2"), "BTC", "USDT", "1.5", "60000"),
    )

    expect(
        result,
        "BTC",
        quantity="1.5",
        unknown="1",
        cost_basis="15000",
        average="30000",
        realized="5000",
        unmatched="40000",
        flags=UNKNOWN,
    )
    assert result.warnings == ()


def test_unknown_basis_is_not_sticky() -> None:
    """Once the unknown-cost units are gone, the flag goes with them."""
    result = run(
        adjust(key(9, "m1", "manual"), "BTC", "1", None),
        sell(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "31000"),
    )

    expect(result, "BTC", quantity="1", cost_basis="31000", unmatched="30000")


def test_full_liquidation_residue_goes_to_realized() -> None:
    """docs example 6: three thirds, each basis rounded, the last one takes what is left.

    Basis given up: 0.333333333333333333, then 0.333333333333333334 (a tie on an odd digit,
    to even), then the remaining 0.333333333333333333. They sum to exactly 1, so realized
    is exactly 1.5 - 1 = 0.5 and the basis is exactly zero at quantity zero.
    """
    events: list[AccountingEvent] = [
        buy(key(10, "e1"), "KAS", "USDT", "3", "1"),
        sell(key(11, "e2"), "KAS", "USDT", "1", "0.5"),
        sell(key(12, "e3"), "KAS", "USDT", "1", "0.5"),
        sell(key(13, "e4"), "KAS", "USDT", "1", "0.5"),
    ]

    after_one = replay(events[:2], CONFIG)
    after_two = replay(events[:3], CONFIG)
    after_all = replay(events, CONFIG)

    expect(
        after_one,
        "KAS",
        quantity="2",
        cost_basis="0.666666666666666667",
        realized="0.166666666666666667",
    )
    expect(
        after_two,
        "KAS",
        quantity="1",
        cost_basis="0.333333333333333333",
        realized="0.333333333333333333",
    )
    expect(after_all, "KAS", quantity="0", cost_basis="0", realized="0.5")
    assert position(after_all, "KAS").cost_basis == 0


def test_full_liquidation_after_an_unknown_basis_split_zeroes_every_part() -> None:
    """I4 with both parts in play: known and unknown are both empty after a take-all."""
    result = run(
        adjust(key(9, "m1", "manual"), "KAS", "7", None),
        buy(key(10, "e1"), "KAS", "USDT", "3", "1"),
        sell(key(11, "e2"), "KAS", "USDT", "4", "2"),
        sell(key(12, "e3"), "KAS", "USDT", "6", "3"),
    )

    found = position(result, "KAS")
    assert found.quantity == 0
    assert found.unknown_basis_quantity == 0
    assert found.cost_basis == 0
    assert found.average_cost is None
    assert result.warnings == ()


# --------------------------------------------------------------------------------------
# Swap: non-cash given, non-cash received
# --------------------------------------------------------------------------------------


def test_a_swap_carries_the_cost_over_and_realizes_nothing() -> None:
    """docs example 7: half of 1 BTC leaves with 15000, which becomes the KAS cost."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "KAS", "BTC", "100000", "0.5"),
    )

    expect(result, "BTC", quantity="0.5", cost_basis="15000", average="30000")
    expect(result, "KAS", quantity="100000", cost_basis="15000", average="0.15")


def test_a_swap_with_a_fee_in_the_received_asset() -> None:
    """10 KAS of fee: 99990 arrive for the same 15000."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "KAS", "BTC", "100000", "0.5", "10", "KAS"),
    )

    expect(result, "KAS", quantity="99990", cost_basis="15000")


def test_a_swap_with_a_cash_fee_adds_the_fee_to_the_received_cost() -> None:
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "KAS", "BTC", "100000", "0.5", "15", "USDT"),
    )

    expect(result, "KAS", quantity="100000", cost_basis="15015")
    expect(result, "BTC", quantity="0.5", cost_basis="15000")


def test_a_swap_of_partly_unknown_cost_carries_only_the_known_share() -> None:
    """Y = 1 known (cost 100) + 1 unknown; giving 1 Y takes 0.5 known with 50 of basis.

    known_in = 10 x 0.5 / 1 = 5 X at a cost of 50, and unknown_in = 5.
    """
    result = run(
        adjust(key(9, "m1", "manual"), "ETH", "1", None),
        buy(key(10, "e1"), "ETH", "USDT", "1", "100"),
        sell(key(11, "e2"), "ETH", "BTC", "1", "10"),
    )

    expect(result, "ETH", quantity="1", unknown="0.5", cost_basis="50", flags=UNKNOWN)
    expect(result, "BTC", quantity="10", unknown="5", cost_basis="50", average="10", flags=UNKNOWN)
    assert result.unallocated_costs == 0
    lot = result.lots[-1]
    assert (lot.asset, lot.quantity, lot.cost_basis, lot.unknown_basis_quantity) == (
        "BTC",
        Decimal("10"),
        Decimal("50"),
        Decimal("5"),
    )


def test_a_swap_with_no_known_part_sends_its_value_to_unallocated_costs() -> None:
    """DOGE held at unknown cost only; the 0.2 USDT fee has no known quantity to attach to."""
    result = run(
        adjust(key(9, "m1", "manual"), "DOGE", "1000", None),
        sell(key(10, "e1"), "DOGE", "ETH", "400", "0.05", "0.2", "USDT"),
    )

    expect(result, "ETH", quantity="0.05", unknown="0.05", cost_basis="0", flags=UNKNOWN)
    expect(result, "DOGE", quantity="600", unknown="600", cost_basis="0", flags=UNKNOWN)
    assert result.unallocated_costs == Decimal("0.2")
    lot = result.lots[-1]
    assert (lot.asset, lot.quantity, lot.cost_basis, lot.unknown_basis_quantity) == (
        "ETH",
        Decimal("0.05"),
        Decimal("0"),
        Decimal("0.05"),
    )


def test_an_unattributed_fee_on_a_swap_is_charged_to_the_asset_received() -> None:
    """R3: for a swap, `charged_to` is the received asset, whose cost the fee would join."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "KAS", "BTC", "100000", "0.5", "3", "BGB"),
    )

    expect(result, "KAS", quantity="100000", cost_basis="15000", flags=UNATTRIBUTED)
    expect(result, "BTC", quantity="0.5", cost_basis="15000")
    assert result.warnings[-1] == UnattributedFee(key(11, "e2"), "BGB", Decimal("3"), "KAS")


def test_r4_a_short_sale_with_a_short_fee_warns_given_leg_first() -> None:
    """R4: the given leg is disposed of before the fee leg, so its warning comes first."""
    result = run(sell(key(12, "e2"), "BTC", "USDT", "1", "30000", "2", "BGB"))

    assert result.warnings == (
        NegativeInventory(key(12, "e2"), "BTC", Decimal("1")),
        NegativeInventory(key(12, "e2"), "BGB", Decimal("2")),
        UnattributedFee(key(12, "e2"), "BGB", Decimal("2"), "BTC"),
    )


# --------------------------------------------------------------------------------------
# Conversion: cash given, cash received
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "fee", "fee_asset", "unallocated"),
    [
        pytest.param("buy", "0.1", "USDT", "0.1", id="fee in the cash given"),
        pytest.param("buy", "0.1", "USDC", "0.1", id="fee in the cash received"),
        pytest.param("sell", "0.25", "USDT", "0.25", id="sell, fee in the cash received"),
        pytest.param("buy", "-0.05", "USDT", "-0.05", id="a cash rebate"),
        pytest.param("buy", "0", None, "0", id="no fee"),
    ],
)
def test_a_conversion_changes_no_position_and_its_fee_is_unallocated(
    side: str, fee: str, fee_asset: str | None, unallocated: str
) -> None:
    """docs example 11, and its variations: both sides are pinned at 1."""
    make = buy if side == "buy" else sell
    result = run(make(key(10, "e1"), "USDC", "USDT", "100", "100", fee, fee_asset))

    assert result.positions == ()
    assert result.lots == ()
    assert result.unallocated_costs == Decimal(unallocated)


def test_a_conversion_fee_in_a_third_cash_asset_is_unallocated() -> None:
    config = AccountingConfig(frozenset({"USDC", "USDT", "DAI"}))

    result = replay([buy(key(10, "e1"), "USDC", "USDT", "100", "100", "0.3", "DAI")], config)

    assert result.positions == ()
    assert result.unallocated_costs == Decimal("0.3")


def test_a_conversion_fee_in_a_non_cash_asset_moves_its_carried_cost() -> None:
    """0.25 BGB at 1.5 each: 0.375 leaves BGB's basis and lands in `unallocated_costs`."""
    result = run(
        buy(key(9, "e0"), "BGB", "USDT", "10", "15"),
        sell(key(10, "e1"), "USDC", "USDT", "500", "499.9", "0.25", "BGB"),
    )

    expect(result, "BGB", quantity="9.75", cost_basis="14.625", average="1.5")
    assert result.unallocated_costs == Decimal("0.375")
    assert [found.asset for found in result.positions] == ["BGB"]


def test_an_unattributed_conversion_fee_is_charged_to_nothing() -> None:
    """R3: `charged_to` is `None` for a conversion, and no position is flagged for it."""
    result = run(sell(key(10, "e1"), "USDC", "USDT", "500", "499.9", "0.25", "BGB"))

    expect(result, "BGB", quantity="0", cost_basis="0", flags=INCOMPLETE)
    assert result.warnings == (
        NegativeInventory(key(10, "e1"), "BGB", Decimal("0.25")),
        UnattributedFee(key(10, "e1"), "BGB", Decimal("0.25"), None),
    )
    assert result.unallocated_costs == 0


def test_a_conversion_with_a_non_cash_rebate_acquires_it_at_unknown_cost() -> None:
    result = run(buy(key(10, "e1"), "USDC", "USDT", "100", "100", "-0.2", "BGB"))

    expect(result, "BGB", quantity="0.2", unknown="0.2", cost_basis="0", flags=UNKNOWN)
    assert result.unallocated_costs == 0
    assert len(result.lots) == 1


# --------------------------------------------------------------------------------------
# R8: a zero fee naming an asset
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("fee_asset", ["BGB", "SOL", "BTC", "USDT", "USDC"])
def test_r8_a_zero_fee_creates_no_leg_and_touches_no_position(fee_asset: str) -> None:
    """The same answer as no fee at all, and no position for a third asset it names."""
    named = run(buy(key(10, "e1"), "BTC", "USDT", "1", "30000", "0", fee_asset))
    bare = run(buy(key(10, "e1"), "BTC", "USDT", "1", "30000"))

    assert [found.asset for found in named.positions] == ["BTC"]
    assert named.positions == bare.positions
    assert named.lots == bare.lots
    assert named.warnings == ()
    assert named.unallocated_costs == 0


# --------------------------------------------------------------------------------------
# Adjustments and transfers
# --------------------------------------------------------------------------------------


def test_an_adjustment_with_a_cost_is_known_basis() -> None:
    """docs example 9: 0.5 at 25000 is 12500, and with 30000 more the sale empties the pool."""
    result = run(
        adjust(key(9, "m1", "manual"), "BTC", "0.5", "25000"),
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e2"), "BTC", "USDT", "1.5", "60000"),
    )

    expect(result, "BTC", quantity="0", cost_basis="0", realized="17500")
    assert result.warnings == ()


def test_an_adjustment_cost_is_rounded_once_at_18_places() -> None:
    """2.3 x 2500.123456789012345678 = 5750.2839506147283950594, rounded to ...059."""
    result = run(adjust(key(9, "m1", "manual"), "ETH", "2.3", "2500.123456789012345678"))

    expect(result, "ETH", quantity="2.3", cost_basis="5750.283950614728395059")
    assert result.lots[0].cost_basis == Decimal("5750.283950614728395059")


def test_an_adjustment_at_zero_cost_is_known_and_free() -> None:
    """Zero is not unknown: the units are in the average, which is zero."""
    result = run(adjust(key(9, "m1", "manual"), "KAS", "100", "0"))

    expect(result, "KAS", quantity="100", cost_basis="0", average="0")


def test_an_adjustment_without_a_cost_is_unknown_basis() -> None:
    result = run(adjust(key(9, "m1", "manual"), "KAS", "100", None))

    expect(result, "KAS", quantity="100", unknown="100", cost_basis="0", flags=UNKNOWN)
    lot = result.lots[0]
    assert (lot.quantity, lot.cost_basis, lot.unknown_basis_quantity) == (
        Decimal("100"),
        Decimal("0"),
        Decimal("100"),
    )


def test_an_adjustment_of_a_cash_asset_changes_nothing_but_the_count() -> None:
    result = run(adjust(key(9, "m1", "manual"), "USDT", "1000", "1"))

    assert result.positions == ()
    assert result.lots == ()
    assert result.unallocated_costs == 0
    assert result.event_count == 1


def test_a_transfer_creates_no_position() -> None:
    result = run(move(key(9, "w1"), "SOL", "5"))

    assert result.positions == ()
    assert result.lots == ()
    assert result.event_count == 1


def test_the_cash_set_is_configuration() -> None:
    """With only USDT as cash, USDC is inventory: it gets a position and a basis."""
    config = AccountingConfig(frozenset({"USDT"}))

    result = replay([buy(key(10, "e1"), "USDC", "USDT", "100", "100", "0.1", "USDT")], config)

    expect(result, "USDC", quantity="100", cost_basis="100.1")


# --------------------------------------------------------------------------------------
# Identity, duplicates and order
# --------------------------------------------------------------------------------------


def test_a_repeated_fill_counts_once() -> None:
    fill = buy(key(10, "e1"), "BTC", "USDT", "1", "30000")

    result = run(fill, fill, fill)

    assert result.event_count == 1
    expect(result, "BTC", quantity="1", cost_basis="30000")


def test_a_repeat_spelled_differently_but_numerically_equal_counts_once() -> None:
    """`1` and `1.000` are one amount, so this is a duplicate and not a conflict."""
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000", "30", "USDT"),
        buy(key(10, "e1"), "BTC", "USDT", "1.000", "30000.0", "30.00", "USDT"),
    )

    assert result.event_count == 1
    expect(result, "BTC", quantity="1", cost_basis="30030")


@pytest.mark.parametrize(
    "conflicting",
    [
        pytest.param(buy(key(10, "e1"), "BTC", "USDT", "2", "30000"), id="another quantity"),
        pytest.param(buy(key(11, "e1"), "BTC", "USDT", "1", "30000"), id="another time"),
        pytest.param(sell(key(10, "e1"), "BTC", "USDT", "1", "30000"), id="another side"),
        pytest.param(
            buy(key(10, "e1"), "BTC", "USDT", "1", "30000", "0", "BGB"), id="a named zero fee"
        ),
    ],
)
def test_conflicting_duplicate_raises(conflicting: AccountingEvent) -> None:
    """One identity, two contents: corrupted input, refused as `ConflictingEventError`."""
    original = buy(key(10, "e1"), "BTC", "USDT", "1", "30000")

    with pytest.raises(ConflictingEventError):
        run(original, conflicting)
    with pytest.raises(ValueError, match=r"."):
        run(conflicting, original)


def test_conflicting_adjustments_raise_too() -> None:
    with pytest.raises(ConflictingEventError):
        run(
            adjust(key(9, "m1", "manual"), "BTC", "1", None),
            adjust(key(9, "m1", "manual"), "BTC", "1", "30000"),
        )


def test_kind_is_part_of_identity() -> None:
    """A trade and a transfer may share a venue id: two number spaces, two events."""
    result = run(
        buy(key(10, "5001"), "BTC", "USDT", "1", "30000"),
        move(key(10, "5001"), "BTC", "1"),
    )

    assert result.event_count == 2
    expect(result, "BTC", quantity="1", cost_basis="30000")


def test_events_are_replayed_in_time_order_whatever_the_input_order() -> None:
    later = sell(key(12, "e2"), "BTC", "USDT", "1", "35000")
    earlier = buy(key(10, "e1"), "BTC", "USDT", "1", "30000")

    result = run(later, earlier)

    expect(result, "BTC", quantity="0", cost_basis="0", realized="5000")
    assert result.warnings == ()


def test_same_millisecond_ids_compare_as_plain_strings() -> None:
    """ "1000" sorts before "999" as a string, so the sale runs first and falls short.

    Numerically the buy would come first and nothing would be short. The spec chooses the
    string order and records the transient shortfall as a known, rare risk.
    """
    moment = at(12, 0, 123000)
    result = run(
        buy(key(moment, "999"), "BTC", "USDT", "0.25", "17000"),
        sell(key(moment, "1000"), "BTC", "USDT", "0.25", "18000"),
    )

    assert result.warnings == (NegativeInventory(key(moment, "1000"), "BTC", Decimal("0.25")),)
    expect(
        result,
        "BTC",
        quantity="0.25",
        cost_basis="17000",
        unmatched="18000",
        flags=INCOMPLETE,
    )


def test_source_breaks_a_tie_before_the_id() -> None:
    """At one instant, "bingx" < "bitget": the bingx buy runs before the bitget sale."""
    moment = at(12)
    result = run(
        sell(key(moment, "a", "bitget"), "BTC", "USDT", "1", "35000"),
        buy(key(moment, "z", "bingx"), "BTC", "USDT", "1", "30000"),
    )

    assert result.warnings == ()
    expect(result, "BTC", quantity="0", cost_basis="0", realized="5000")


def test_kind_breaks_a_tie_on_the_whole_key() -> None:
    """R10: "adjustment" < "trade", so an adjustment sharing a sale's key is applied first."""
    shared = key(12, "x1", "manual")
    result = run(
        sell(shared, "BTC", "USDT", "1", "35000"),
        adjust(shared, "BTC", "1", "30000"),
    )

    assert result.warnings == ()
    expect(result, "BTC", quantity="0", cost_basis="0", realized="5000")


# --------------------------------------------------------------------------------------
# R1: the average never raises
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            buy(key(10, "e1"), "BTC", "USDT", "0.000000000000000001", "100"), id="a dust buy"
        ),
        pytest.param(
            buy(key(10, "e1"), "BTC", "USDT", "1", "30000", "0.999999999999999999", "BTC"),
            id="a fee leaving one unit",
        ),
    ],
)
def test_r1_an_average_that_does_not_fit_is_none_and_nothing_raises(
    event: AccountingEvent,
) -> None:
    """Validated input can make `C / Qk` exceed 20 integer digits. R1: `None`, not a raise.

    The basis and quantity are still reported beside it.
    """
    result = run(event)

    found = position(result, "BTC")
    assert found.quantity == Decimal("0.000000000000000001")
    assert found.cost_basis in {Decimal("100"), Decimal("30000")}
    assert found.average_cost is None


def test_r1_the_widest_average_that_fits_is_reported() -> None:
    """99999999999999999999 cash units for one unit: 20 integer digits, which fit."""
    result = run(buy(key(10, "e1"), "BTC", "USDT", "1", "99999999999999999999"))

    assert position(result, "BTC").average_cost == Decimal("99999999999999999999")


def test_r1_an_average_that_rounds_up_past_the_ceiling_is_none() -> None:
    """A quotient just under 10**20 that rounds up to it does not fit either.

    Two units for a basis of 199999999999999999999.999999999999999999 -- the second buy's
    one-unit fee is what takes the basis past what one amount can spell. The exact average
    is 99999999999999999999.9999999999999999995, a tie on an odd digit, which half-even
    rounds up to 21 integer digits.
    """
    widest = "99999999999999999999.999999999999999999"
    result = run(
        buy(key(10, "e1"), "BTC", "USDT", "1", widest),
        buy(key(11, "e2"), "BTC", "USDT", "1", widest, "0.000000000000000001", "USDT"),
    )

    found = position(result, "BTC")
    assert found.cost_basis == Decimal("199999999999999999999.999999999999999999")
    assert found.average_cost is None


# --------------------------------------------------------------------------------------
# Ambient context
# --------------------------------------------------------------------------------------


def test_replay_inside_a_hostile_decimal_context_is_unchanged() -> None:
    """The spec's own example context: `prec=6, rounding=ROUND_UP`, around docs example 6."""
    events = [
        buy(key(10, "e1"), "KAS", "USDT", "3", "1"),
        sell(key(11, "e2"), "KAS", "USDT", "1", "0.5"),
        buy(key(12, "e3"), "BTC", "USDT", "0.123456789012345678", "8641.975308641975308642"),
    ]
    outside = replay(events, CONFIG)
    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_UP
        inside = replay(events, CONFIG)

    assert inside == outside
    assert [str(found.cost_basis) for found in inside.positions] == [
        str(found.cost_basis) for found in outside.positions
    ]
