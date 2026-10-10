"""`domain.investment`: invested, explained, difference and profit, R7 to R10 (criterion 4).

Every expected figure is a literal. Every unknown case is asserted as `None` with its reason,
never as `0`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from portfolio.domain.exchange_exports import OperationKind
from portfolio.domain.investment import (
    AssetInvestment,
    Holding,
    InvestedOnDay,
    InvestmentUnavailable,
    Movement,
    TotalInvestment,
    summarize_investment,
)


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def buy(
    asset: str,
    quantity: str,
    quote: str | None,
    amount: str | None,
    *,
    day: int = 1,
    fee: tuple[str, str] | None = None,
    kind: OperationKind = OperationKind.BUY,
) -> Movement:
    return Movement(
        executed_at=at(day),
        kind=kind,
        asset=asset,
        quantity=Decimal(quantity),
        quote_currency=quote,
        quote_amount=None if amount is None else Decimal(amount),
        fee_asset=None if fee is None else fee[0],
        fee_amount=None if fee is None else Decimal(fee[1]),
    )


def sell(
    asset: str,
    quantity: str,
    quote: str,
    amount: str,
    *,
    day: int = 1,
    fee: tuple[str, str] | None = None,
) -> Movement:
    return buy(asset, quantity, quote, amount, kind=OperationKind.SELL, day=day, fee=fee)


def moved(
    asset: str, kind: OperationKind, quantity: str, fee: tuple[str, str] | None = None
) -> Movement:
    return buy(asset, quantity, None, None, kind=kind, fee=fee)


def test_invested_counts_fees_paid_in_the_quote_and_explained_counts_fees_in_the_coin() -> None:
    movements = [
        # 1000 KAS for 25 USDT, the fee taken in KAS.
        buy("KAS", "1000", "USDT", "25", fee=("KAS", "1")),
        # 500 KAS for 15 USDC plus a 0.5 USDC fee.
        buy("KAS", "500", "USDC", "15", fee=("USDC", "0.5"), day=2),
        # 200 KAS sold for 8 DAI, less a 0.2 DAI fee.
        sell("KAS", "200", "DAI", "8", fee=("DAI", "0.2"), day=3),
        moved("KAS", OperationKind.REWARD, "10"),
        moved("KAS", OperationKind.WITHDRAWAL, "1300", fee=("KAS", "2")),
        moved("KAS", OperationKind.DEPOSIT, "1300"),
        moved("KAS", OperationKind.TRANSFER, "-1300"),
        moved("KAS", OperationKind.OTHER, "99"),
        # Another coin entirely: ignored.
        buy("NEXO", "100", "USDT", "100"),
    ]

    result = summarize_investment(movements, [Holding("KAS", Decimal(1300), Decimal(65))])

    assert result.assets == (
        AssetInvestment(
            asset="KAS",
            invested=Decimal("32.7"),
            value=Decimal(65),
            pnl=Decimal("32.3"),
            pnl_pct=Decimal("98.7768"),
            held=Decimal(1300),
            explained=Decimal(1307),
            difference=Decimal(-7),
            trades=3,
            unvalued_trades=0,
            unavailable=None,
        ),
    )
    assert result.total == TotalInvestment(
        invested=Decimal("32.7"),
        value=Decimal(65),
        pnl=Decimal("32.3"),
        pnl_pct=Decimal("98.7768"),
        unavailable=None,
    )
    assert result.invested_by_day == (
        InvestedOnDay(date(2026, 5, 1), Decimal(25)),
        InvestedOnDay(date(2026, 5, 2), Decimal("40.5")),
        InvestedOnDay(date(2026, 5, 3), Decimal("32.7")),
    )


def test_a_trade_not_priced_in_a_stablecoin_makes_invested_unknown_never_zero() -> None:
    movements = [
        buy("BTC", "0.01", "USDT", "1000", day=1),
        buy("BTC", "0.001", None, None, day=2),
        buy("BTC", "0.001", "USDT", "100", day=3),
    ]

    result = summarize_investment(movements, [Holding("BTC", Decimal("0.012"), Decimal(1200))])

    (btc,) = result.assets
    assert (btc.invested, btc.pnl, btc.pnl_pct) == (None, None, None)
    assert btc.unavailable is InvestmentUnavailable.UNVALUED_TRADES
    assert (btc.trades, btc.unvalued_trades) == (3, 1)
    assert btc.explained == Decimal("0.012")
    assert btc.difference == Decimal(0)
    assert result.total.invested is None
    assert result.total.unavailable is InvestmentUnavailable.UNVALUED_TRADES
    assert result.invested_by_day == (
        InvestedOnDay(date(2026, 5, 1), Decimal(1000)),
        InvestedOnDay(date(2026, 5, 2), None),
        InvestedOnDay(date(2026, 5, 3), None),
    )


def test_a_tracked_coin_paying_for_another_moves_it_and_is_unvalued_for_both() -> None:
    """KAS bought with BTC: BTC's quantity falls by the price, and neither has a USDT cost."""
    movements = [
        buy("BTC", "1", "USDT", "100000"),
        buy("KAS", "1000", "BTC", "0.0005", day=2),
        sell("NEXO", "10", "BTC", "0.0001", day=3),
        # A futures order quoted in BTC moves no BTC.
        buy("ETH", "1", "BTC", "5", kind=OperationKind.OTHER, day=3),
    ]

    result = summarize_investment(
        movements,
        [Holding("BTC", Decimal("0.9996"), None), Holding("KAS", Decimal(1000), Decimal(40))],
    )

    btc, kas = result.assets
    assert btc.explained == Decimal("0.9996")
    assert (btc.trades, btc.unvalued_trades, btc.invested) == (3, 2, None)
    assert (kas.trades, kas.unvalued_trades, kas.invested) == (1, 1, None)
    assert result.invested_by_day[-1] == InvestedOnDay(date(2026, 5, 3), None)


def test_a_coin_without_a_value_has_no_profit_and_says_why() -> None:
    result = summarize_investment([buy("KAS", "10", "USDT", "1")], [Holding("KAS", None, None)])

    (kas,) = result.assets
    assert (kas.invested, kas.value, kas.pnl, kas.pnl_pct) == (Decimal(1), None, None, None)
    assert kas.unavailable is InvestmentUnavailable.VALUE_UNKNOWN
    assert (kas.held, kas.difference) == (None, None)
    assert result.total == TotalInvestment(
        invested=Decimal(1),
        value=None,
        pnl=None,
        pnl_pct=None,
        unavailable=InvestmentUnavailable.VALUE_UNKNOWN,
    )


def test_nothing_invested_has_a_profit_but_no_percentage() -> None:
    movements = [buy("KAS", "10", "USDT", "1"), sell("KAS", "10", "USDT", "3", day=2)]

    result = summarize_investment(movements, [Holding("KAS", Decimal(0), Decimal(0))])

    (kas,) = result.assets
    assert (kas.invested, kas.pnl, kas.pnl_pct) == (Decimal(-2), Decimal(2), None)
    assert kas.unavailable is InvestmentUnavailable.NOTHING_INVESTED


def test_no_tracked_assets_is_an_empty_answer() -> None:
    result = summarize_investment([buy("KAS", "10", "USDT", "1")], [])

    assert result.assets == ()
    assert result.total == TotalInvestment(
        invested=Decimal(0),
        value=Decimal(0),
        pnl=Decimal(0),
        pnl_pct=None,
        unavailable=InvestmentUnavailable.NOTHING_INVESTED,
    )
    assert result.invested_by_day == ()


def test_assets_are_by_symbol_and_movements_by_time_whatever_order_they_arrive_in() -> None:
    movements = [
        buy("KAS", "10", "USDT", "2", day=5),
        buy("BTC", "1", "USDT", "3", day=4),
        buy("KAS", "10", "USDT", "1", day=4),
    ]

    result = summarize_investment(
        movements,
        [Holding("KAS", Decimal(20), Decimal(4)), Holding("BTC", Decimal(1), Decimal(3))],
    )

    assert [asset.asset for asset in result.assets] == ["BTC", "KAS"]
    assert result.invested_by_day == (
        InvestedOnDay(date(2026, 5, 4), Decimal(4)),
        InvestedOnDay(date(2026, 5, 5), Decimal(6)),
    )
    assert result.total.pnl_pct == Decimal("16.6667")
