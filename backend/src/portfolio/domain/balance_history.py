"""A wallet's past closing balances, rebuilt backwards from its transactions (spec 038).

`rebuild_daily` is rulings R3 and R4. **Pure**: no I/O, no clock, no ORM. The service reads
each address's history from the chain and hands this module the effects, the balance they
were checked against, and "today".

## Backwards from the balance, forwards in the result

A day's closing balance is the current balance minus every effect dated after that day. The
walk goes newest first, which is why it is anchored on a number the chain reported rather than
on an assumed zero at the start: an anchor at the start would make any missing transaction
look like a balance that was simply different, while an anchor at the end makes it a walk that
does not reach zero.

## A rebuild proves itself or is refused

The walk must never go below zero, and must reach exactly zero before the first transaction.
Either failure means the history is not the whole history, and `rebuild_daily` says so instead
of returning balances nobody could trust. Base units are integers, so every step is exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date, datetime

__all__ = ["DailyBalance", "Effect", "RebuildRefused", "RebuiltHistory", "rebuild_daily"]


@dataclass(frozen=True, slots=True)
class Effect:
    """One confirmed transaction's net effect on the wallet, in base units, and when."""

    occurred_at: datetime
    delta: int


@dataclass(frozen=True, slots=True)
class DailyBalance:
    """The wallet's balance at the end of `day`, in base units."""

    day: date
    confirmed: int


class RebuildRefused(StrEnum):
    """Why a walk back from the balance is not a history (R4). The member is its wire form.

    * `goes_negative` -- some day's balance would be below zero.
    * `does_not_reach_zero` -- the balance before the first transaction is not zero.
    """

    GOES_NEGATIVE = "goes_negative"
    DOES_NOT_REACH_ZERO = "does_not_reach_zero"


@dataclass(frozen=True, slots=True)
class RebuiltHistory:
    """The closing balance of every day from the first transaction to `today`, or a refusal.

    `days` is empty exactly when `refused` is set, and when there was no transaction at all.
    """

    days: tuple[DailyBalance, ...]
    refused: RebuildRefused | None


def rebuild_daily(effects: Iterable[Effect], *, balance: int, today: date) -> RebuiltHistory:
    """Every day's closing balance, oldest first, from the first effect's day to `today`.

    `balance` is the confirmed balance the effects were checked against; it is the closing
    balance of `today`. An effect dated after `today` (a clock behind the chain's) is applied
    to `today` itself rather than lost. A day is the UTC date of `occurred_at` (R3).
    """
    by_day: dict[date, int] = {}
    for effect in effects:
        day = min(effect.occurred_at.astimezone(UTC).date(), today)
        by_day[day] = by_day.get(day, 0) + effect.delta
    if not by_day:
        if balance != 0:
            return RebuiltHistory(days=(), refused=RebuildRefused.DOES_NOT_REACH_ZERO)
        return RebuiltHistory(days=(), refused=None)

    first = min(by_day)
    closing = balance
    days: list[DailyBalance] = []
    day = today
    while day >= first:
        if closing < 0:
            return RebuiltHistory(days=(), refused=RebuildRefused.GOES_NEGATIVE)
        days.append(DailyBalance(day=day, confirmed=closing))
        closing -= by_day.get(day, 0)
        day -= timedelta(days=1)
    if closing != 0:
        return RebuiltHistory(days=(), refused=RebuildRefused.DOES_NOT_REACH_ZERO)
    return RebuiltHistory(days=tuple(reversed(days)), refused=None)
