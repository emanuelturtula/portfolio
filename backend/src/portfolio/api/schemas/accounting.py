"""Response models for `/api/accounting`: the owner's holdings, costs, returns and balances.

`GET /api/accounting/positions` serves the snapshot valued, and
`GET /api/accounting/reconciliation` (#104) the same snapshot's quantities beside the balances
read from the wallets and the venues. `GET /api/accounting/first-trades` (#111) says when each
asset's imported history begins: an asset and an instant, and no amount.

**This is the first schema module whose money fields are the owner's own position** -- what
they hold, what it cost them, what they have gained -- rather than a price or a balance read
off a public chain. Two rules follow, and both are the ones every other money field here
already obeys; they are restated because the stakes are higher.

## Every amount, quantity and percentage is a JSON string

`MoneyStr`, for the reason `api/schemas/money.py` gives: JSON has one numeric type, a client
parses it into an IEEE-754 double, and a cost basis at eighteen places is inexact before any
client code runs. The percentage return is a string too -- it is a quotient of two money
figures, and a number there would be the one field a client could sum with `+`.

Amounts and quantities arrive at the scale the engine carries them at, eighteen places; the
price at the scale it is stored at, twelve (`PRICE_SCALE`); and the percentage at four. They
are exact rather than pretty; the frontend formats them with `decimal.js`.

## Nothing identifies a trade

A warning carries the venue and the moment, which is what the owner needs to find the fill,
and not its `external_id`: a trade id is a cursor at one venue, and a warning is exactly the
kind of row that ends up quoted in a log or a support message. The snapshot keeps the ids it
needs -- on its lots, which no endpoint reads.

## A missing figure is `null` with a reason, never a zero

A position without a price has `market_value`, `unrealized_pnl` and `unrealized_return_pct`
`null` and `market_value_unavailable_reason` set; the totals leave it out and name it in
`totals.excluded`. So does a position holding units of unknown cost. The reason is a price's
(`PriceUnavailable`), or `value_out_of_range` when the price times the quantity is too large to
represent (`ValueUnavailable`, spec 021, R6). With no snapshot yet, `computed_at` is `null` and
the lists are empty: "not computed" rather than "holds nothing".

## The reconciliation names every source it left out

The balances the holdings check compares against are a lower bound on what the owner holds,
and only a reading that is current is one (spec 025, R9). So beside the per-asset comparison
the response says how each source stands: every exchange account with when its balances were
last read, the kind its last attempt failed with, and the reason it was left out if it was;
and how many wallets were compared, how many had a reading too old to compare, how many
were never read, and how many were left out because the latest finished balance sync could
not read their chain (spec 028), with each such chain named. A source that is left out is a
reason or a count, never a quantity of zero.
`last_recompute` is served for the reason the positions endpoint serves it: a failed recompute
means the history compared is older than the balances beside it. It is the one response that
carries what a venue holds, per asset and summed over the accounts; no log line does.
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
from portfolio.domain.accounting import (
    ExclusionReason,
    PositionFlag,
    ReconciliationStatus,
    ValueUnavailable,
)
from portfolio.domain.currencies import QuoteCurrency
from portfolio.domain.exchanges import ExchangeKey
from portfolio.services.accounting import AccountingWarningKind, RecomputeOutcome
from portfolio.services.prices import PriceUnavailable
from portfolio.services.reconciliation import ExchangeSyncErrorKind, NotComparedReason

if TYPE_CHECKING:
    from collections.abc import Iterable

    from portfolio.domain.accounting import AssetReconciliation, Exclusion, PortfolioTotals
    from portfolio.services.accounting import (
        AccountingStatus,
        FirstTrade,
        PositionsView,
        PricedPosition,
        SnapshotWarning,
    )
    from portfolio.services.reconciliation import (
        ExchangeBalanceSource,
        FailedChain,
        ReconciliationView,
        WalletSources,
    )


def _unavailable(reason: str | None) -> PriceUnavailable | ValueUnavailable | None:
    """A position's reason as the member of the vocabulary it belongs to.

    Two vocabularies, because two layers produce them: `services.prices` knows why there is no
    price, and `domain.accounting.valuation` -- which may not import it -- knows when a price
    gives a value too large to represent. A string in neither raises `ValueError`, which is a
    defect in whatever produced it rather than something to render.
    """
    if reason is None:
        return None
    if reason in {member.value for member in ValueUnavailable}:
        return ValueUnavailable(reason)
    return PriceUnavailable(reason)


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
    market_value_unavailable_reason: PriceUnavailable | ValueUnavailable | None
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
            market_value_unavailable_reason=_unavailable(reason),
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
    covers every position, and so does `unmatched_proceeds`: held or not, left out or not.
    `unmatched_proceeds` is signed, because a sale's proceeds are net of every fee. The client
    sums nothing: every figure it shows is here.
    """

    total_invested: MoneyStr
    market_value: MoneyStr
    unrealized_pnl: MoneyStr
    unrealized_return_pct: MoneyStr | None
    realized_pnl: MoneyStr
    unmatched_proceeds: MoneyStr
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
            unmatched_proceeds=totals.unmatched_proceeds,
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


class AssetReconciliationResponse(BaseModel):
    """One asset: what the history says is held, what was read as held, and how they compare.

    * `history_quantity` is the replayed position's quantity.
    * `wallet_quantity` and `exchange_quantity` are what the wallets and the exchange accounts
      with a current reading were read as holding, and `held_quantity` their sum. A source
      that was left out adds nothing to them.
    * `difference` is `held_quantity - history_quantity`, signed.
    * `status` is `match` when the difference is within `tolerance_pct` percent of the larger
      side; otherwise `history_short` when more is held than the history accounts for --
      usually buys missing from it, and for a while coins in transit between two readings --
      and `history_over` when the history accounts for more than was read, which coins held
      elsewhere, a withdrawal, an unrecorded fee and a sale the import did not see all
      produce, and the check cannot tell apart.
    """

    asset: str
    history_quantity: MoneyStr
    wallet_quantity: MoneyStr
    exchange_quantity: MoneyStr
    held_quantity: MoneyStr
    difference: MoneyStr
    status: ReconciliationStatus

    @classmethod
    def of(cls, row: AssetReconciliation) -> AssetReconciliationResponse:
        """Render one row of the comparison."""
        return cls(
            asset=row.asset,
            history_quantity=row.history_quantity,
            wallet_quantity=row.wallet_quantity,
            exchange_quantity=row.exchange_quantity,
            held_quantity=row.held_quantity,
            difference=row.difference,
            status=row.status,
        )


class ExchangeBalancesResponse(BaseModel):
    """How one exchange account's balances stand as a source of the comparison.

    `balances_read_at` is when a read last succeeded, `null` when none ever has.
    `balances_error` is the kind the last attempt failed with, `null` when it succeeded or none
    was made.

    `not_compared_reason` is `null` when the account's balances are in the comparison. Otherwise
    they are **not**, whatever was last read, and it says why: `read_failed` (the last read
    failed), `never_read`, `sync_failed` (the account's fill sync is not `ok`, so nothing is
    refreshing the reading) or `out_of_date` (the reading is older than
    `max_reading_age_hours`). The first that applies, in that order.
    """

    exchange_key: ExchangeKey
    balances_read_at: datetime | None
    balances_error: ExchangeSyncErrorKind | None
    not_compared_reason: NotComparedReason | None

    @classmethod
    def of(cls, source: ExchangeBalanceSource) -> ExchangeBalancesResponse:
        """Render one account's state."""
        return cls(
            exchange_key=source.exchange_key,
            balances_read_at=source.balances_read_at,
            balances_error=source.balances_error,
            not_compared_reason=source.not_compared_reason,
        )


class FailedChainResponse(BaseModel):
    """A chain the latest finished balance sync could not read, and what that left out.

    `chain_key` is the chain's key, as a wallet carries it. `wallets` is how many of the
    owner's active wallets on that chain are left out of the comparison because of it, and is
    never zero. It does not say why the chain failed: `GET /api/balances/runs` does.
    """

    chain_key: str
    wallets: int

    @classmethod
    def of(cls, chain: FailedChain) -> FailedChainResponse:
        """Render one failed chain."""
        return cls(chain_key=chain.chain_key, wallets=chain.wallets)


class WalletsReadResponse(BaseModel):
    """How the active wallets stand as a source of the comparison. The counts add up to all.

    `compared` wallets are in the comparison: their reading is at most
    `max_reading_age_hours` old, and their chain did not fail in the latest finished balance
    sync, or a later sync has read them since. The other three counts are wallets that add
    nothing, so their coins are missing from it, each under the first reason that applies:
    `chain_failed` ones are on a chain the latest finished balance sync could not read, and
    no later sync has read them; `unread` ones have no reading; `stale` ones have a reading
    older than the limit.

    `failed_chains` names the chains behind `chain_failed`, sorted by `chain_key`. Only a
    chain with at least one wallet left out is listed, and the entries' `wallets` add up to
    `chain_failed`. `oldest_observed_at` is the oldest reading among the `compared` wallets,
    `null` when none is compared.
    """

    compared: int
    stale: int
    unread: int
    chain_failed: int
    failed_chains: list[FailedChainResponse]
    oldest_observed_at: datetime | None

    @classmethod
    def of(cls, sources: WalletSources) -> WalletsReadResponse:
        """Render the wallets' state, the failed chains in the order the service gives."""
        return cls(
            compared=sources.compared,
            stale=sources.stale,
            unread=sources.unread,
            chain_failed=sources.chain_failed,
            failed_chains=[FailedChainResponse.of(chain) for chain in sources.failed_chains],
            oldest_observed_at=sources.oldest_observed_at,
        )


class ReconciliationResponse(BaseModel):
    """The replayed quantities beside the balances read, per asset, and every source's state.

    `computed_at` is the cost-basis snapshot's, and `null` before the first one -- and then
    `assets` is empty: nothing has been compared, which is not the same as nothing matching.
    `exchanges` and `wallets` are answered either way. `tolerance_pct` is the percentage, as a
    string, at or under which a difference counts as a match. `assets` is sorted by asset and
    leaves the cash assets out; `exchanges` lists every account, by `exchange_key`, compared
    or not.

    `max_reading_age_hours` is how old a wallet's or a venue's reading may be and still be
    compared. `last_recompute` is the last recompute attempt since the process started, exactly
    as `GET /api/accounting/positions` serves it. It is `null` after a restart until the startup
    recompute ends, and the stored snapshot is compared meanwhile. When its outcome is
    `failed`, the snapshot compared here is older than the balances beside it, and an asset
    bought since shows as missing from the history.
    """

    computed_at: datetime | None
    tolerance_pct: MoneyStr
    assets: list[AssetReconciliationResponse]
    max_reading_age_hours: int
    last_recompute: LastRecomputeResponse | None
    exchanges: list[ExchangeBalancesResponse]
    wallets: WalletsReadResponse

    @classmethod
    def of(
        cls,
        view: ReconciliationView,
        *,
        last_recompute: AccountingStatus | None,
    ) -> ReconciliationResponse:
        """Render the service's view, and the trigger's last outcome beside it."""
        return cls(
            computed_at=view.computed_at,
            tolerance_pct=view.tolerance_pct,
            assets=[AssetReconciliationResponse.of(row) for row in view.assets],
            max_reading_age_hours=view.max_reading_age_hours,
            last_recompute=(
                None if last_recompute is None else LastRecomputeResponse.of(last_recompute)
            ),
            exchanges=[ExchangeBalancesResponse.of(source) for source in view.exchanges],
            wallets=WalletsReadResponse.of(view.wallets),
        )


class FirstTradeResponse(BaseModel):
    """One asset, and the instant of the earliest imported fill it takes part in.

    It takes part as the fill's base asset, its quote asset, or its fee asset when the fee is
    not zero. `first_trade_at` is the fill's own time, in UTC.
    """

    asset: str
    first_trade_at: datetime

    @classmethod
    def of(cls, first_trade: FirstTrade) -> FirstTradeResponse:
        """Render one asset's first trade."""
        return cls(asset=first_trade.asset, first_trade_at=first_trade.first_trade_at)


class FirstTradesResponse(BaseModel):
    """When the imported history of each asset begins, sorted by asset.

    One entry per asset that takes part in at least one of the owner's imported fills. The
    cash assets are left out, and manual adjustments are not counted: an asset that only an
    adjustment names is not listed. With no fills, `assets` is empty.
    """

    assets: list[FirstTradeResponse]

    @classmethod
    def of(cls, first_trades: Iterable[FirstTrade]) -> FirstTradesResponse:
        """Render the service's answer, in the order it gives."""
        return cls(assets=[FirstTradeResponse.of(first_trade) for first_trade in first_trades])
