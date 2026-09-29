"""Reading exchange accounts and the exchange run log back. **No provider here, by contract.**

The read side of #15, and what `api/routers/exchanges.py` imports. It imports repositories
and the domain vocabulary and nothing from `providers`, and `backend/.importlinter`'s
`api-never-reaches-an-exchange-provider` contract makes that structural: no module under
`portfolio.api` may import `portfolio.providers.exchanges`, directly or through anything
else. So no request path can reach the module that holds `Credentials`, except through the
coordinator `main.py` wired -- which is why `ExchangeSyncRunSummary` and its vocabulary live
in `repositories/exchange_sync_runs.py` rather than beside the sync that fills them in.

## `configured` is the whole disclosure about credentials

The lifespan publishes `configured_exchanges`, a `frozenset[ExchangeKey]` built from the keys
of the provider mapping, and that set is the only thing this module learns about
credentials. `configured` is `exchange_key in configured_exchanges`. No view here has a field
that could carry a key, a secret or a passphrase, and a test walks the OpenAPI document to
prove no response model does either.

## `history_truncated` is derived, never stored

`effective_since > requested_since`, and `False` when either is unknown. It compares two
aware datetimes in Python, as every datetime comparison in this application does.

## The transactions view filters, orders and totals in Python (#93)

`list_fills` reads every fill of the owner's accounts on the selected venues -- the one filter
applied in SQL, on an enum column -- and then, here: keeps the half-open range `[from_, to)`,
orders newest first by `executed_at` with ties broken by id descending, totals the **whole**
filtered set with `domain.fill_totals.total_fills`, and only then slices the page. So the
totals are the same whatever `limit` and `offset` are, and no datetime or amount is compared,
ordered or summed in SQL, where both are text. Spec 024 records what that costs at 5,000,
20,000 and 50,000 fills.

**The range's rules are here, not in the router**: a bound must be timezone-aware and
representable in UTC, and `from_` must be before `to`. A refusal is `InvalidFillRangeError`,
naming the parameter and the rule and never the value, which the router turns into a 422.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Final, Literal

from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey, FillSide
from portfolio.domain.fill_totals import FillLine, FillTotals, total_fills, usdt_value
from portfolio.repositories.exchange_sync_runs import (
    AccountOutcome,
    AccountOutcomeStatus,
    ExchangeSyncErrorKind,
    ExchangeSyncRunRepository,
    ExchangeSyncRunSummary,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.repositories.exchanges import (
    ExchangeAccountRepository,
    ExchangeFillRepository,
    ExchangeSyncWindowRepository,
)

if TYPE_CHECKING:
    from collections.abc import Collection
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.repositories.exchanges import ExchangeAccountState, FillViewRecord
    from portfolio.services.auth import Principal

__all__ = [
    "DEFAULT_EXCHANGE_RUNS_LIMIT",
    "DEFAULT_FILLS_LIMIT",
    "INVERTED_RANGE_RULE",
    "MAX_EXCHANGE_RUNS_LIMIT",
    "MAX_FILLS_LIMIT",
    "NAIVE_BOUND_RULE",
    "UNREPRESENTABLE_BOUND_RULE",
    "AccountOutcome",
    "AccountOutcomeStatus",
    "AccountSyncStatus",
    "ExchangeKey",
    "ExchangeService",
    "ExchangeSyncErrorKind",
    "ExchangeSyncRunSummary",
    "ExchangeView",
    "FillSide",
    "FillView",
    "FillsPage",
    "InvalidFillRangeError",
    "LastError",
    "SyncRunStatus",
    "SyncTrigger",
    "build_exchange_service",
    "history_truncated",
]
"""The run vocabulary and the domain enums are **re-exported** for `api/schemas/exchanges.py`,
which may not import `portfolio.repositories` -- the reason `services/balances.py` re-exports
its run vocabulary."""

DEFAULT_EXCHANGE_RUNS_LIMIT: Final = 20
MAX_EXCHANGE_RUNS_LIMIT: Final = 100
"""`GET /api/exchanges/runs` page sizes. The schema refuses anything outside `1..100` with a
422; `list_runs` clamps as well, for a caller that does not go through the schema."""

DEFAULT_FILLS_LIMIT: Final = 50
MAX_FILLS_LIMIT: Final = 200
"""`GET /api/exchanges/fills` page sizes. The schema refuses anything outside `1..200` with a
422; `list_fills` clamps as well, for a caller that does not go through the schema."""

FillRangeField = Literal["from", "to"]
"""The query parameter a range refusal is about, spelled as the API spells it."""

NAIVE_BOUND_RULE: Final = (
    "must carry a timezone offset, such as 2026-03-01T00:00:00Z or 2026-03-01T01:00:00+01:00; "
    "a datetime without one is refused rather than assumed to be UTC"
)
"""Why a naive bound is refused. The message is the parameter's name, a space, and this."""

UNREPRESENTABLE_BOUND_RULE: Final = "is outside the range of instants UTC can represent"
"""Why a bound whose offset carries it past `datetime`'s range is refused. Prefixed like
`NAIVE_BOUND_RULE`."""

INVERTED_RANGE_RULE: Final = (
    "to must be later than from: from is inclusive and to is exclusive, so this range is empty"
)
"""Why `from >= to` is refused. The whole message, reported against `to`."""


class InvalidFillRangeError(ValueError):
    """A date bound of the transactions view that the service refuses.

    `field` is the query parameter it is about, `from` or `to`, and `rule` the whole sentence:
    it names the parameter and what it must be, and **never quotes the value**.
    """

    def __init__(self, field: FillRangeField, rule: str) -> None:
        """Carry the parameter and the rule it broke."""
        self.field: FillRangeField = field
        self.rule = rule
        super().__init__(rule)


@dataclass(frozen=True, slots=True)
class FillView:
    """One fill as `GET /api/exchanges/fills` renders it.

    `id` is our row id, a stable key, and never the venue's trade id, which this view does not
    load. `order_id` is the venue's order id, which the owner needs to find the trade at the
    venue, or `None` when it sent none. `usdt_value` is `quote_quantity` for a USDT-quoted
    fill and `None` otherwise (`domain.fill_totals.usdt_value`).
    """

    id: int
    executed_at: datetime
    exchange_key: ExchangeKey
    symbol: str
    base_asset: str
    quote_asset: str
    side: FillSide
    quantity: Decimal
    price: Decimal
    quote_quantity: Decimal
    quote_quantity_derived: bool
    usdt_value: Decimal | None
    fee_amount: Decimal
    fee_asset: str | None
    order_id: str | None


@dataclass(frozen=True, slots=True)
class FillsPage:
    """One page of the filtered fills, how many matched, and the totals over all of them."""

    fills: tuple[FillView, ...]
    total_count: int
    totals: FillTotals


def _bound(field: FillRangeField, value: datetime | None) -> datetime | None:
    """A range bound in UTC, or `None`, refusing a naive one and one UTC cannot represent.

    `astimezone(UTC)` raises `OverflowError` for `0001-01-01T00:00:00+01:00`, an hour before
    the first instant a `datetime` holds; that is re-raised as the refusal it is, since only a
    `ValueError` becomes a 422.
    """
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidFillRangeError(field, f"{field} {NAIVE_BOUND_RULE}")
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise InvalidFillRangeError(field, f"{field} {UNREPRESENTABLE_BOUND_RULE}") from None


def _line_of(record: FillViewRecord) -> FillLine:
    """The fields of a stored fill that its totals need."""
    return FillLine(
        base_asset=record.base_asset,
        quote_asset=record.quote_asset,
        side=record.side,
        quantity=record.quantity,
        quote_quantity=record.quote_quantity,
        fee_amount=record.fee_amount,
        fee_asset=record.fee_asset,
    )


def _view_of(record: FillViewRecord) -> FillView:
    """A stored fill as the view renders it."""
    return FillView(
        id=record.id,
        executed_at=record.executed_at,
        exchange_key=record.exchange_key,
        symbol=record.symbol,
        base_asset=record.base_asset,
        quote_asset=record.quote_asset,
        side=record.side,
        quantity=record.quantity,
        price=record.price,
        quote_quantity=record.quote_quantity,
        quote_quantity_derived=record.quote_quantity_derived,
        usdt_value=usdt_value(record.quote_asset, record.quote_quantity),
        fee_amount=record.fee_amount,
        fee_asset=record.fee_asset,
        order_id=record.external_order_id,
    )


def _newest_first(record: FillViewRecord) -> tuple[datetime, int]:
    """The view's sort key, applied in reverse: `executed_at`, then id."""
    return (record.executed_at, record.id)


@dataclass(frozen=True, slots=True)
class LastError:
    """Why the account's latest attempted sync failed: the kind, and the recorded detail."""

    error_kind: ExchangeSyncErrorKind
    detail: str | None


@dataclass(frozen=True, slots=True)
class ExchangeView:
    """One venue as `GET /api/exchanges` renders it.

    A configured venue with no account row yet -- nothing has run since its credentials were
    set -- is `never_synced` with every instant `None` and every count zero. A venue with a
    row and no credentials any more is listed with `configured=False` and its last state.
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
    last_error: LastError | None


def history_truncated(
    requested_since: datetime | None,
    effective_since: datetime | None,
) -> bool:
    """Whether the history held starts later than the owner asked. `False` if either is unknown.

    Strictly later: an effective start equal to the requested one is the whole request.
    """
    if requested_since is None or effective_since is None:
        return False
    return effective_since > requested_since


def _last_error_of(outcome: AccountOutcome | None) -> LastError | None:
    """The error of the latest attempted outcome, if that outcome failed.

    A failed outcome always carries a kind when this application wrote it; one without is a
    hand edit, and reporting no error is better than inventing one.
    """
    if (
        outcome is None
        or outcome.status is not AccountOutcomeStatus.FAILED
        or outcome.error_kind is None
    ):
        return None
    return LastError(error_kind=outcome.error_kind, detail=outcome.detail)


class ExchangeService:
    """Builds the account list and the run log. Read-only: the session is never committed."""

    def __init__(
        self,
        *,
        accounts: ExchangeAccountRepository,
        windows: ExchangeSyncWindowRepository,
        fills: ExchangeFillRepository,
        runs: ExchangeSyncRunRepository,
        configured: frozenset[ExchangeKey],
        syncing: bool,
    ) -> None:
        self._accounts = accounts
        self._windows = windows
        self._fills = fills
        self._runs = runs
        self._configured = configured
        self._syncing = syncing

    async def list_exchanges(self, principal: Principal) -> list[ExchangeView]:
        """Every configured venue plus every venue the caller has an account row for.

        Sorted by `exchange_key`. Empty when nothing is configured and nothing was ever
        synced, which is #16's empty state.

        `syncing` is `True` for a configured venue while an exchange sync is in flight: the
        run covers every configured venue, and one without credentials is not in it.
        """
        rows = {
            state.exchange_key: state
            for state in await self._accounts.list_for_user(principal.user_id)
        }
        keys = sorted(self._configured | rows.keys())
        return [await self._view(key, rows.get(key)) for key in keys]

    async def _view(
        self, exchange_key: ExchangeKey, state: ExchangeAccountState | None
    ) -> ExchangeView:
        """One venue's view, from its account row if it has one."""
        configured = exchange_key in self._configured
        syncing = self._syncing and configured
        if state is None:
            return ExchangeView(
                exchange_key=exchange_key,
                configured=configured,
                status=AccountSyncStatus.NEVER_SYNCED,
                syncing=syncing,
                requested_since=None,
                effective_since=None,
                history_truncated=False,
                last_synced_at=None,
                fills_stored=0,
                pending_windows=0,
                last_error=None,
            )
        return ExchangeView(
            exchange_key=exchange_key,
            configured=configured,
            status=state.sync_status,
            syncing=syncing,
            requested_since=state.requested_since,
            effective_since=state.effective_since,
            history_truncated=history_truncated(state.requested_since, state.effective_since),
            last_synced_at=state.last_synced_at,
            fills_stored=await self._fills.count_for_account(state.id),
            pending_windows=await self._windows.count_for_account(state.id),
            last_error=_last_error_of(await self._runs.latest_attempted_outcome(state.id)),
        )

    async def list_runs(self, *, limit: int) -> list[ExchangeSyncRunSummary]:
        """The most recent exchange sync runs, newest first, `limit` clamped to `1..100`."""
        bounded = max(1, min(limit, MAX_EXCHANGE_RUNS_LIMIT))
        return await self._runs.list_runs(limit=bounded)

    async def list_fills(
        self,
        user_id: int,
        *,
        exchanges: Collection[ExchangeKey] | None,
        from_: datetime | None,
        to: datetime | None,
        limit: int,
        offset: int,
    ) -> FillsPage:
        """One page of the owner's fills on `exchanges` in `[from_, to)`, and the totals of all.

        * `exchanges` is `None` for every venue; a repeated venue counts once, and an empty
          collection selects none.
        * `from_` is inclusive and `to` exclusive, each optional, so a fill exactly on a
          boundary lands in exactly one of two adjacent ranges.
        * The page is newest first by `executed_at`, ties broken by id descending. `limit` is
          clamped to `1..200` and `offset` to `0..`; an offset past the end is an empty page
          with the same `total_count` and totals.

        The bounds are checked before anything is read. See the module docstring for why the
        range, the order and the totals are applied here rather than in SQL.

        Raises:
            InvalidFillRangeError: a bound is naive or outside what UTC can represent, or
                `from_` is not before `to`.
        """
        since = _bound("from", from_)
        until = _bound("to", to)
        if since is not None and until is not None and since >= until:
            raise InvalidFillRangeError("to", INVERTED_RANGE_RULE)
        records = await self._fills.list_fills_for_view(
            user_id, None if exchanges is None else frozenset(exchanges)
        )
        selected = [
            record
            for record in records
            if (since is None or record.executed_at >= since)
            and (until is None or record.executed_at < until)
        ]
        selected.sort(key=_newest_first, reverse=True)
        totals = total_fills(_line_of(record) for record in selected)
        start = max(0, offset)
        page = selected[start : start + max(1, min(limit, MAX_FILLS_LIMIT))]
        return FillsPage(
            fills=tuple(_view_of(record) for record in page),
            total_count=len(selected),
            totals=totals,
        )


def build_exchange_service(
    session: AsyncSession,
    *,
    configured: frozenset[ExchangeKey],
    syncing: bool,
) -> ExchangeService:
    """Assemble the read side over one session.

    `configured` is the lifespan's `configured_exchanges`; `syncing` is the exchange
    coordinator's `in_flight` at the moment the request was served.
    """
    return ExchangeService(
        accounts=ExchangeAccountRepository(session),
        windows=ExchangeSyncWindowRepository(session),
        fills=ExchangeFillRepository(session),
        runs=ExchangeSyncRunRepository(session),
        configured=configured,
        syncing=syncing,
    )
