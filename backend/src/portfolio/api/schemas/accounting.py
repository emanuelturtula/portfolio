"""Response models for `GET /api/accounting/positions`: the owner's holdings, costs and returns.

**This is the first schema module whose money fields are the owner's own position** -- what
they hold, what it cost them, what they have gained -- rather than a price or a balance read
off a public chain. Two rules follow, and both are the ones every other money field here
already obeys; they are restated because the stakes are higher.

## Every amount, quantity and percentage is a JSON string

`MoneyStr`, for the reason `api/schemas/money.py` gives: JSON has one numeric type, a client
parses it into an IEEE-754 double, and a cost basis at eighteen places is inexact before any
client code runs. The percentage return is a string too -- it is a quotient of two money
figures, and a number there would be the one field a client could sum with `+`.

Amounts arrive at the scale the engine carries them at, eighteen places, and the percentage
at four. They are exact rather than pretty; the frontend formats them with `decimal.js`.

## Nothing identifies a trade

A warning carries the venue and the moment, which is what the owner needs to find the fill,
and not its `external_id`: a trade id is a cursor at one venue, and a warning is exactly the
kind of row that ends up quoted in a log or a support message. The snapshot keeps the ids it
needs -- on its lots, which no endpoint reads.

## A missing figure is `null` with a reason, never a zero

A position without a price has `market_value`, `unrealized_pnl` and `unrealized_return_pct`
`null` and `market_value_unavailable_reason` set; the totals leave it out and name it in
`totals.excluded`. So does a position holding units of unknown cost. With no snapshot yet,
`computed_at` is `null` and the lists are empty: "not computed" rather than "holds nothing".
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from portfolio.api.schemas.balances import PriceResponse
from portfolio.api.schemas.money import MoneyStr

# Runtime imports, not `TYPE_CHECKING` ones: Pydantic resolves a field's type when the model
# class is created. The two storage enums come from the service, which re-exports them,
# because the API layer may not import `portfolio.repositories`; the domain's own
# vocabulary comes from `domain`, which every layer may import.
from portfolio.domain.accounting import ExclusionReason, PositionFlag
from portfolio.domain.currencies import QuoteCurrency
from portfolio.services.accounting import AccountingWarningKind, RecomputeOutcome
from portfolio.services.prices import PriceUnavailable

if TYPE_CHECKING:
    from portfolio.domain.accounting import Exclusion, PortfolioTotals
    from portfolio.services.accounting import (
        AccountingStatus,
        PositionsView,
        PricedPosition,
        SnapshotWarning,
    )


class LastRecomputeResponse(BaseModel):
    """The last recompute attempt since the process started. In memory: a restart clears it.

    `error` is the exception's class name when `outcome` is `failed` -- never its message --
    and `null` otherwise. A `failed` outcome means the snapshot served is the one before it.
    """

    at: datetime
    outcome: RecomputeOutcome
    error: str | None

    @classmethod
    def of(cls, status: AccountingStatus) -> LastRecomputeResponse:
        """Render the status the trigger recorded."""
        return cls(at=status.at, outcome=status.outcome, error=status.error)


class AccountingPositionResponse(BaseModel):
    """One asset's position, valued.

    * `quantity` is everything held; `unknown_basis_quantity` the part of it with no known
      cost.
    * `total_invested` is the cost of the known-cost part, and `average_cost` that cost per
      known-cost unit, `null` when there is none.
    * `realized_pnl` is what sales of known-cost units made; `unmatched_proceeds` what sales
      of units with no known cost brought in, kept out of it.
    * `market_value` is the price times **every** unit held. `unrealized_pnl` and
      `unrealized_return_pct` cover **only the known-cost part**, which is the only part with a
      cost to compare against. The percentage is `null` when the basis is zero or negative.
    """

    asset: str
    quantity: MoneyStr
    unknown_basis_quantity: MoneyStr
    average_cost: MoneyStr | None
    total_invested: MoneyStr
    realized_pnl: MoneyStr
    unmatched_proceeds: MoneyStr
    flags: list[PositionFlag]
    price: PriceResponse | None
    market_value: MoneyStr | None
    market_value_unavailable_reason: PriceUnavailable | None
    unrealized_pnl: MoneyStr | None
    unrealized_return_pct: MoneyStr | None

    @classmethod
    def of(cls, entry: PricedPosition) -> AccountingPositionResponse:
        """Render one valued position. The flags are sorted, so the order is stable."""
        value = entry.value
        position = value.position
        reason = value.market_value_unavailable_reason
        return cls(
            asset=position.asset,
            quantity=position.quantity,
            unknown_basis_quantity=position.unknown_basis_quantity,
            average_cost=position.average_cost,
            total_invested=position.cost_basis,
            realized_pnl=position.realized_pnl,
            unmatched_proceeds=position.unmatched_proceeds,
            flags=sorted(position.flags),
            price=None if entry.price is None else PriceResponse.of(entry.price),
            market_value=value.market_value,
            market_value_unavailable_reason=None if reason is None else PriceUnavailable(reason),
            unrealized_pnl=value.unrealized_pnl,
            unrealized_return_pct=value.unrealized_return_pct,
        )


class ExclusionResponse(BaseModel):
    """A position left out of the totals, and why: `unknown_basis` or `unpriced`."""

    asset: str
    reason: ExclusionReason

    @classmethod
    def of(cls, exclusion: Exclusion) -> ExclusionResponse:
        """Render one exclusion."""
        return cls(asset=exclusion.asset, reason=exclusion.reason)


class AccountingTotalsResponse(BaseModel):
    """The portfolio's totals, over the positions that can be compared, and the ones left out.

    `total_invested`, `market_value`, `unrealized_pnl` and `unrealized_return_pct` cover the same
    positions -- valued, or holding nothing, and with no unknown-cost units -- so the
    percentage is the return on exactly the money in the total beside it. `realized_pnl`
    covers every position. The client sums nothing: every figure it shows is here.
    """

    total_invested: MoneyStr
    market_value: MoneyStr
    unrealized_pnl: MoneyStr
    unrealized_return_pct: MoneyStr | None
    realized_pnl: MoneyStr
    excluded: list[ExclusionResponse]

    @classmethod
    def of(cls, totals: PortfolioTotals) -> AccountingTotalsResponse:
        """Render the domain's totals."""
        return cls(
            total_invested=totals.total_invested,
            market_value=totals.market_value,
            unrealized_pnl=totals.unrealized_pnl,
            unrealized_return_pct=totals.unrealized_return_pct,
            realized_pnl=totals.realized_pnl,
            excluded=[ExclusionResponse.of(exclusion) for exclusion in totals.excluded],
        )


class AccountingWarningResponse(BaseModel):
    """Something the history could not account for, and where to look. No trade id.

    * `negative_inventory` -- a disposal of `asset` at `occurred_at` on `source` was
      `quantity` larger than everything the history held: a deposit or an older fill is
      missing.
    * `unattributed_fee` -- `quantity` of a fee paid in `asset` could not be valued, so
      `charged_to`'s figures leave it out (`null` for a conversion between two stablecoins).
    """

    kind: AccountingWarningKind
    occurred_at: datetime
    source: str
    asset: str
    quantity: MoneyStr
    charged_to: str | None

    @classmethod
    def of(cls, warning: SnapshotWarning) -> AccountingWarningResponse:
        """Render one stored warning."""
        return cls(
            kind=warning.kind,
            occurred_at=warning.occurred_at,
            source=warning.source,
            asset=warning.asset,
            quantity=warning.quantity,
            charged_to=warning.charged_to,
        )


class PositionsResponse(BaseModel):
    """The owner's cost-basis snapshot, valued in USD, with how old it is.

    `computed_at` is when the snapshot was last written, `null` before the first one;
    `last_recompute` is the last attempt since the process started, `null` before it, and
    says whether the snapshot served is current. `unallocated_costs` is known value that
    belongs to no position, such as a stablecoin conversion's fee.
    """

    method: str
    quote_currency: QuoteCurrency
    computed_at: datetime | None
    event_count: int
    last_recompute: LastRecomputeResponse | None
    positions: list[AccountingPositionResponse]
    totals: AccountingTotalsResponse
    unallocated_costs: MoneyStr
    warnings: list[AccountingWarningResponse]

    @classmethod
    def of(
        cls,
        view: PositionsView,
        *,
        last_recompute: AccountingStatus | None,
    ) -> PositionsResponse:
        """Render the service's view, and the trigger's last outcome beside it."""
        return cls(
            method=view.method,
            quote_currency=view.quote_currency,
            computed_at=view.computed_at,
            event_count=view.event_count,
            last_recompute=(
                None if last_recompute is None else LastRecomputeResponse.of(last_recompute)
            ),
            positions=[AccountingPositionResponse.of(entry) for entry in view.positions],
            totals=AccountingTotalsResponse.of(view.totals),
            unallocated_costs=view.unallocated_costs,
            warnings=[AccountingWarningResponse.of(warning) for warning in view.warnings],
        )
