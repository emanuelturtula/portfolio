"""What the portfolio is worth: the dashboard's total and every holding's share of it.

`summarize(holdings)` is the arithmetic behind `GET /api/portfolio/summary`. **Pure**: no I/O,
no clock, no ORM. The service reads the balances and the prices, and hands this module plain
values.

## Every figure is exact or rounded once

Sums are `money.add`, which never rounds. A value is `price x quantity` rounded once to
`VALUE_SCALE` by `value_of`, and a percentage is one `money.divide` to `RETURN_PCT_SCALE`.
Nothing here is a `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from portfolio.domain.money import add, divide, multiply, quantize

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "MAX_READING_AGE",
    "RETURN_PCT_SCALE",
    "VALUE_SCALE",
    "HoldingSummary",
    "PortfolioSummary",
    "PricedQuantity",
    "summarize",
    "value_of",
]

VALUE_SCALE: Final = 18
"""The places a market value is rounded to.

A price is stored at twelve places (`db.models.PRICE_SCALE`) and a quantity at up to eighteen,
so their product can carry thirty; this is the one rounding it gets. Eighteen leaves
`MONEY_PRECISION - 18` = 20 digits in front of the point.
"""

RETURN_PCT_SCALE: Final = 4
"""The places a percentage is rounded to: `71.4286`, a hundredth of a basis point."""

MAX_READING_AGE: Final = timedelta(hours=24)
"""How old a wallet's reading may be and still be current.

The balance sync runs on a timer of minutes, so a reading a day old means a wallet that has
stopped being read with nothing recorded against it -- a timer switched off, a balance run that
never finishes -- and its coins may have moved since. A day is far above any healthy interval,
so a reading is never called stale for being a few runs late.
"""

_ZERO: Final = Decimal((0, (0,), -VALUE_SCALE))
"""Zero at `VALUE_SCALE` places, so an empty total reads `0E-18` like every value beside it."""

_HUNDRED: Final = Decimal(100)


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
    * `holdings` -- by value, largest first, then the unpriced ones; ties by asset.
    """

    total_value: Decimal
    holdings: tuple[HoldingSummary, ...]


def value_of(quantity: Decimal, price: Decimal) -> Decimal:
    """`quantity x price`, rounded once to `VALUE_SCALE`.

    Raises:
        decimal.InvalidOperation: the value has more integer digits than `MONEY_PRECISION`
            leaves room for at `VALUE_SCALE` places -- `quantize`'s refusal, for a holding
            worth 10**20 units or more, which no portfolio is.
    """
    return quantize(multiply(quantity, price), VALUE_SCALE)


def summarize(holdings: Iterable[PricedQuantity]) -> PortfolioSummary:
    """Value each holding, total them, and give each its share of the total.

    Raises:
        decimal.InvalidOperation: as `value_of` does.
    """
    valued = [
        (holding, None if holding.price is None else value_of(holding.quantity, holding.price))
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
    return PortfolioSummary(total_value=total, holdings=tuple(summaries))


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
