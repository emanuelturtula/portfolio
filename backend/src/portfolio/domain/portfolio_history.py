"""What the wallets were worth on each day: the arithmetic behind the value-history chart.

`portfolio_days` and `wallet_days` are spec 037's rules R3 to R6. **Pure**: no I/O, no clock,
no ORM. The service reads the closing snapshots and the daily prices and hands this module
plain values; "today" is an argument.

## A day's balance is its closing balance (R3)

Each wallet contributes the quantity of its last reading on or before the day, carried
forward over the days it was not read. A wallet with no reading yet adds nothing: nothing is
known about it on that day.

## A gap is `None`, never zero (R4)

A day is `None` when no wallet had been read by its end, or when a wallet holding a non-zero
quantity that day has no price for its asset on that day. A partial sum would be believed as
the portfolio's value; a gap on the chart is not. A wallet holding nothing needs no price.

## Every figure is exact or rounded once

A holding's value is `domain.portfolio.value_of`, rounded once to `VALUE_SCALE`, and a total
is `money.add` over those, which never rounds. Nothing here is a `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.money import add
from portfolio.domain.portfolio import VALUE_SCALE, value_of

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from datetime import date

__all__ = [
    "DailyReading",
    "DayValue",
    "HistoryRange",
    "WalletDay",
    "WalletReadings",
    "asset_days",
    "days_of",
    "portfolio_days",
    "wallet_days",
]

_ZERO: Final = Decimal((0, (0,), -VALUE_SCALE))
"""Zero at `VALUE_SCALE` places, so a total of holdings worth nothing reads like any other."""


class HistoryRange(StrEnum):
    """How far back a history reaches (R6). The member is its wire form.

    `all` starts on the first day any wallet in the history has a reading.
    """

    DAYS_30 = "30d"
    DAYS_90 = "90d"
    YEAR = "1y"
    ALL = "all"


_LENGTHS: Final[Mapping[HistoryRange, int]] = {
    HistoryRange.DAYS_30: 30,
    HistoryRange.DAYS_90: 90,
    HistoryRange.YEAR: 365,
}


@dataclass(frozen=True, slots=True)
class DailyReading:
    """A wallet's quantity at the end of `day`: its last reading observed that day."""

    day: date
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class WalletReadings:
    """One wallet's asset and its daily closing readings, oldest first, one per day at most."""

    asset: str
    readings: Sequence[DailyReading]


@dataclass(frozen=True, slots=True)
class DayValue:
    """The portfolio's value at the end of `day`, or `None` when it cannot be known (R4)."""

    day: date
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class WalletDay:
    """One wallet at the end of `day`: what it held, and what that was worth.

    `quantity` is `None` before the wallet's first reading. `value` is `None` then too, and
    when the wallet held something that has no price that day.
    """

    day: date
    quantity: Decimal | None
    value: Decimal | None


def days_of(
    history_range: HistoryRange, *, today: date, first_reading: date | None
) -> tuple[date, ...]:
    """Every day of the range, oldest first, ending `today` (R6).

    A fixed range is that many days ending today, whatever the readings. `all` starts on
    `first_reading`, or is today alone when nothing has been read; a first reading dated
    after today (a clock stepped back) is today alone as well.
    """
    if history_range is HistoryRange.ALL:
        first = today if first_reading is None or first_reading > today else first_reading
    else:
        first = today - timedelta(days=_LENGTHS[history_range] - 1)
    return tuple(first + timedelta(days=offset) for offset in range((today - first).days + 1))


def portfolio_days(
    days: Sequence[date],
    wallets: Sequence[WalletReadings],
    prices: Mapping[str, Mapping[date, Decimal]],
) -> tuple[DayValue, ...]:
    """The value of every wallet together at the end of each day, by R3 and R4.

    `days` is ascending; `prices` maps an asset to its price on each day it has one.
    """
    per_wallet = [_closing_quantities(days, wallet.readings) for wallet in wallets]
    points: list[DayValue] = []
    for index, day in enumerate(days):
        total = _ZERO
        known = False
        priced = True
        for wallet, quantities in zip(wallets, per_wallet, strict=True):
            quantity = quantities[index]
            if quantity is None:
                continue
            known = True
            value = _value(quantity, prices.get(wallet.asset, {}).get(day))
            if value is None:
                priced = False
            else:
                total = add(total, value)
        points.append(DayValue(day=day, value=total if known and priced else None))
    return tuple(points)


def asset_days(
    days: Sequence[date],
    wallets: Sequence[WalletReadings],
    prices: Mapping[str, Mapping[date, Decimal]],
) -> dict[str, tuple[DayValue, ...]]:
    """Each asset's value at the end of each day, keyed by asset (spec 041, R7).

    An asset's value is `portfolio_days` over its own wallets alone, so R3 and R4 hold for it
    as they hold for the total: `None` before any of its wallets was read, and `None` on a
    day one of them held something with no price.
    """
    by_asset: dict[str, list[WalletReadings]] = {}
    for wallet in wallets:
        by_asset.setdefault(wallet.asset, []).append(wallet)
    return {asset: portfolio_days(days, by_asset[asset], prices) for asset in sorted(by_asset)}


def wallet_days(
    days: Sequence[date],
    wallet: WalletReadings,
    prices: Mapping[date, Decimal],
) -> tuple[WalletDay, ...]:
    """One wallet's quantity and value at the end of each day, by R3 and R4."""
    return tuple(
        WalletDay(
            day=day,
            quantity=quantity,
            value=None if quantity is None else _value(quantity, prices.get(day)),
        )
        for day, quantity in zip(days, _closing_quantities(days, wallet.readings), strict=True)
    )


def _value(quantity: Decimal, price: Decimal | None) -> Decimal | None:
    """A holding's value, zero for nothing held whatever the price, `None` when unpriced."""
    if quantity == 0:
        return _ZERO
    if price is None:
        return None
    return value_of(quantity, price)


def _closing_quantities(
    days: Sequence[date], readings: Sequence[DailyReading]
) -> list[Decimal | None]:
    """Each day's quantity: the last reading on or before it, `None` before the first."""
    quantities: list[Decimal | None] = []
    pending: Iterator[DailyReading] = iter(readings)
    upcoming = next(pending, None)
    current: Decimal | None = None
    for day in days:
        while upcoming is not None and upcoming.day <= day:
            current = upcoming.quantity
            upcoming = next(pending, None)
        quantities.append(current)
    return quantities
