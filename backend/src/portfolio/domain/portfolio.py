"""What the portfolio is worth against what went into it: the dashboard's three figures.

`net_invested(lines, cash_assets)` and `summarize(holdings, invested)` are the arithmetic
behind `GET /api/portfolio/summary` (#154). **Pure**: no I/O, no clock, no ORM. The service
reads the fills, the balances and the prices, and hands this module plain values.

## Invested is the cash that went into the fills, net

The owner's definition (#154): what the exchange fills spent buying, minus what they received
selling, in the cash assets -- USDT and USDC, each taken at one unit of account. Fill by fill:

* **Quoted in a cash asset**: a buy adds its `quote_quantity`, a sell subtracts it.
* **A fee paid in a cash asset** adds its amount on either side: on a buy it is cash spent on
  top of the quote, on a sell it is cash withheld from the proceeds. A rebate is negative and
  subtracts.
* **Base asset in cash** -- USDC bought with USDT -- is a conversion between two kinds of cash:
  nothing went into anything else, so its quote is not counted. Its fee still is: that cash is
  gone.
* **Quoted in anything else** -- KAS bought with BTC -- moved no cash, and has no cash value on
  its day without a price this module will not invent. It is not counted, and its quote asset
  is named in `unvalued_quotes`, so the figure can say it is partial instead of being quietly
  short.

Manual adjustments are not fills and are not counted: the owner's definition is the fills.

**This is not the accounting engine's cost basis.** The cost basis is the cost of what is
still held, and every sale reduces it. Invested here is the net cash flow, so it carries the
realized result inside it: `value - invested` is the portfolio's whole P/L, realized and
unrealized together, provided the cash a sale brought back is not counted in `value` -- and it
is not, because cash is not a holding.

## Every figure is exact or rounded once

Sums are `money.add` and `money.subtract`, which never round. A value is `price x quantity`
rounded once to `VALUE_SCALE`, as the positions are (`domain.accounting.valuation`), and a
percentage is one `money.divide` to `RETURN_PCT_SCALE`. Nothing here is a `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final, assert_never

from portfolio.domain.accounting import RETURN_PCT_SCALE, VALUE_SCALE
from portfolio.domain.exchanges import FillSide
from portfolio.domain.money import add, divide, multiply, quantize, subtract

if TYPE_CHECKING:
    from collections.abc import Iterable

    from portfolio.domain.fill_totals import FillLine

__all__ = [
    "HoldingSummary",
    "NetInvested",
    "PortfolioSummary",
    "PricedQuantity",
    "net_invested",
    "summarize",
]

_ZERO: Final = Decimal((0, (0,), -VALUE_SCALE))
"""Zero at `VALUE_SCALE` places, so an empty total reads `0E-18` like every value beside it."""

_HUNDRED: Final = Decimal(100)


@dataclass(frozen=True, slots=True)
class NetInvested:
    """The net cash the fills put in, and the quote assets of the fills it could not count.

    `unvalued_quotes` is sorted and holds each quote asset once. Empty means every fill that
    bought or sold something other than cash was counted.
    """

    amount: Decimal
    unvalued_quotes: tuple[str, ...]


def net_invested(lines: Iterable[FillLine], cash_assets: frozenset[str]) -> NetInvested:
    """What the fills spent buying minus what they received selling, in cash, net of fees.

    See the module docstring for the rule, fill by fill. A sum over no fills is zero at
    `VALUE_SCALE` places, the scale the fills are stored at.

    Matched on both sides rather than `BUY` and "anything else", so a value that is not a
    side is an `AssertionError` rather than a sale.
    """
    amount = _ZERO
    unvalued: set[str] = set()
    for line in lines:
        if line.fee_asset is not None and line.fee_asset in cash_assets:
            amount = add(amount, line.fee_amount)
        if line.base_asset in cash_assets:
            continue
        if line.quote_asset not in cash_assets:
            unvalued.add(line.quote_asset)
            continue
        match line.side:
            case FillSide.BUY:
                amount = add(amount, line.quote_quantity)
            case FillSide.SELL:
                amount = subtract(amount, line.quote_quantity)
            case _:
                assert_never(line.side)
    return NetInvested(amount=amount, unvalued_quotes=tuple(sorted(unvalued)))


@dataclass(frozen=True, slots=True)
class PricedQuantity:
    """How much of one asset is held, and its price; `None` when nothing prices it."""

    asset: str
    quantity: Decimal
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class HoldingSummary:
    """One holding valued, and its share of the total.

    `value` is `None` exactly when `price` is. `share_pct` is the value's percentage of the
    total at `RETURN_PCT_SCALE`, and `None` when there is no value or the total is not above
    zero, since a share of nothing is not a number.
    """

    asset: str
    quantity: Decimal
    price: Decimal | None
    value: Decimal | None
    share_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class PortfolioSummary:
    """The dashboard's figures.

    * `total_value` -- the sum of every holding that has a value. An unpriced holding adds
      nothing, and the caller says so: this module only knows it had no price.
    * `invested` -- passed in, as `net_invested` computed it. It can be negative: more came
      back from sales than went into buys.
    * `pnl` -- `total_value - invested`.
    * `pnl_pct` -- `pnl / invested x 100` at `RETURN_PCT_SCALE`; `None` when `invested` is
      not above zero, since a return on nothing, or on a withdrawal, is not a percentage.
    * `holdings` -- by value, largest first, then the unpriced ones; ties by asset.
    """

    total_value: Decimal
    invested: Decimal
    pnl: Decimal
    pnl_pct: Decimal | None
    holdings: tuple[HoldingSummary, ...]


def summarize(holdings: Iterable[PricedQuantity], invested: Decimal) -> PortfolioSummary:
    """Value each holding, total them, and set the total against what was invested.

    Raises:
        decimal.InvalidOperation: a value has more integer digits than `MONEY_PRECISION`
            leaves room for at `VALUE_SCALE` places -- `quantize`'s refusal, for a holding
            worth 10**20 units or more, which no portfolio is.
    """
    valued = [
        (
            holding,
            None
            if holding.price is None
            else quantize(multiply(holding.quantity, holding.price), VALUE_SCALE),
        )
        for holding in holdings
    ]
    total = _ZERO
    for _, value in valued:
        if value is not None:
            total = add(total, value)

    summaries = [
        HoldingSummary(
            asset=holding.asset,
            quantity=holding.quantity,
            price=holding.price,
            value=value,
            share_pct=None if value is None else _percent(value, total),
        )
        for holding, value in valued
    ]
    summaries.sort(key=_largest_first)
    pnl = subtract(total, invested)
    return PortfolioSummary(
        total_value=total,
        invested=invested,
        pnl=pnl,
        pnl_pct=_percent(pnl, invested),
        holdings=tuple(summaries),
    )


def _percent(part: Decimal, whole: Decimal) -> Decimal | None:
    """`part / whole x 100` at `RETURN_PCT_SCALE`, or `None` when `whole` is not above zero."""
    if whole <= 0:
        return None
    return divide(multiply(part, _HUNDRED), whole, RETURN_PCT_SCALE)


def _largest_first(holding: HoldingSummary) -> tuple[bool, Decimal, str]:
    """Valued before unpriced, then the larger value first, then by asset.

    Two `Decimal`s compared in Python, never in SQL. `copy_negate` rather than unary minus,
    which rounds to the ambient context's precision.
    """
    if holding.value is None:
        return (True, _ZERO, holding.asset)
    return (False, holding.value.copy_negate(), holding.asset)
