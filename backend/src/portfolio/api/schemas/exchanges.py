"""Response models for the exchange endpoints.

**No field here can carry a credential, and none is named like one.** `configured` is the
whole disclosure: whether the process has credentials for a venue, never what they are. No
model has a field whose name contains `key` (other than `exchange_key`), `secret`,
`passphrase`, `credential`, `token` or `signature`, and a test walks the OpenAPI document to
hold that.

**No monetary field crosses this API.** Fill counts are counts; the fills themselves are M4's
to read. Datetimes are ISO 8601 with an offset, as every datetime this API serves.

**No trade id, cursor or symbol either.** `detail` is the recorded outcome detail -- an
exchange error's fixed summary with a status and a digits-only venue code, a conflict's count,
or an exception's type name -- and nothing else is text a venue sent.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

# Runtime imports: Pydantic resolves an enum field's type at class-creation time. Taken from
# the read-side service, which re-exports them, because the API layer may not import
# `portfolio.repositories`.
from portfolio.services.exchanges import (
    AccountOutcomeStatus,
    AccountSyncStatus,
    ExchangeKey,
    ExchangeSyncErrorKind,
    SyncRunStatus,
    SyncTrigger,
)

if TYPE_CHECKING:
    from portfolio.services.exchanges import (
        AccountOutcome,
        ExchangeSyncRunSummary,
        ExchangeView,
        LastError,
    )
    from portfolio.services.sync_coordinator import SyncOutcome


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
