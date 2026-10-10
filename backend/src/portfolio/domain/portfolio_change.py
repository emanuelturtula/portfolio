"""How much the portfolio moved over 24 hours and 7 days: the arithmetic behind the widget.

Spec 041's rules R3 to R6. **Pure**: no I/O, no clock, no ORM. The service reads each
wallet's snapshot at the instant, its rebuilt days and the hourly prices, and hands this
module plain values; "now" and the instant are arguments.

## A change nothing can value is unavailable, never zero (R4, R5, R6)

The value then is unknown when a wallet's quantity at the instant is unknown, or when a
wallet held something with no price at the instant. The value now is unknown when the
summary could not value everything. Either way the change is `None`, with the reason beside
it: a widget showing `+0.00` on the day a price was missing would be believed.

## Every figure is exact or rounded once

A holding's value is `domain.portfolio.value_of`, rounded once to `VALUE_SCALE`, and a total
is `money.add`, which never rounds. The change is an exact subtraction; only its percentage
is rounded, once. Nothing here is a `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.money import add, divide, multiply, subtract
from portfolio.domain.portfolio import RETURN_PCT_SCALE, VALUE_SCALE, value_of

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from portfolio.domain.portfolio_history import DailyReading

__all__ = [
    "PERIODS",
    "Change",
    "ChangePeriod",
    "Unavailable",
    "WalletThen",
    "change_over",
    "quantity_at",
    "value_at",
]

_ZERO: Final = Decimal((0, (0,), -VALUE_SCALE))
"""Zero at `VALUE_SCALE` places, so a total of holdings worth nothing reads like any other."""

_HUNDRED: Final = Decimal(100)


class ChangePeriod(StrEnum):
    """How far back a change looks (R8). The member is its wire form."""

    DAY = "24h"
    WEEK = "7d"


PERIODS: Final[Mapping[ChangePeriod, timedelta]] = {
    ChangePeriod.DAY: timedelta(hours=24),
    ChangePeriod.WEEK: timedelta(days=7),
}
"""Each period's length, in the order the widget shows them."""


class Unavailable(StrEnum):
    """Why a change could not be worked out. The member is its wire form.

    * `value_unknown_now` -- the summary could not value everything held now (R5).
    * `no_reading_then` -- a wallet's quantity at the instant is unknown (R3).
    * `no_price_then` -- a wallet held something with no price at the instant (R2).
    """

    VALUE_UNKNOWN_NOW = "value_unknown_now"
    NO_READING_THEN = "no_reading_then"
    NO_PRICE_THEN = "no_price_then"


@dataclass(frozen=True, slots=True)
class WalletThen:
    """One wallet at the instant: its asset, and what it held then, `None` when unknown."""

    asset: str
    quantity: Decimal | None


@dataclass(frozen=True, slots=True)
class Change:
    """The change over one period, or why there is none.

    `change` and `change_pct` are `None` exactly when `unavailable` is set, and `change_pct`
    is also `None` when nothing was held at `since`. `value_then` is set whenever it is
    known, even when the value now is not.
    """

    period: ChangePeriod
    since: datetime
    value_then: Decimal | None
    change: Decimal | None
    change_pct: Decimal | None
    unavailable: Unavailable | None


def quantity_at(
    at: datetime,
    snapshot: Decimal | None,
    rebuilt: Sequence[DailyReading],
) -> Decimal | None:
    """What one wallet held at the instant `at`, by R3; `None` when it is unknown.

    `snapshot` is the quantity of its latest snapshot at or before `at`, and wins. Without
    one, `rebuilt` (oldest first, one per day) gives the balance at the end of the day before
    `at`'s UTC day; a rebuilt history starts from zero, so with no rebuilt day that early the
    wallet held nothing yet.
    """
    if snapshot is not None:
        return snapshot
    if not rebuilt:
        return None
    day_before = at.astimezone(UTC).date() - timedelta(days=1)
    held = Decimal(0)
    for reading in rebuilt:
        if reading.day > day_before:
            break
        held = reading.quantity
    return held


def value_at(wallets: Sequence[WalletThen], prices: Mapping[str, Decimal]) -> Decimal | Unavailable:
    """What these wallets were worth together at an instant, by R4, or why it is unknown.

    `prices` maps an asset to its price at the instant. A wallet holding nothing needs no
    price; an unknown wallet comes first among the reasons, because a price could not help.
    """
    if any(wallet.quantity is None for wallet in wallets):
        return Unavailable.NO_READING_THEN
    total = _ZERO
    for wallet in wallets:
        quantity = wallet.quantity
        if quantity is None or quantity == 0:
            continue
        price = prices.get(wallet.asset)
        if price is None:
            return Unavailable.NO_PRICE_THEN
        total = add(total, value_of(quantity, price))
    return total


def change_over(
    period: ChangePeriod,
    *,
    since: datetime,
    now: Decimal | None,
    then: Decimal | Unavailable,
) -> Change:
    """The change from `then` to `now`, by R6: exact, with its percentage of `then`.

    `now` is `None` when the value now is unknown (R5). The percentage is rounded half-even
    to `RETURN_PCT_SCALE` places, and is `None` when `then` is not above zero.
    """
    value_then = None if isinstance(then, Unavailable) else then
    if isinstance(then, Unavailable) or now is None:
        reason = then if isinstance(then, Unavailable) else Unavailable.VALUE_UNKNOWN_NOW
        return Change(
            period=period,
            since=since,
            value_then=value_then,
            change=None,
            change_pct=None,
            unavailable=reason,
        )
    change = subtract(now, then)
    return Change(
        period=period,
        since=since,
        value_then=then,
        change=change,
        change_pct=(
            divide(multiply(change, _HUNDRED), then, RETURN_PCT_SCALE) if then > 0 else None
        ),
        unavailable=None,
    )
