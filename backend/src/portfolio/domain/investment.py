"""What went in, what it is worth now, and whether the operations explain the coins held.

`summarize_investment(movements, holdings)` is the arithmetic behind `GET /api/investment`
(spec 042). **Pure**: the service reads the stored operations and the dashboard summary, and
hands this module plain values. Only the tracked assets in `holdings` are figured (R6): a
movement of any other coin is ignored, unless a tracked coin paid for it.

## The rulings, as arithmetic

* **R7. Invested.** A buy adds what it cost in USDT, its fee included when the fee was paid
  in the quote coin; a sell takes away what it brought in, net of such a fee. USDT, USDC and
  DAI count one for one. A buy or sell priced in anything else, or in nothing (a P2P purchase
  paid in pesos), makes the figure **unknown** -- never zero, never partial.
* **R8. Explained** = buys - sells + rewards - every fee charged in the coin. A trade that
  paid with the coin, or was paid in it, moves it too. Deposits, withdrawals and transfers
  move coins between places the owner controls, and change nothing here.
* **R9. Profit** = value now - invested, with the percentage over what was invested.
* **R10.** The cumulative invested per UTC day, known until the first unvalued trade.

Sums are `money.add` and `money.subtract`, which never round. The one rounding is the
percentage, to `RETURN_PCT_SCALE`. Nothing here is a `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.exchange_exports import STABLECOINS, OperationKind
from portfolio.domain.money import add, divide, multiply, subtract
from portfolio.domain.portfolio import RETURN_PCT_SCALE

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "AssetInvestment",
    "Holding",
    "InvestedOnDay",
    "Investment",
    "InvestmentUnavailable",
    "Movement",
    "TotalInvestment",
    "summarize_investment",
]

_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)
_TRADES: Final = frozenset({OperationKind.BUY, OperationKind.SELL})


class InvestmentUnavailable(StrEnum):
    """Why a profit, or its percentage, is not a number. The member is its own wire form.

    * `unvalued_trades` -- a trade was not priced in a stablecoin, so nothing invested is known.
    * `value_unknown` -- the coin has no value now: unpriced, or a wallet never read.
    * `nothing_invested` -- the profit is known, but there is nothing above zero to divide it
      by, so it has no percentage.
    """

    UNVALUED_TRADES = "unvalued_trades"
    VALUE_UNKNOWN = "value_unknown"
    NOTHING_INVESTED = "nothing_invested"


@dataclass(frozen=True, slots=True)
class Movement:
    """One stored operation, as the figures read it: the shape of `ParsedOperation` less
    where it came from."""

    executed_at: datetime
    kind: OperationKind
    asset: str
    quantity: Decimal
    quote_currency: str | None
    quote_amount: Decimal | None
    fee_asset: str | None
    fee_amount: Decimal | None


@dataclass(frozen=True, slots=True)
class Holding:
    """A tracked asset as the dashboard summary has it.

    `held` is `None` when a wallet holding it has never been read, since its quantity is then
    unknown; `value` is `None` when `held` is, or when nothing prices the coin.
    """

    asset: str
    held: Decimal | None
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class AssetInvestment:
    """One tracked asset's figures. `difference` is `held - explained`."""

    asset: str
    invested: Decimal | None
    value: Decimal | None
    pnl: Decimal | None
    pnl_pct: Decimal | None
    held: Decimal | None
    explained: Decimal
    difference: Decimal | None
    trades: int
    unvalued_trades: int
    unavailable: InvestmentUnavailable | None


@dataclass(frozen=True, slots=True)
class TotalInvestment:
    """The sum over the tracked assets, unknown where any of them is."""

    invested: Decimal | None
    value: Decimal | None
    pnl: Decimal | None
    pnl_pct: Decimal | None
    unavailable: InvestmentUnavailable | None


@dataclass(frozen=True, slots=True)
class InvestedOnDay:
    """The total invested at the end of a UTC day on which a trade changed it (R10)."""

    day: date
    invested: Decimal | None


@dataclass(frozen=True, slots=True)
class Investment:
    """Everything `GET /api/investment` serves. `assets` is by symbol."""

    assets: tuple[AssetInvestment, ...]
    total: TotalInvestment
    invested_by_day: tuple[InvestedOnDay, ...]


def summarize_investment(movements: Iterable[Movement], holdings: Sequence[Holding]) -> Investment:
    """Every tracked asset's figures, their total, and the invested series."""
    ordered = sorted(movements, key=lambda movement: movement.executed_at)
    assets = tuple(_asset(holding, ordered) for holding in sorted(holdings, key=lambda h: h.asset))
    tracked = frozenset(holding.asset for holding in holdings)
    return Investment(
        assets=assets,
        total=_total(assets),
        invested_by_day=_by_day(ordered, tracked),
    )


def _asset(holding: Holding, movements: Sequence[Movement]) -> AssetInvestment:
    asset = holding.asset
    explained = _ZERO
    invested: Decimal | None = _ZERO
    trades = 0
    unvalued = 0
    for movement in movements:
        explained = add(explained, _explained_change(movement, asset))
        if movement.kind not in _TRADES or asset not in {movement.asset, movement.quote_currency}:
            continue
        trades += 1
        cash = _cash(movement) if movement.asset == asset else None
        if cash is None:
            unvalued += 1
            invested = None
        elif invested is not None:
            invested = add(invested, cash)

    pnl, pnl_pct, unavailable = _profit(invested, holding.value)
    return AssetInvestment(
        asset=asset,
        invested=invested,
        value=holding.value,
        pnl=pnl,
        pnl_pct=pnl_pct,
        held=holding.held,
        explained=explained,
        difference=None if holding.held is None else subtract(holding.held, explained),
        trades=trades,
        unvalued_trades=unvalued,
        unavailable=unavailable,
    )


def _explained_change(movement: Movement, asset: str) -> Decimal:
    """What one movement did to `asset`'s explained quantity (R8)."""
    change = _ZERO
    if movement.asset == asset:
        if movement.kind in {OperationKind.BUY, OperationKind.REWARD}:
            change = movement.quantity
        elif movement.kind is OperationKind.SELL:
            change = movement.quantity.copy_negate()
    elif movement.quote_currency == asset and movement.quote_amount is not None:
        if movement.kind is OperationKind.BUY:
            change = movement.quote_amount.copy_negate()
        elif movement.kind is OperationKind.SELL:
            change = movement.quote_amount
    if movement.fee_asset == asset and movement.fee_amount is not None:
        change = subtract(change, movement.fee_amount)
    return change


def _cash(movement: Movement) -> Decimal | None:
    """The USDT a trade put in (positive) or took out (negative), or `None` when it was not
    priced in a stablecoin (R7)."""
    if movement.quote_currency not in STABLECOINS or movement.quote_amount is None:
        return None
    fee = _ZERO
    if movement.fee_asset == movement.quote_currency and movement.fee_amount is not None:
        fee = movement.fee_amount
    if movement.kind is OperationKind.BUY:
        return add(movement.quote_amount, fee)
    return subtract(fee, movement.quote_amount)


def _profit(
    invested: Decimal | None, value: Decimal | None
) -> tuple[Decimal | None, Decimal | None, InvestmentUnavailable | None]:
    """R9: the profit and its percentage, or why they are not numbers."""
    if invested is None:
        return None, None, InvestmentUnavailable.UNVALUED_TRADES
    if value is None:
        return None, None, InvestmentUnavailable.VALUE_UNKNOWN
    pnl = subtract(value, invested)
    if invested <= 0:
        return pnl, None, InvestmentUnavailable.NOTHING_INVESTED
    return pnl, divide(multiply(pnl, _HUNDRED), invested, RETURN_PCT_SCALE), None


def _total(assets: Sequence[AssetInvestment]) -> TotalInvestment:
    invested: Decimal | None = _ZERO
    value: Decimal | None = _ZERO
    for asset in assets:
        invested = _add_known(invested, asset.invested)
        value = _add_known(value, asset.value)
    pnl, pnl_pct, unavailable = _profit(invested, value)
    return TotalInvestment(
        invested=invested, value=value, pnl=pnl, pnl_pct=pnl_pct, unavailable=unavailable
    )


def _add_known(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    """A sum that is unknown when either side is."""
    if left is None or right is None:
        return None
    return add(left, right)


def _by_day(movements: Sequence[Movement], tracked: frozenset[str]) -> tuple[InvestedOnDay, ...]:
    """R10: one step per UTC day a trade of a tracked asset changed the total."""
    running: Decimal | None = _ZERO
    days: dict[date, Decimal | None] = {}
    for movement in movements:
        if movement.kind not in _TRADES:
            continue
        touched = {movement.asset, movement.quote_currency} & tracked
        if not touched:
            continue
        cash = _cash(movement) if movement.asset in tracked else None
        if running is not None:
            running = None if cash is None else add(running, cash)
        days[movement.executed_at.astimezone(UTC).date()] = running
    return tuple(InvestedOnDay(day, invested) for day, invested in days.items())
