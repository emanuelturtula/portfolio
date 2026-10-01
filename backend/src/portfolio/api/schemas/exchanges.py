"""Request parsing and response models for the exchange endpoints.

**No field here can carry a credential, and none is named like one.** `configured` is the
whole disclosure: whether the process has credentials for a venue, never what they are. No
model has a field whose name contains `key` (other than `exchange_key`), `secret`,
`passphrase`, `credential`, `token` or `signature`, and a test walks the OpenAPI document to
hold that.

## Money crosses this API since #93, and only as a JSON string

`GET /api/exchanges/fills` serves the owner's fills and what they add up to: quantities,
prices, quote amounts, USDT values and fees. Every one of them is `MoneyStr` -- a JSON string,
in positional notation, at the stored scale of eighteen places -- for the reason
`api/schemas/money.py` gives, and the totals are the server's: a client holds one page and
must never sum it. Datetimes are ISO 8601 with an offset, as every datetime this API serves.

## The order id crosses it; the trade id never does, and neither is logged

A fill carries `order_id`, the venue's `external_order_id`, because the owner needs it to find
the trade at the venue. **The venue's trade id, `external_trade_id`, is never served**: it is a
cursor at Bitget, and the view does not even load it. `id` is our own row id, a stable key.
Nothing in this module logs either id, and nothing on the request path does.

**No cursor, and no text a venue wrote, either.** `detail` is the recorded outcome detail --
an exchange error's fixed summary with a status and a digits-only venue code, a conflict's
count, or an exception's type name. `raw_payload`, the venue's own fill object, is never read
on this path, let alone served.

## `from` and `to` are ISO 8601, parsed by the standard library

For the reason spec 023 (R8) found: Pydantic's lax `datetime` reads a string of digits as Unix
time, so `1767225600` would become 2026-01-01 and `20260101` 1970-08-23, both timezone-aware
and so past every later check. `InstantQuery` parses with `datetime.fromisoformat` instead: a
string it refuses is a 422 with `INSTANT_FORMAT_REFUSAL`, which does not quote it, and
`20260101` becomes the naive midnight it spells, which the service refuses as naive. Whether a
bound is aware, representable and before the other is the service's to decide.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BaseModel, BeforeValidator, Field

from portfolio.api.schemas.money import MoneyStr

# Runtime imports: Pydantic resolves an enum field's type at class-creation time. Taken from
# the read-side service, which re-exports them, because the API layer may not import
# `portfolio.repositories`.
from portfolio.services.exchanges import (
    AccountOutcomeStatus,
    AccountSyncStatus,
    ExchangeKey,
    ExchangeSyncErrorKind,
    FillSide,
    SyncRunStatus,
    SyncTrigger,
)

if TYPE_CHECKING:
    from portfolio.domain.fill_totals import (
        AssetFillTotals,
        FeeTotal,
        FillTotals,
        NotValuedInUsdtTotals,
        QuoteAssetFillTotals,
        UsdtFillTotals,
    )
    from portfolio.services.exchanges import (
        AccountOutcome,
        ExchangeSyncRunSummary,
        ExchangeView,
        FillsPage,
        FillView,
        LastError,
    )
    from portfolio.services.sync_coordinator import SyncOutcome

INSTANT_FORMAT_REFUSAL: Final = (
    "a datetime must be ISO 8601 with a timezone offset, such as 2026-03-01T00:00:00Z"
)
"""The refusal of a `from` or `to` that `datetime.fromisoformat` cannot parse. Fixed text: the
parser's own message quotes the input. Pydantic prefixes it with `Value error, `."""


def _parse_iso_instant(value: object) -> object:
    """Parse a query-string datetime with `datetime.fromisoformat`, never as Unix time.

    A string that does not parse is refused with `INSTANT_FORMAT_REFUSAL`, raised `from None`.
    Anything else -- only a `datetime` built in Python can reach here otherwise -- goes on to
    Pydantic unchanged. A naive result is returned as it is: refusing it is the service's rule.
    """
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(INSTANT_FORMAT_REFUSAL) from None
    return value


InstantQuery = Annotated[datetime, BeforeValidator(_parse_iso_instant)]
"""A `from` or `to` query parameter: ISO 8601 by the standard library (spec 023, R8)."""


class ExchangeLastErrorResponse(BaseModel):
    """Why the account's latest attempted sync failed. Skipped runs are not attempts."""

    error_kind: ExchangeSyncErrorKind
    detail: str | None

    @classmethod
    def of(cls, error: LastError) -> ExchangeLastErrorResponse:
        """Render a service view."""
        return cls(error_kind=error.error_kind, detail=error.detail)


class ExchangeResponse(BaseModel):
    """One venue: whether it is configured, where its sync stands, what history it holds.

    `history_truncated` is `effective_since > requested_since`: the venue's retention cut the
    requested history short, and `effective_since` is where what is held begins. `syncing` is
    true while an exchange sync is in flight and this venue is configured.
    """

    exchange_key: ExchangeKey
    configured: bool
    status: AccountSyncStatus
    syncing: bool
    requested_since: datetime | None
    effective_since: datetime | None
    history_truncated: bool
    last_synced_at: datetime | None
    fills_stored: int
    pending_windows: int
    last_error: ExchangeLastErrorResponse | None

    @classmethod
    def of(cls, view: ExchangeView) -> ExchangeResponse:
        """Render a service view."""
        return cls(
            exchange_key=view.exchange_key,
            configured=view.configured,
            status=view.status,
            syncing=view.syncing,
            requested_since=view.requested_since,
            effective_since=view.effective_since,
            history_truncated=view.history_truncated,
            last_synced_at=view.last_synced_at,
            fills_stored=view.fills_stored,
            pending_windows=view.pending_windows,
            last_error=(
                None if view.last_error is None else ExchangeLastErrorResponse.of(view.last_error)
            ),
        )


class ExchangeListResponse(BaseModel):
    """Every configured venue and every venue with an account, sorted by `exchange_key`."""

    exchanges: list[ExchangeResponse]


class ExchangeAccountOutcomeResponse(BaseModel):
    """What one account did during one run."""

    exchange_key: ExchangeKey
    status: AccountOutcomeStatus
    windows_completed: int
    pages: int
    fills_seen: int
    fills_inserted: int
    error_kind: ExchangeSyncErrorKind | None
    detail: str | None

    @classmethod
    def of(cls, outcome: AccountOutcome) -> ExchangeAccountOutcomeResponse:
        """Render a service view."""
        return cls(
            exchange_key=outcome.exchange_key,
            status=outcome.status,
            windows_completed=outcome.windows_completed,
            pages=outcome.pages,
            fills_seen=outcome.fills_seen,
            fills_inserted=outcome.fills_inserted,
            error_kind=outcome.error_kind,
            detail=outcome.detail,
        )


class ExchangeSyncRunResponse(BaseModel):
    """One exchange sync run: when, how long, how many accounts, and what each did.

    `fills_seen` and `fills_inserted` are the sums of the accounts' counts. `finished_at` and
    `duration_ms` are `null` for a run in flight and for an interrupted one.
    """

    run_id: int
    trigger: SyncTrigger
    status: SyncRunStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    accounts_total: int
    accounts_succeeded: int
    accounts_failed: int
    accounts_skipped: int
    fills_seen: int
    fills_inserted: int
    accounts: list[ExchangeAccountOutcomeResponse]

    @classmethod
    def of(cls, summary: ExchangeSyncRunSummary) -> ExchangeSyncRunResponse:
        """Render a service view."""
        return cls(
            run_id=summary.run_id,
            trigger=summary.trigger,
            status=summary.status,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            duration_ms=summary.duration_ms,
            accounts_total=summary.accounts_total,
            accounts_succeeded=summary.accounts_succeeded,
            accounts_failed=summary.accounts_failed,
            accounts_skipped=summary.accounts_skipped,
            fills_seen=summary.fills_seen,
            fills_inserted=summary.fills_inserted,
            accounts=[ExchangeAccountOutcomeResponse.of(outcome) for outcome in summary.accounts],
        )


class ExchangeSyncTriggeredResponse(ExchangeSyncRunResponse):
    """A run summary plus whether this request started it or joined one in flight.

    `joined` is a fact about this call, not the run, for the reason `SyncTriggeredResponse`
    gives. When it is true, `trigger` is the running run's.
    """

    joined: bool

    @classmethod
    def of_outcome(
        cls,
        outcome: SyncOutcome[ExchangeSyncRunSummary],
    ) -> ExchangeSyncTriggeredResponse:
        """Render the coordinator's answer."""
        summary = outcome.summary
        return cls(
            run_id=summary.run_id,
            trigger=summary.trigger,
            status=summary.status,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            duration_ms=summary.duration_ms,
            accounts_total=summary.accounts_total,
            accounts_succeeded=summary.accounts_succeeded,
            accounts_failed=summary.accounts_failed,
            accounts_skipped=summary.accounts_skipped,
            fills_seen=summary.fills_seen,
            fills_inserted=summary.fills_inserted,
            accounts=[ExchangeAccountOutcomeResponse.of(item) for item in summary.accounts],
            joined=outcome.joined,
        )


class ExchangeSyncRunListResponse(BaseModel):
    """The exchange run log, newest first, wrapped in an object so it can grow."""

    runs: list[ExchangeSyncRunResponse]


_NET_SIGN: Final = (
    "Buys minus sells: positive is net buying. May be negative over a filtered range, and is "
    "never clamped to zero."
)


class ExchangeFillResponse(BaseModel):
    """One stored fill, as the venue reported it. Never its trade id, never its raw payload."""

    id: int = Field(description="Our row id: a stable key, not the venue's trade id.")
    executed_at: datetime = Field(description="When the venue executed it, in UTC.")
    exchange_key: ExchangeKey
    symbol: str = Field(description="The pair as the venue spells it, such as `BTCUSDT`.")
    base_asset: str
    quote_asset: str
    side: FillSide = Field(description="Which way the base asset moved, for the owner.")
    quantity: MoneyStr = Field(description="In the base asset.")
    price: MoneyStr = Field(description="Quote asset per unit of the base asset.")
    quote_quantity: MoneyStr = Field(
        description="In the quote asset, as stored: never recomputed as quantity times price."
    )
    quote_quantity_derived: bool = Field(
        description="Whether `quote_quantity` was derived because the venue did not send it."
    )
    usdt_value: MoneyStr | None = Field(
        description="`quote_quantity` when `quote_asset` is `USDT`, else `null`: another quote "
        "is never converted."
    )
    fee_amount: MoneyStr = Field(
        description="In `fee_asset`, signed: positive is a fee paid, negative a rebate."
    )
    fee_asset: str | None = Field(description="`null` only when the fee is zero.")
    order_id: str | None = Field(
        description="The venue's order id, to find the trade at the venue; `null` when it sent "
        "none. Several fills may share one."
    )

    @classmethod
    def of(cls, view: FillView) -> ExchangeFillResponse:
        """Render a service view."""
        return cls(
            id=view.id,
            executed_at=view.executed_at,
            exchange_key=view.exchange_key,
            symbol=view.symbol,
            base_asset=view.base_asset,
            quote_asset=view.quote_asset,
            side=view.side,
            quantity=view.quantity,
            price=view.price,
            quote_quantity=view.quote_quantity,
            quote_quantity_derived=view.quote_quantity_derived,
            usdt_value=view.usdt_value,
            fee_amount=view.fee_amount,
            fee_asset=view.fee_asset,
            order_id=view.order_id,
        )


class ExchangeFillAssetTotalsResponse(BaseModel):
    """One base asset: quantities over all its fills, USDT over its USDT-quoted ones."""

    asset: str
    fill_count: int
    bought: MoneyStr = Field(description="Quantity bought, in the asset, over every quote.")
    sold: MoneyStr = Field(description="Quantity sold, in the asset, over every quote.")
    net: MoneyStr = Field(description=f"`bought - sold`, in the asset. {_NET_SIGN}")
    usdt_spent: MoneyStr = Field(description="USDT spent on its USDT-quoted buys.")
    usdt_received: MoneyStr = Field(description="USDT received from its USDT-quoted sells.")
    usdt_net: MoneyStr = Field(description=f"`usdt_spent - usdt_received`. {_NET_SIGN}")
    usdt_unvalued_fill_count: int = Field(
        description="How many of its fills are quoted in something other than USDT, and so are "
        "left out of its USDT figures."
    )

    @classmethod
    def of(cls, totals: AssetFillTotals) -> ExchangeFillAssetTotalsResponse:
        """Render a domain total."""
        return cls(
            asset=totals.asset,
            fill_count=totals.fill_count,
            bought=totals.bought,
            sold=totals.sold,
            net=totals.net,
            usdt_spent=totals.usdt_spent,
            usdt_received=totals.usdt_received,
            usdt_net=totals.usdt_net,
            usdt_unvalued_fill_count=totals.usdt_unvalued_fill_count,
        )


class ExchangeFillUsdtTotalsResponse(BaseModel):
    """USDT across every asset, over the USDT-quoted fills."""

    spent: MoneyStr = Field(description="USDT spent on buys.")
    received: MoneyStr = Field(description="USDT received from sells.")
    net: MoneyStr = Field(description=f"`spent - received`. {_NET_SIGN}")

    @classmethod
    def of(cls, totals: UsdtFillTotals) -> ExchangeFillUsdtTotalsResponse:
        """Render a domain total."""
        return cls(spent=totals.spent, received=totals.received, net=totals.net)


class ExchangeFillQuoteAssetTotalsResponse(BaseModel):
    """One quote asset other than USDT, summed in itself. Never converted."""

    quote_asset: str
    fill_count: int
    spent: MoneyStr = Field(description="The quote paid on buys, in `quote_asset`.")
    received: MoneyStr = Field(description="The quote received on sells, in `quote_asset`.")
    net: MoneyStr = Field(description=f"`spent - received`, in `quote_asset`. {_NET_SIGN}")

    @classmethod
    def of(cls, totals: QuoteAssetFillTotals) -> ExchangeFillQuoteAssetTotalsResponse:
        """Render a domain total."""
        return cls(
            quote_asset=totals.quote_asset,
            fill_count=totals.fill_count,
            spent=totals.spent,
            received=totals.received,
            net=totals.net,
        )


class ExchangeFillNotValuedInUsdtResponse(BaseModel):
    """The fills quoted in anything but USDT: how many, and their sums per quote asset."""

    fill_count: int
    by_quote_asset: list[ExchangeFillQuoteAssetTotalsResponse] = Field(
        description="Sorted by quote asset."
    )

    @classmethod
    def of(cls, totals: NotValuedInUsdtTotals) -> ExchangeFillNotValuedInUsdtResponse:
        """Render a domain total."""
        return cls(
            fill_count=totals.fill_count,
            by_quote_asset=[
                ExchangeFillQuoteAssetTotalsResponse.of(item) for item in totals.by_quote_asset
            ],
        )


class ExchangeFillFeeTotalResponse(BaseModel):
    """The fees paid in one asset, summed with their sign and never converted."""

    asset: str
    amount: MoneyStr = Field(
        description="Signed: positive is fees paid, negative is rebates. Listed even when it "
        "sums to zero, if a fill carried a fee in this asset."
    )

    @classmethod
    def of(cls, total: FeeTotal) -> ExchangeFillFeeTotalResponse:
        """Render a domain total."""
        return cls(asset=total.asset, amount=total.amount)


class ExchangeFillTotalsResponse(BaseModel):
    """What the whole filtered set adds up to, whatever page was asked for."""

    fill_count: int
    by_asset: list[ExchangeFillAssetTotalsResponse] = Field(description="Sorted by asset.")
    usdt: ExchangeFillUsdtTotalsResponse
    not_valued_in_usdt: ExchangeFillNotValuedInUsdtResponse
    fees: list[ExchangeFillFeeTotalResponse] = Field(description="Sorted by asset.")

    @classmethod
    def of(cls, totals: FillTotals) -> ExchangeFillTotalsResponse:
        """Render the domain totals."""
        return cls(
            fill_count=totals.fill_count,
            by_asset=[ExchangeFillAssetTotalsResponse.of(item) for item in totals.by_asset],
            usdt=ExchangeFillUsdtTotalsResponse.of(totals.usdt),
            not_valued_in_usdt=ExchangeFillNotValuedInUsdtResponse.of(totals.not_valued_in_usdt),
            fees=[ExchangeFillFeeTotalResponse.of(item) for item in totals.fees],
        )


class ExchangeFillListResponse(BaseModel):
    """One page of the filtered fills, newest first, and the totals over all of them.

    `total_count` is how many fills matched the filters, which `totals.fill_count` repeats;
    `fills` holds at most `limit` of them, from `offset`.
    """

    fills: list[ExchangeFillResponse]
    total_count: int
    totals: ExchangeFillTotalsResponse

    @classmethod
    def of(cls, page: FillsPage) -> ExchangeFillListResponse:
        """Render a service page."""
        return cls(
            fills=[ExchangeFillResponse.of(view) for view in page.fills],
            total_count=page.total_count,
            totals=ExchangeFillTotalsResponse.of(page.totals),
        )
