"""`domain.portfolio`: net invested and the dashboard's summary (#154). Pure, so no fixtures.

Every expected figure is written out as a literal, never recomputed with the arithmetic under
test, so a test cannot agree with a bug by repeating it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portfolio.domain.accounting import DEFAULT_CASH_ASSETS
from portfolio.domain.exchanges import FillSide
from portfolio.domain.fill_totals import FillLine
from portfolio.domain.portfolio import (
    HoldingSummary,
    NetInvested,
    PricedQuantity,
    net_invested,
    summarize,
)

CASH: Final = DEFAULT_CASH_ASSETS


def line(
    side: FillSide,
    quote_quantity: str,
    *,
    base: str = "KAS",
    quote: str = "USDT",
    fee: str = "0",
    fee_asset: str | None = None,
) -> FillLine:
    return FillLine(
        base_asset=base,
        quote_asset=quote,
        side=side,
        quantity=Decimal(1),
        quote_quantity=Decimal(quote_quantity),
        fee_amount=Decimal(fee),
        fee_asset=fee_asset,
    )


# --------------------------------------------------------------------------------------
# net_invested
# --------------------------------------------------------------------------------------


def test_no_fills_is_zero_at_eighteen_places() -> None:
    result = net_invested([], CASH)

    assert result == NetInvested(amount=Decimal("0E-18"), unvalued_quotes=())
    assert str(result.amount) == "0E-18"


def test_buys_add_and_sells_subtract() -> None:
    result = net_invested(
        [line(FillSide.BUY, "1000"), line(FillSide.BUY, "300"), line(FillSide.SELL, "450.5")],
        CASH,
    )

    assert result.amount == Decimal("849.5")
    assert result.unvalued_quotes == ()


def test_the_example_the_owner_was_shown() -> None:
    """Bought for 1000, sold for 300, put the 300 back in: net 1000, where gross would be 1300."""
    result = net_invested(
        [line(FillSide.BUY, "1000"), line(FillSide.SELL, "300"), line(FillSide.BUY, "300")], CASH
    )

    assert result.amount == Decimal(1000)


def test_more_sold_than_bought_is_negative_and_not_clamped() -> None:
    result = net_invested([line(FillSide.BUY, "100"), line(FillSide.SELL, "250")], CASH)

    assert result.amount == Decimal(-150)


def test_a_fill_quoted_in_usdc_counts_one_for_one() -> None:
    result = net_invested([line(FillSide.BUY, "70", base="BTC", quote="USDC")], CASH)

    assert result.amount == Decimal(70)


@pytest.mark.parametrize("side", [FillSide.BUY, FillSide.SELL])
def test_a_fee_paid_in_cash_adds_on_either_side(side: FillSide) -> None:
    """On a buy it is spent on top of the quote; on a sell it is withheld from the proceeds."""
    result = net_invested([line(side, "0", fee="1.25", fee_asset="USDT")], CASH)

    assert result.amount == Decimal("1.25")


def test_a_cash_rebate_subtracts() -> None:
    result = net_invested([line(FillSide.BUY, "100", fee="-0.5", fee_asset="USDT")], CASH)

    assert result.amount == Decimal("99.5")


def test_a_fee_paid_in_the_base_asset_is_not_cash() -> None:
    """It reduces what was received, which the held quantity already shows."""
    result = net_invested([line(FillSide.BUY, "100", fee="3", fee_asset="KAS")], CASH)

    assert result.amount == Decimal(100)


def test_a_conversion_between_cash_assets_is_not_invested_but_its_fee_is() -> None:
    result = net_invested(
        [line(FillSide.BUY, "500", base="USDC", quote="USDT", fee="0.1", fee_asset="USDT")], CASH
    )

    assert result.amount == Decimal("0.1")
    assert result.unvalued_quotes == ()


def test_a_fill_quoted_in_crypto_is_left_out_and_named_once() -> None:
    result = net_invested(
        [
            line(FillSide.BUY, "100"),
            line(FillSide.BUY, "0.002", base="KAS", quote="BTC"),
            line(FillSide.SELL, "0.001", base="KAS", quote="BTC"),
            line(FillSide.BUY, "3", base="SOL", quote="ETH"),
        ],
        CASH,
    )

    assert result.amount == Decimal(100)
    assert result.unvalued_quotes == ("BTC", "ETH")


def test_a_value_that_is_not_a_side_is_refused() -> None:
    bogus = FillLine(
        base_asset="KAS",
        quote_asset="USDT",
        side="borrow",  # type: ignore[arg-type]
        quantity=Decimal(1),
        quote_quantity=Decimal(1),
        fee_amount=Decimal(0),
        fee_asset=None,
    )

    with pytest.raises(AssertionError):
        net_invested([bogus], CASH)


_AMOUNTS: Final = st.decimals(min_value=0, max_value=10**9, places=8, allow_nan=False)


@given(st.lists(st.tuples(st.sampled_from(list(FillSide)), _AMOUNTS), max_size=30))
def test_net_is_buys_minus_sells_whatever_the_order(fills: list[tuple[FillSide, Decimal]]) -> None:
    lines = [line(side, str(amount)) for side, amount in fills]
    buys = sum((amount for side, amount in fills if side is FillSide.BUY), Decimal(0))
    sells = sum((amount for side, amount in fills if side is FillSide.SELL), Decimal(0))

    assert net_invested(lines, CASH).amount == buys - sells
    assert net_invested(reversed(lines), CASH).amount == buys - sells


# --------------------------------------------------------------------------------------
# summarize
# --------------------------------------------------------------------------------------


def test_values_shares_and_pnl() -> None:
    """0.1 BTC at 60000 is 6000; 50000 KAS at 0.08 is 4000; 10000 against 8000 invested."""
    summary = summarize(
        [
            PricedQuantity("KAS", Decimal(50000), Decimal("0.08")),
            PricedQuantity("BTC", Decimal("0.1"), Decimal(60000)),
        ],
        Decimal(8000),
    )

    assert summary.total_value == Decimal(10000)
    assert summary.invested == Decimal(8000)
    assert summary.pnl == Decimal(2000)
    assert summary.pnl_pct == Decimal("25.0000")
    assert summary.holdings == (
        HoldingSummary("BTC", Decimal("0.1"), Decimal(60000), Decimal(6000), Decimal("60.0000")),
        HoldingSummary("KAS", Decimal(50000), Decimal("0.08"), Decimal(4000), Decimal("40.0000")),
    )


def test_every_value_is_carried_at_eighteen_places() -> None:
    summary = summarize([PricedQuantity("BTC", Decimal("0.1"), Decimal(60000))], Decimal(0))

    assert str(summary.total_value) == "6000.000000000000000000"
    assert str(summary.holdings[0].value) == "6000.000000000000000000"


def test_a_loss_is_negative_with_a_negative_percentage() -> None:
    summary = summarize([PricedQuantity("KAS", Decimal(1000), Decimal("0.05"))], Decimal(80))

    assert summary.pnl == Decimal(-30)
    assert summary.pnl_pct == Decimal("-37.5000")


def test_an_unpriced_holding_adds_nothing_and_is_listed_last() -> None:
    summary = summarize(
        [
            PricedQuantity("AAA", Decimal(5), None),
            PricedQuantity("KAS", Decimal(100), Decimal(1)),
        ],
        Decimal(50),
    )

    assert summary.total_value == Decimal(100)
    assert [holding.asset for holding in summary.holdings] == ["KAS", "AAA"]
    assert summary.holdings[1] == HoldingSummary("AAA", Decimal(5), None, None, None)
    assert summary.holdings[0].share_pct == Decimal("100.0000")


@pytest.mark.parametrize("invested", ["0", "-25"])
def test_no_percentage_on_nothing_invested_or_a_net_withdrawal(invested: str) -> None:
    summary = summarize([PricedQuantity("KAS", Decimal(10), Decimal(1))], Decimal(invested))

    assert summary.pnl == Decimal(10) - Decimal(invested)
    assert summary.pnl_pct is None


def test_no_share_of_a_total_of_zero() -> None:
    summary = summarize([PricedQuantity("KAS", Decimal(10), Decimal(0))], Decimal(1))

    assert summary.total_value == 0
    assert summary.holdings[0].value == 0
    assert summary.holdings[0].share_pct is None


def test_nothing_held() -> None:
    summary = summarize([], Decimal(500))

    assert summary.total_value == 0
    assert str(summary.total_value) == "0E-18"
    assert summary.pnl == Decimal(-500)
    assert summary.pnl_pct == Decimal("-100.0000")
    assert summary.holdings == ()


def test_equal_values_are_ordered_by_asset() -> None:
    summary = summarize(
        [
            PricedQuantity("ZZZ", Decimal(1), Decimal(5)),
            PricedQuantity("AAA", Decimal(5), Decimal(1)),
            PricedQuantity("MMM", Decimal(1), None),
            PricedQuantity("BBB", Decimal(1), None),
        ],
        Decimal(1),
    )

    assert [holding.asset for holding in summary.holdings] == ["AAA", "ZZZ", "BBB", "MMM"]


def test_a_value_is_rounded_once_half_to_even() -> None:
    """3 x 0.0000000000000000005 is 1.5E-18, which rounds to 2E-18 at eighteen places."""
    summary = summarize(
        [PricedQuantity("KAS", Decimal(3), Decimal("0.0000000000000000005"))], Decimal(0)
    )

    assert summary.holdings[0].value == Decimal("2E-18")
