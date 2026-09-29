"""Valuation: a position and a current price in, market value and unrealized P&L out. Pure.

The arithmetic lives here rather than in the service that looks the price up so that it is
held to the same rules as the engine: no bare `+`, `-`, `*` or `/` on a `Decimal`, the result
independent of the calling thread's decimal context, and the `domain` coverage floor. The
service decides *which* price; this module decides what it means for the position.

## What each figure covers, and why they differ (spec 021, *Valuation*)

With `Qk` the known-cost quantity, `C` its basis and `P` the price:

* **`market_value` is `P x quantity`, over every unit held**, known cost or not. It answers
  "what is this holding worth", and a unit whose cost is unknown is worth exactly as much as
  one whose cost is known.
* **`unrealized_pnl` is `P x Qk - C`, over the known part only.** A unit of unknown cost has
  nothing to compare its value against, and counting its whole value as gain is the
  fictitious profit `Position.unknown_basis_quantity` exists to prevent.
* **`unrealized_return_pct` is `unrealized_pnl / C x 100`**, and there is none when `C` is
  zero or negative. A negative basis is reachable (spec 019, R7), and a percentage of it
  would read as a return with its sign reversed.

Each product is rounded once, half to even, to `VALUE_SCALE` places, and the percentage once,
to `RETURN_PCT_SCALE` places. Every other operation is `money`'s exact `add` or `subtract`.

## A missing price is a reason, never a zero

`value_position` takes either a price or the reason there is none, and refuses both or
neither. The reason travels to `market_value_unavailable_reason`, which is the rule
`services/prices.py` applies one layer up: *a portfolio silently showing 0 is worse than one
showing an error, because it is believed.* The one exception is a position holding nothing,
which is worth exactly zero whatever the price is -- a statement about the quantity, not a
guess about the price.

**A value that cannot be represented is a reason too** (spec 021, R6). A price times a quantity
of 10**20 cash units or more has no room at `VALUE_SCALE` places within `MONEY_PRECISION`, and
rather than raise out of a read it reports `market_value` and `unrealized_pnl` as `None` with
`ValueUnavailable.VALUE_OUT_OF_RANGE` -- the rule spec 019's R1 set for the average cost and
R4 for the percentage. No real price reaches it.

## The totals cover only what can be compared

`value_portfolio` sums the invested amount, the market value and the unrealized P&L over the
positions that are **fully comparable**: valued (or holding nothing) and with no unknown-cost
units. A total that mixed in a value with no cost, or a cost with no value, would make the
percentage return a fiction. The positions left out are named in `excluded`, each with its
reason, which is what the dashboard shows beside the total (#20). Realized P&L is summed over
every position, because it does not depend on any current price.

**A stale price is used as it is.** Staleness is shown beside the price, never used to hide
one (spec 021).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.accounting.results import Position, PositionFlag
from portfolio.domain.money import add, divide, multiply, quantize, subtract

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "RETURN_PCT_SCALE",
    "VALUE_SCALE",
    "Exclusion",
    "ExclusionReason",
    "PortfolioTotals",
    "PositionValue",
    "ValueUnavailable",
    "value_portfolio",
    "value_position",
]

VALUE_SCALE: Final = 18
"""The places a market value, and the valued part of an unrealized P&L, is rounded to.

Eighteen, the scale of every amount the engine returns, so that `P x Qk - C` subtracts two
figures of one scale and the totals add figures of one scale. The price itself is stored at
twelve places (`db.models.PRICE_SCALE`) and a quantity at eighteen, so their product can
carry thirty; this is the one rounding it gets.
"""

RETURN_PCT_SCALE: Final = 4
"""The places a percentage return is rounded to: `71.4286`, a hundredth of a basis point."""

_HUNDRED: Final = Decimal(100)

_ZERO: Final = Decimal((0, (0,), -VALUE_SCALE))
"""Zero at `VALUE_SCALE` places, so a position holding nothing reports `0E-18` like any other
amount, and an empty portfolio's totals have the same shape as a full one's."""


class ValueUnavailable(StrEnum):
    """Why a position has no market value although it has a price. The member is its wire form.

    The price reasons -- `never_fetched`, `unsupported_pair` and the rest -- belong to
    `services.prices.PriceUnavailable`, which `domain` may not import; a caller passes one in as
    its string. This is the one reason the valuation itself can produce.

    * `VALUE_OUT_OF_RANGE` -- the price times the quantity is 10**20 cash units or more, which
      no figure here can hold (spec 021, R6).
    """

    VALUE_OUT_OF_RANGE = "value_out_of_range"


class ExclusionReason(StrEnum):
    """Why a position was left out of the portfolio totals. The member is its wire form.

    * `UNKNOWN_BASIS` -- some of its units have no known cost, so its value and its cost
      describe different quantities.
    * `UNPRICED` -- it holds something and has no market value: there is no price for it, or
      the value cannot be represented (spec 021, R6). It has a cost and no value.

    **A position that is both is reported once, as `UNKNOWN_BASIS`** (spec 021, R4): the
    check runs in the order the members are declared, and one entry per position keeps the
    list a list of positions rather than of problems.
    """

    UNKNOWN_BASIS = "unknown_basis"
    UNPRICED = "unpriced"


@dataclass(frozen=True, slots=True)
class Exclusion:
    """One position left out of the totals, and why."""

    asset: str
    reason: ExclusionReason


@dataclass(frozen=True, slots=True)
class PositionValue:
    """One position, the price it was valued at, and what the two give.

    * `price` -- the price used, or `None` when there was none.
    * `market_value` -- `price x quantity`; `None` when there is no price and something is
      held, zero when nothing is held.
    * `market_value_unavailable_reason` -- whenever `market_value` is `None`, why: the price's
      reason, or `value_out_of_range` when the product cannot be represented (spec 021, R6).
      `None` otherwise. Never one without the other.
    * `unrealized_pnl` -- `price x known quantity - cost_basis`; `None` when there is no price
      and a known-cost quantity is held, zero when none is; and `None` whenever the market value
      is out of range.
    * `unrealized_return_pct` -- `unrealized_pnl / cost_basis x 100` at four places; `None`
      when there is no P&L, when the basis is zero or negative, or when the quotient cannot
      be represented at all (spec 021, R4), which is the rule spec 019's R1 set for the
      average cost: a display figure is not a reason for a read to fail.
    """

    position: Position
    price: Decimal | None
    market_value: Decimal | None
    market_value_unavailable_reason: str | None
    unrealized_pnl: Decimal | None
    unrealized_return_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class PortfolioTotals:
    """What the comparable positions add up to, and which positions are not in it.

    `total_invested`, `market_value`, `unrealized_pnl` and `unrealized_return_pct` cover the
    same positions, the ones not in `excluded`, so the percentage is the return on exactly the
    money in the total beside it. `realized_pnl` covers every position. `unrealized_return_pct`
    is `None` when the comparable positions hold no positive basis.
    """

    total_invested: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    unrealized_return_pct: Decimal | None
    realized_pnl: Decimal
    excluded: tuple[Exclusion, ...]


def value_position(
    position: Position,
    price: Decimal | None,
    price_reason: str | None,
) -> PositionValue:
    """Value one position at `price`, or record why it could not be valued.

    Args:
        position: a position as `replay` returned it, or as the snapshot stored it.
        price: the asset's current price in the unit of account, used as it is, stale or
            not; or `None` when there is none.
        price_reason: why there is no price, as the reason's wire form
            (`services.prices.PriceUnavailable`, which `domain` may not import); `None`
            exactly when a price is given.

    Raises:
        ValueError: a price and a reason were both given, or neither was.
        TypeError: the price is not a `Decimal`.
    """
    if (price is None) == (price_reason is None):
        message = "value_position takes either a price or the reason there is none, not both"
        raise ValueError(message)
    try:
        market_value = _market_value(position.quantity, price)
    except InvalidOperation:
        return PositionValue(
            position=position,
            price=price,
            market_value=None,
            market_value_unavailable_reason=ValueUnavailable.VALUE_OUT_OF_RANGE.value,
            unrealized_pnl=None,
            unrealized_return_pct=None,
        )
    unrealized_pnl = _unrealized_pnl(position.known_quantity, position.cost_basis, price)
    return PositionValue(
        position=position,
        price=price,
        market_value=market_value,
        market_value_unavailable_reason=price_reason if market_value is None else None,
        unrealized_pnl=unrealized_pnl,
        unrealized_return_pct=_return_pct(unrealized_pnl, position.cost_basis),
    )


def value_portfolio(values: Iterable[PositionValue]) -> PortfolioTotals:
    """Sum the comparable positions, and name the ones left out, in the order given.

    Every sum is `money.add`, which is exact. Over no positions, or over none comparable,
    each total is zero and the percentage is `None`.
    """
    total_invested = _ZERO
    market_value = _ZERO
    unrealized_pnl = _ZERO
    realized_pnl = _ZERO
    excluded: list[Exclusion] = []
    for value in values:
        position = value.position
        realized_pnl = add(realized_pnl, position.realized_pnl)
        if PositionFlag.UNKNOWN_BASIS in position.flags:
            excluded.append(Exclusion(position.asset, ExclusionReason.UNKNOWN_BASIS))
        elif value.market_value is None or value.unrealized_pnl is None:
            excluded.append(Exclusion(position.asset, ExclusionReason.UNPRICED))
        else:
            total_invested = add(total_invested, position.cost_basis)
            market_value = add(market_value, value.market_value)
            unrealized_pnl = add(unrealized_pnl, value.unrealized_pnl)
    return PortfolioTotals(
        total_invested=total_invested,
        market_value=market_value,
        unrealized_pnl=unrealized_pnl,
        unrealized_return_pct=_return_pct(unrealized_pnl, total_invested),
        realized_pnl=realized_pnl,
        excluded=tuple(excluded),
    )


def _market_value(quantity: Decimal, price: Decimal | None) -> Decimal | None:
    """`price x quantity` at `VALUE_SCALE`; zero when nothing is held; `None` when unpriced.

    Raises:
        decimal.InvalidOperation: the product is 10**20 or more, past what `VALUE_SCALE`
            places leave room for; `value_position` reports it as `value_out_of_range`.
    """
    if quantity.is_zero():
        return _ZERO
    if price is None:
        return None
    return quantize(multiply(quantity, price), VALUE_SCALE)


def _unrealized_pnl(known: Decimal, basis: Decimal, price: Decimal | None) -> Decimal | None:
    """`price x known - basis`; zero when no known-cost quantity is held; `None` when unpriced.

    Zero rather than `-basis` when `known` is zero, because the engine guarantees the basis is
    then exactly zero too (spec 019, I4) -- so zero is the same answer, reached without a price.

    The `quantize` cannot raise: it is called only after `_market_value` fitted the product of
    the same price and the whole quantity, and `known` is part of that quantity.
    """
    if known.is_zero():
        return _ZERO
    if price is None:
        return None
    return subtract(quantize(multiply(known, price), VALUE_SCALE), basis)


def _return_pct(pnl: Decimal | None, basis: Decimal) -> Decimal | None:
    """`pnl / basis x 100` at `RETURN_PCT_SCALE`, or `None` where there is no such figure."""
    if pnl is None or basis <= 0:
        return None
    try:
        return divide(multiply(pnl, _HUNDRED), basis, RETURN_PCT_SCALE)
    except InvalidOperation:
        return None
