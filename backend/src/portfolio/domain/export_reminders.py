"""Which months still owe the owner's manual exchange exports (spec 040).

Once a month the owner downloads each exchange's transaction export by hand and files it.
The dashboard reminds them from the first day after a month ends until they mark that month
done. This module decides which months are owed. It reads no clock and no database: "now"
and the months already marked done are arguments.

**A month is a calendar month in Argentina time**, the owner's: UTC-3 all year, since
Argentina has kept no daylight saving time since 2009. The offset is a constant rather than
a `zoneinfo` lookup because `domain` may not read the host's time-zone database. A month
ends at midnight on the 1st, Argentina time, which is 03:00 UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Collection

__all__ = [
    "EXPORT_EXCHANGES",
    "FIRST_REMINDED_MONTH",
    "OWNER_TIMEZONE",
    "MonthNotClosedError",
    "MonthNotRemindedError",
    "closed_months",
    "is_month_start",
    "parse_month",
    "pending_months",
    "require_closed",
]

OWNER_TIMEZONE: Final = timezone(timedelta(hours=-3), "ART")
"""Argentina time, which decides when a month has ended."""

FIRST_REMINDED_MONTH: Final = date(2026, 9, 1)
"""The oldest month the reminder asks about. Earlier months are not owed."""

EXPORT_EXCHANGES: Final = ("Binance", "Bitget", "BingX", "Nexo")
"""The exchanges whose exports the reminder names, in the order it names them."""


class MonthNotClosedError(ValueError):
    """The month has not ended yet in Argentina time, so its exports cannot be done."""


class MonthNotRemindedError(ValueError):
    """The month is older than `FIRST_REMINDED_MONTH`, so nothing about it is owed."""


def is_month_start(value: date) -> bool:
    """Whether `value` is the first day of a month, the form a month is stored in."""
    return value.day == 1


def parse_month(text: str) -> date:
    """`YYYY-MM` as the first day of that month.

    Raises:
        ValueError: `text` is not exactly a four-digit year, a hyphen and a two-digit month.
    """
    if len(text) != len("YYYY-MM") or text[4] != "-":
        message = "a month is written YYYY-MM"
        raise ValueError(message)
    year, month = text[:4], text[5:]
    if not (year.isascii() and year.isdigit() and month.isascii() and month.isdigit()):
        message = "a month is written YYYY-MM"
        raise ValueError(message)
    return date(int(year), int(month), 1)


def _next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def closed_months(now: datetime, first: date = FIRST_REMINDED_MONTH) -> list[date]:
    """Every month from `first` that has ended by `now` in Argentina time, oldest first.

    Raises:
        ValueError: `now` is naive, or `first` is not the first day of a month.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        message = "now must be timezone-aware"
        raise ValueError(message)
    if not is_month_start(first):
        message = "first must be the first day of a month"
        raise ValueError(message)
    local = now.astimezone(OWNER_TIMEZONE)
    current = date(local.year, local.month, 1)
    months: list[date] = []
    month = first
    while month < current:
        months.append(month)
        month = _next_month(month)
    return months


def pending_months(
    now: datetime, done: Collection[date], first: date = FIRST_REMINDED_MONTH
) -> list[date]:
    """The closed months not marked done, oldest first."""
    return [month for month in closed_months(now, first) if month not in done]


def require_closed(month: date, now: datetime, first: date = FIRST_REMINDED_MONTH) -> None:
    """Refuse a month whose exports cannot be marked done.

    Raises:
        MonthNotRemindedError: `month` is before `first`.
        MonthNotClosedError: `month` has not ended by `now` in Argentina time.
    """
    if month < first:
        message = f"the reminder starts at {first:%Y-%m}"
        raise MonthNotRemindedError(message)
    if month not in closed_months(now, first):
        message = f"{month:%Y-%m} has not ended yet"
        raise MonthNotClosedError(message)
