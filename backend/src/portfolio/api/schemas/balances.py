"""Response models for the balance endpoints.

**Two different kinds of number cross this wire as JSON strings, for two different
reasons, and both are written down here because a reader will otherwise assume one rule.**

*Monetary values* -- `total`, `value`, `quantity`, `price.amount` -- are strings because
rule 2 says so: JSON has one numeric type, every mainstream parser reads it into an
IEEE-754 double, and `{"amount": 0.1}` is already inexact before any client code runs.
`MoneyStr` carries that contract into the OpenAPI document.

*Base units* -- `confirmed` and `pending` -- are `INTEGER` columns, so rule 2 does not
reach them, and they still have to be strings. The reason was measured rather than
assumed:

```
KAS supply ~28.7e9 x 1e8 sompi  = 2.87e18
Number.MAX_SAFE_INTEGER         = 9.007e15
```

A Kaspa balance above roughly 90 million KAS does not survive `JSON.parse`. That is a
plausible address rather than a hypothetical one, and the failure is silent: the number
arrives rounded, renders fine, and is wrong in the last digits. `BaseUnitsStr` is the
answer, and it is a separate annotated type from `MoneyStr` precisely so that the two
reasons stay distinguishable -- one day a base unit might legitimately become something
other than an integer, and conflating them now would hide that.

## Nullable, and never zero

A wallet no successful run has covered comes back with `confirmed`, `quantity`, `value`
and `observed_at` all `null`. A zero renders, sums and is believed; "we have not read this
address yet" and "this address holds nothing" are different facts and the dashboard is
allowed to say which.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import AfterValidator, BaseModel, PlainSerializer, WithJsonSchema

from portfolio.api.schemas.money import MoneyStr

# Runtime imports, not `TYPE_CHECKING` ones: Pydantic resolves a model's annotations at
# class-creation time, so an enum used as a field type has to be a real name. Taken from
# the service rather than from `repositories/sync_runs.py`, where they are defined: the
# API layer may not import `portfolio.repositories`, and the service that produces a
# value is where its type belongs to a caller.
from portfolio.services.balances import (
    HistoryCursor,
    QuoteCurrency,
    SyncErrorKind,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.services.prices import PriceUnavailable

if TYPE_CHECKING:
    from portfolio.services.balances import (
        ChainOutcome,
        CurrentBalances,
        SnapshotView,
        SyncRunSummary,
        UnreadWallet,
        WalletBalance,
        WalletHistory,
    )
    from portfolio.services.prices import Price, UnpricedHolding
    from portfolio.services.sync_coordinator import SyncOutcome

DEFAULT_QUOTE_CURRENCY: Final = QuoteCurrency.EUR
"""What `GET /api/balances/current` values in when the caller does not say.

**The spec shows this field in the response body and never says where it comes from**, so
it is a query parameter with a default rather than a setting: a setting would be a variable
with one right value per deployment, and this is a question a client may reasonably ask
differently on two renders. EUR because that is the currency the spec's own example uses.

**The parameter is a `QuoteCurrency`, so anything else is a 422.** It used to be passed
through unchecked, on the theory that an unknown currency would honestly come back with
every holding unpriced. It came back as `reason: never_fetched`, which sends an operator to
check whether the refresh has run when the real cause is that nothing will ever price that
currency. `domain/currencies.py` says why the set lives there rather than being imported
from the price package.
"""

MAX_CURSOR_LENGTH: Final = 128
"""The longest cursor the history endpoint will try to decode.

A real one is under eighty characters: an ISO-8601 instant with microseconds and an offset,
a separator and at most nineteen digits, base64-encoded. The bound is there so that decoding
is never the expensive part of refusing a request.
"""

_CURSOR_SEPARATOR: Final = "|"
_MAX_SNAPSHOT_ID: Final = 2**63 - 1
"""SQLite's `INTEGER` ceiling. A cursor claiming a larger id is refused as malformed rather than
reaching the driver, where binding it raises `OverflowError` -- a 500 out of a query string."""

_NAIVE_TIMESTAMP: Final = (
    "a timestamp must carry a timezone offset, for example "
    "2026-09-24T00:00:00Z or 2026-09-24T02:00:00+02:00"
)


def _in_utc(value: datetime) -> datetime:
    """The same instant in UTC, refusing one that UTC cannot represent.

    **`astimezone(UTC)` raises `OverflowError`, not `ValueError`, and Pydantic only turns the
    second into a 422.** `0001-01-01T00:00:00+01:00` is an hour before the first instant a
    `datetime` can hold, and `9999-12-31T23:59:59-01:00` is an hour after the last; both
    parse, both carry an offset, and both reached a client as a 500. Re-raised as the error
    type the validation machinery actually handles.
    """
    try:
        return value.astimezone(UTC)
    except OverflowError:
        message = "the timestamp is outside the range of instants that can be represented in UTC"
        raise ValueError(message) from None


def _require_aware(value: datetime) -> datetime:
    """Refuse a naive `since`, rather than guessing which timezone the client meant.

    The same rule `UtcDateTime` applies at the database boundary, applied at the request
    boundary so the failure is a 422 naming the field instead of a 500 out of a bind
    parameter. Guessing UTC would be the one thing a portfolio must not do with a
    timestamp: it is a time-ordered event log, and an hour of silent error in a filter is
    an hour of history a chart quietly omits.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(_NAIVE_TIMESTAMP)
    return _in_utc(value)


def encode_cursor(cursor: HistoryCursor) -> str:
    """The opaque wire form of a history cursor.

    URL-safe base64, unpadded, of `<observed_at isoformat>|<snapshot id>`. **Opaque by
    convention rather than by cryptography**: nothing here is secret -- it is the position of
    a row the client has just been shown -- and a client that decodes it learns an instant
    and an integer. It is encoded so that nothing is tempted to build one by hand, which is
    the failure that would couple a client to this layout.

    `isoformat()` because it keeps the microseconds, and the keyset compares `observed_at`
    for *equality*: a coarser rendering would make the cursor unequal to its own row and
    skip every reading sharing that instant.
    """
    raw = f"{cursor.observed_at.isoformat()}{_CURSOR_SEPARATOR}{cursor.snapshot_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> HistoryCursor:
    """Read a cursor back, refusing anything `encode_cursor` could not have produced.

    **Every failure is a `ValueError`, and that is the whole contract.** A malformed cursor
    must be a 422, and each of the ways one can be malformed raises something different out
    of the standard library -- `binascii.Error` for a character outside the alphabet,
    `UnicodeDecodeError` for bytes that are not text, `ValueError` from `fromisoformat`,
    `OverflowError` from an extreme offset. The first two are already `ValueError`
    subclasses; the last is not, and it is the one `_in_utc` converts.

    Checked beyond parsing, because parsing alone accepts values that would fail later and
    worse: a naive instant, which `UtcDateTime` refuses at bind time with a 500; an id that
    is not a run of ASCII digits (`int` accepts `"٢"` and `" 7"`); and an id past SQLite's
    `INTEGER` range, which the driver refuses with an `OverflowError`.

    The message never echoes the cursor. It carries no address, but a message built from
    input is the habit that eventually carries one.

    Raises:
        ValueError: the value is not a cursor this endpoint issued.
    """
    malformed = "the cursor is not one this endpoint issued"
    padded = value + "=" * (-len(value) % 4)
    try:
        text = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
    except ValueError:
        raise ValueError(malformed) from None
    parts = text.split(_CURSOR_SEPARATOR)
    # Two parts, an instant and an id, and nothing else.
    if len(parts) != 2:
        raise ValueError(malformed)
    instant_text, id_text = parts
    if not id_text.isascii() or not id_text.isdigit():
        raise ValueError(malformed)
    snapshot_id = int(id_text)
    if not 1 <= snapshot_id <= _MAX_SNAPSHOT_ID:
        raise ValueError(malformed)
    try:
        instant = datetime.fromisoformat(instant_text)
    except ValueError:
        raise ValueError(malformed) from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(malformed)
    return HistoryCursor(observed_at=_in_utc(instant), snapshot_id=snapshot_id)


AwareDatetime = Annotated[datetime, AfterValidator(_require_aware)]
"""A query-string datetime that must say what timezone it is in, normalised to UTC."""


def _as_base_units(value: int) -> str:
    """Render an integer base-unit count for the wire. Always a string; see the module doc."""
    return str(value)


BaseUnitsStr = Annotated[
    int,
    PlainSerializer(_as_base_units, return_type=str),
    WithJsonSchema({"type": "string", "examples": ["123456789"]}),
]
"""An integer count of base units that serializes to a JSON string.

Response-only, so there is no validator: nothing in this API accepts a balance from a
client. If one ever does, it needs the mirror of `MoneyStr`'s `_refuse_a_lossy_number` --
an integer past `Number.MAX_SAFE_INTEGER` arrives from JavaScript already rounded, and
accepting it would launder the damage rather than report it.
"""


class PriceResponse(BaseModel):
    """The price one asset was valued at, and how old it is.

    `stale` is computed at read time against an injected clock and is never stored; `as_of`
    is our own clock at the instant the refresh that wrote the row began, not a vendor quote
    time -- no vendor supplies one. `db.models.AssetPrice` carries the full account.
    """

    amount: MoneyStr
    source: str
    as_of: datetime
    stale: bool

    @classmethod
    def of(cls, price: Price) -> PriceResponse:
        """Render a service snapshot."""
        return cls(amount=price.amount, source=price.source, as_of=price.as_of, stale=price.stale)


class WalletBalanceResponse(BaseModel):
    """One wallet's latest reading, valued if its asset could be priced.

    The address is deliberately **not** here. It is the owner's holdings, the client already
    has it from `GET /api/wallets`, and every field this endpoint adds is one more place it
    could reach a log.
    """

    wallet_id: int
    chain_key: str
    label: str | None
    asset_symbol: str
    confirmed: BaseUnitsStr | None
    pending: BaseUnitsStr | None
    decimals: int | None
    quantity: MoneyStr | None
    value: MoneyStr | None
    price: PriceResponse | None
    observed_at: datetime | None

    @classmethod
    def of(cls, balance: WalletBalance) -> WalletBalanceResponse:
        """Render a service view."""
        return cls(
            wallet_id=balance.wallet_id,
            chain_key=balance.chain_key,
            label=balance.label,
            asset_symbol=balance.asset_symbol,
            confirmed=balance.confirmed,
            pending=balance.pending,
            decimals=balance.decimals,
            quantity=balance.quantity,
            value=balance.value,
            price=None if balance.price is None else PriceResponse.of(balance.price),
            observed_at=balance.observed_at,
        )


class UnpricedHoldingResponse(BaseModel):
    """One asset the portfolio holds and could not value, with the reason.

    The reason is `PriceUnavailable`, so a client can branch on `never_fetched` against
    `every_source_failed` without parsing prose -- the first means the refresh has not run,
    the second means a vendor is down, and they send an operator to look at different things.
    """

    asset_symbol: str
    quantity: MoneyStr
    reason: PriceUnavailable

    @classmethod
    def of(cls, holding: UnpricedHolding) -> UnpricedHoldingResponse:
        """Render a service view."""
        return cls(
            asset_symbol=holding.asset_symbol,
            quantity=holding.quantity,
            reason=holding.reason,
        )


class UnreadWalletResponse(BaseModel):
    """An active wallet no successful run has ever read, so it is missing from `total`.

    The address is deliberately not here: a client matches `wallet_id` against the wallet
    list it already has, and every field this endpoint adds is one more place an address
    could reach a log.
    """

    wallet_id: int
    chain_key: str
    asset_symbol: str

    @classmethod
    def of(cls, wallet: UnreadWallet) -> UnreadWalletResponse:
        """Render a service view."""
        return cls(
            wallet_id=wallet.wallet_id,
            chain_key=wallet.chain_key,
            asset_symbol=wallet.asset_symbol,
        )


class CurrentBalancesResponse(BaseModel):
    """What the portfolio holds now and what it is worth.

    **`total` is the sum of what could be valued and is not the answer on its own.** Read
    without `complete` it silently omits a holding, which is indistinguishable from a number
    that includes it. Every renderer has to look at `complete`, and two lists say what is
    missing: `unpriced` has quantities nothing could value, `unread` has wallets whose
    quantity nobody knows yet. `complete` is true only when both are empty.
    """

    quote_currency: QuoteCurrency
    total: MoneyStr
    complete: bool
    as_of: datetime | None
    wallets: list[WalletBalanceResponse]
    unpriced: list[UnpricedHoldingResponse]
    unread: list[UnreadWalletResponse]

    @classmethod
    def of(cls, view: CurrentBalances) -> CurrentBalancesResponse:
        """Render a service view."""
        return cls(
            quote_currency=view.quote_currency,
            total=view.total,
            complete=view.complete,
            as_of=view.as_of,
            wallets=[WalletBalanceResponse.of(wallet) for wallet in view.wallets],
            unpriced=[UnpricedHoldingResponse.of(holding) for holding in view.unpriced],
            unread=[UnreadWalletResponse.of(wallet) for wallet in view.unread],
        )


class SnapshotResponse(BaseModel):
    """One historical reading of one wallet."""

    observed_at: datetime
    confirmed: BaseUnitsStr
    pending: BaseUnitsStr | None
    quantity: MoneyStr
    sync_run_id: int

    @classmethod
    def of(cls, snapshot: SnapshotView) -> SnapshotResponse:
        """Render a service view."""
        return cls(
            observed_at=snapshot.observed_at,
            confirmed=snapshot.confirmed,
            pending=snapshot.pending,
            quantity=snapshot.quantity,
            sync_run_id=snapshot.sync_run_id,
        )


class WalletHistoryResponse(BaseModel):
    """One wallet's readings, oldest first, and where the next page starts if there is one.

    `decimals` is `null` for a wallet that has never been read: the exponent is a property
    of the readings, and there are none.

    `next_cursor` is non-null exactly when more readings exist forward at the moment of the
    request; pass it back as `cursor` to get them. It is always `null` for the default,
    latest window -- nothing exists forward of the newest reading -- and on the last page of
    a walk forward, which is how a client knows to stop.
    """

    wallet_id: int
    decimals: int | None
    snapshots: list[SnapshotResponse]
    next_cursor: str | None

    @classmethod
    def of(cls, history: WalletHistory) -> WalletHistoryResponse:
        """Render a service view, encoding the cursor into its opaque wire form."""
        return cls(
            wallet_id=history.wallet_id,
            decimals=history.decimals,
            snapshots=[SnapshotResponse.of(snapshot) for snapshot in history.snapshots],
            next_cursor=(
                None if history.next_cursor is None else encode_cursor(history.next_cursor)
            ),
        )


class ChainOutcomeResponse(BaseModel):
    """What one chain did during one run.

    `detail` is the provider's own message, which those providers are written never to quote
    a body, a URL or an address into. For a failure that was **not** a provider's -- an
    exception from our own code -- it is the exception's type name and nothing else, because
    an arbitrary exception has made no such promise.
    """

    chain_key: str
    status: SyncRunStatus
    wallets_read: int
    error_kind: SyncErrorKind | None
    detail: str | None

    @classmethod
    def of(cls, outcome: ChainOutcome) -> ChainOutcomeResponse:
        """Render a service view."""
        return cls(
            chain_key=outcome.chain_key,
            status=outcome.status,
            wallets_read=outcome.wallets_read,
            error_kind=outcome.error_kind,
            detail=outcome.detail,
        )


class SyncRunResponse(BaseModel):
    """One run: when, how long, how many wallets, and what each chain did.

    `finished_at` and `duration_ms` are both `null` for a run still in flight and for one
    that was interrupted; `status` says which. `duration_ms` comes from a monotonic clock
    rather than from `finished_at - started_at`, so a host that synced its clock mid-run
    cannot report a negative one.
    """

    run_id: int
    trigger: SyncTrigger
    status: SyncRunStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    wallets_total: int
    wallets_succeeded: int
    wallets_failed: int
    chains: list[ChainOutcomeResponse]

    @classmethod
    def of(cls, summary: SyncRunSummary) -> SyncRunResponse:
        """Render a service view."""
        return cls(
            run_id=summary.run_id,
            trigger=summary.trigger,
            status=summary.status,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            duration_ms=summary.duration_ms,
            wallets_total=summary.wallets_total,
            wallets_succeeded=summary.wallets_succeeded,
            wallets_failed=summary.wallets_failed,
            chains=[ChainOutcomeResponse.of(chain) for chain in summary.chains],
        )


class SyncTriggeredResponse(SyncRunResponse):
    """A run summary plus whether this request started it or attached to one in flight.

    **`joined` is on this model and not on `SyncRunResponse`**, which is what
    `GET /api/balances/runs` returns. It is a fact about *this call* rather than about the
    run -- the same run is `joined: false` for the caller that started it and `joined: true`
    for everyone who arrived afterwards -- so a historical run has no honest value for it,
    and publishing a hardcoded `false` there would be an answer to a question nobody asked.

    When `joined` is true, `trigger` is the *running* run's trigger and not this caller's: a
    scheduled run that a manual click attached to is still a scheduled run.
    """

    joined: bool

    @classmethod
    def of_outcome(cls, outcome: SyncOutcome) -> SyncTriggeredResponse:
        """Render the coordinator's answer."""
        summary = outcome.summary
        return cls(
            run_id=summary.run_id,
            trigger=summary.trigger,
            status=summary.status,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            duration_ms=summary.duration_ms,
            wallets_total=summary.wallets_total,
            wallets_succeeded=summary.wallets_succeeded,
            wallets_failed=summary.wallets_failed,
            chains=[ChainOutcomeResponse.of(chain) for chain in summary.chains],
            joined=outcome.joined,
        )


class SyncRunListResponse(BaseModel):
    """The run log, wrapped in an object rather than returned as a bare array.

    A top-level array has nowhere to grow: adding a count or a cursor later would break every
    client, and an object costs one key now. The same shape `WalletListResponse` uses.
    """

    runs: list[SyncRunResponse]
