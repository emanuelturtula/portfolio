"""Shared by the accounting tests: engine builders, and one rendering for engine and oracle.

The rendering is the oracle's `result_to_json`, applied to the engine's result through
`engine_to_json`. Both sides therefore compare as the same plain structure, and a failing
assertion prints a readable diff of positions, warnings and lots instead of two object
reprs. Every engine amount goes through `oracle.render`, which **refuses** a value off the
18-place grid rather than rounding it, so an engine amount with a 19th digit fails loudly
here instead of being quietly normalised into agreement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from portfolio.domain.accounting import (
    AccountingConfig,
    Adjustment,
    EventKey,
    NegativeInventory,
    Trade,
    Transfer,
    UnattributedFee,
)
from portfolio.domain.exchanges import FillSide
from tests.domain.accounting import oracle

if TYPE_CHECKING:
    from collections.abc import Iterable

    from portfolio.domain.accounting import AccountingResult, Position
    from portfolio.domain.accounting.events import AccountingEvent

DAY: Final = datetime(2026, 1, 1, tzinfo=UTC)
CONFIG: Final = AccountingConfig(frozenset({"USDC", "USDT"}))
CASH: Final = CONFIG.cash_assets


# --------------------------------------------------------------------------------------
# Building engine events tersely
# --------------------------------------------------------------------------------------


def at(hour: int, minute: int = 0, microsecond: int = 0) -> datetime:
    """A moment on 2026-01-01, UTC -- the day `docs/accounting.md` uses."""
    return DAY.replace(hour=hour, minute=minute, microsecond=microsecond)


def key(moment: datetime | int, external_id: str, source: str = "bitget") -> EventKey:
    when = at(moment) if isinstance(moment, int) else moment
    return EventKey(when, source, external_id)


def trade(
    event_key: EventKey,
    side: FillSide,
    base: str,
    quote: str,
    quantity: str,
    quote_quantity: str,
    fee: str = "0",
    fee_asset: str | None = None,
) -> Trade:
    return Trade(
        key=event_key,
        base_asset=base,
        quote_asset=quote,
        side=side,
        quantity=Decimal(quantity),
        quote_quantity=Decimal(quote_quantity),
        fee_amount=Decimal(fee),
        fee_asset=fee_asset,
    )


def buy(
    event_key: EventKey,
    base: str,
    quote: str,
    quantity: str,
    quote_quantity: str,
    fee: str = "0",
    fee_asset: str | None = None,
) -> Trade:
    """Buy `quantity` of `base`, paying `quote_quantity` of `quote`."""
    return trade(event_key, FillSide.BUY, base, quote, quantity, quote_quantity, fee, fee_asset)


def sell(
    event_key: EventKey,
    base: str,
    quote: str,
    quantity: str,
    quote_quantity: str,
    fee: str = "0",
    fee_asset: str | None = None,
) -> Trade:
    """Sell `quantity` of `base`, receiving `quote_quantity` of `quote`."""
    return trade(event_key, FillSide.SELL, base, quote, quantity, quote_quantity, fee, fee_asset)


def adjust(event_key: EventKey, asset: str, quantity: str, unit_cost: str | None) -> Adjustment:
    return Adjustment(
        key=event_key,
        asset=asset,
        quantity=Decimal(quantity),
        unit_cost=None if unit_cost is None else Decimal(unit_cost),
    )


def move(
    event_key: EventKey,
    asset: str,
    quantity: str,
    from_location: str = "bitget",
    to_location: str = "cold-storage",
) -> Transfer:
    return Transfer(
        key=event_key,
        asset=asset,
        quantity=Decimal(quantity),
        from_location=from_location,
        to_location=to_location,
    )


def position(result: AccountingResult, asset: str) -> Position:
    """The one position for `asset`, asserted to exist exactly once."""
    matches = [candidate for candidate in result.positions if candidate.asset == asset]
    assert len(matches) == 1, f"{asset}: {len(matches)} positions in {result.positions!r}"
    return matches[0]


def flag_names(found: Position) -> set[str]:
    return {flag.name for flag in found.flags}


# --------------------------------------------------------------------------------------
# Engine <-> oracle
# --------------------------------------------------------------------------------------


def to_oracle_key(event_key: EventKey) -> oracle.Key:
    return oracle.Key(event_key.occurred_at, event_key.source, event_key.external_id)


def to_oracle(event: AccountingEvent) -> oracle.Event:
    """The same event in the oracle's vocabulary. Exact: `Fraction(Decimal)` cannot round."""
    oracle_key = to_oracle_key(event.key)
    if isinstance(event, Trade):
        return oracle.Trade(
            key=oracle_key,
            base_asset=event.base_asset,
            quote_asset=event.quote_asset,
            side="buy" if event.side is FillSide.BUY else "sell",
            quantity=Fraction(event.quantity),
            quote_quantity=Fraction(event.quote_quantity),
            fee_amount=Fraction(event.fee_amount),
            fee_asset=event.fee_asset,
        )
    if isinstance(event, Adjustment):
        return oracle.Adjustment(
            key=oracle_key,
            asset=event.asset,
            quantity=Fraction(event.quantity),
            unit_cost=None if event.unit_cost is None else Fraction(event.unit_cost),
        )
    return oracle.Transfer(
        key=oracle_key,
        asset=event.asset,
        quantity=Fraction(event.quantity),
        from_location=event.from_location,
        to_location=event.to_location,
    )


def to_engine(event: oracle.Event) -> AccountingEvent:
    """An oracle event as the engine's, for the golden scenario, which is oracle-native."""
    engine_key = EventKey(event.key.occurred_at, event.key.source, event.key.external_id)
    if isinstance(event, oracle.Trade):
        return Trade(
            key=engine_key,
            base_asset=event.base_asset,
            quote_asset=event.quote_asset,
            side=FillSide.BUY if event.side == "buy" else FillSide.SELL,
            quantity=oracle.to_decimal(event.quantity),
            quote_quantity=oracle.to_decimal(event.quote_quantity),
            fee_amount=oracle.to_decimal(event.fee_amount),
            fee_asset=event.fee_asset,
        )
    if isinstance(event, oracle.Adjustment):
        return Adjustment(
            key=engine_key,
            asset=event.asset,
            quantity=oracle.to_decimal(event.quantity),
            unit_cost=None if event.unit_cost is None else oracle.to_decimal(event.unit_cost),
        )
    return Transfer(
        key=engine_key,
        asset=event.asset,
        quantity=oracle.to_decimal(event.quantity),
        from_location=event.from_location,
        to_location=event.to_location,
    )


def oracle_replay(events: Iterable[AccountingEvent], cash: Iterable[str] = CASH) -> oracle.Result:
    return oracle.replay([to_oracle(event) for event in events], cash)


def _exact(value: Decimal) -> Fraction:
    assert isinstance(value, Decimal), f"{value!r} is not a Decimal"
    assert value.is_finite(), value
    return Fraction(value)


def _exact_or_none(value: Decimal | None) -> Fraction | None:
    return None if value is None else _exact(value)


def engine_to_oracle_result(result: AccountingResult) -> oracle.Result:
    """The engine's result in the oracle's types, exactly, so both render the same way."""
    warnings: list[oracle.ReplayWarning] = []
    for warning in result.warnings:
        if isinstance(warning, NegativeInventory):
            warnings.append(
                oracle.NegativeInventory(
                    to_oracle_key(warning.key), warning.asset, _exact(warning.shortfall)
                )
            )
        else:
            assert isinstance(warning, UnattributedFee), warning
            warnings.append(
                oracle.UnattributedFee(
                    to_oracle_key(warning.key),
                    warning.fee_asset,
                    _exact(warning.quantity),
                    warning.charged_to,
                )
            )
    return oracle.Result(
        positions=tuple(
            oracle.Position(
                asset=found.asset,
                quantity=_exact(found.quantity),
                unknown_basis_quantity=_exact(found.unknown_basis_quantity),
                cost_basis=_exact(found.cost_basis),
                average_cost=_exact_or_none(found.average_cost),
                realized_pnl=_exact(found.realized_pnl),
                unmatched_proceeds=_exact(found.unmatched_proceeds),
                flags=frozenset(flag.name for flag in found.flags),
            )
            for found in result.positions
        ),
        warnings=tuple(warnings),
        lots=tuple(
            oracle.Lot(
                asset=lot.asset,
                key=to_oracle_key(lot.key),
                quantity=_exact(lot.quantity),
                cost_basis=_exact(lot.cost_basis),
                unknown_basis_quantity=_exact(lot.unknown_basis_quantity),
            )
            for lot in result.lots
        ),
        unallocated_costs=_exact(result.unallocated_costs),
        event_count=result.event_count,
    )


def engine_to_json(result: AccountingResult) -> dict[str, object]:
    """The engine's result in the golden file's shape. Off-grid amounts fail, never round."""
    return oracle.result_to_json(engine_to_oracle_result(result))


def comparable(result: AccountingResult) -> dict[str, object]:
    """Everything but the fingerprint and the event count: what I7 says a transfer leaves alone."""
    rendered = engine_to_json(result)
    del rendered["event_count"]
    return rendered
