"""Reads and writes of `exchange_sync_runs` and `exchange_sync_run_accounts`, and their vocabulary.

The exchange counterpart of `repositories/sync_runs.py`, and the vocabulary lives here for the
reason that module gives: `services/exchange_sync.py` fills these types in and is the one
module in `services/` that imports an exchange provider, while `services/exchanges.py` -- the
read side, which a router imports -- renders them. Defined beside the sync, they would put the
module that holds `Credentials` one import away from every request path, which the
`api-never-reaches-an-exchange-provider` contract forbids.

`SyncTrigger` and `SyncRunStatus` are **reused, not redefined**: `exchange_sync_runs` carries
the same `CHECK` constants as `sync_runs`, and one spelling of "how a run started and ended"
is what keeps the two logs comparable.

Queries and nothing else. The repository does not commit.

## Nothing here is money, and no datetime is ordered in SQL

Every number is a count or a duration. Runs are listed newest first by **`id`**, an `INTEGER`,
rather than by `started_at`: one coordinator allows one run at a time in this process, so
identity order is start order, and an integer sort needs no argument about how a `TEXT`
datetime collates. The fill totals of a run are the sums of its accounts' counts, computed in
Python when the run is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from portfolio.db.models import ExchangeAccount, ExchangeSyncRun, ExchangeSyncRunAccount
from portfolio.domain.exchanges import ExchangeKey
from portfolio.repositories.sync_runs import SyncRunStatus, SyncTrigger

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AccountOutcome",
    "AccountOutcomeStatus",
    "ExchangeSyncErrorKind",
    "ExchangeSyncRunRepository",
    "ExchangeSyncRunSummary",
    "SyncRunStatus",
    "SyncTrigger",
]


class AccountOutcomeStatus(StrEnum):
    """What one account did in one run. Mirrored by `ck_exchange_sync_run_accounts_status`.

    `SKIPPED` is an `auth_failed` account a scheduled or startup run did not attempt. It is
    not a failure and not a success, and the run's own status is computed without it.
    """

    FAILED = "failed"
    SKIPPED = "skipped"
    SUCCESS = "success"


class ExchangeSyncErrorKind(StrEnum):
    """Why an account's sync failed, as a value the owner can act on.

    Seven are the exchange error classes, subclass and parent kept apart where the remedy
    differs: `INSUFFICIENT_SCOPE` and `AUTH` both mean "edit the key", and #16 says which edit;
    `RETENTION_WINDOW` is the venue refusing a window as too old after the sync ran out of
    steps. `CONFLICT` is a re-read fill that differs from the stored one. `INTERNAL` is a
    defect of ours, kept apart so that a parser bug is never read as an outage at the venue.
    Mirrored by `ck_exchange_sync_run_accounts_error_kind`, alphabetical like it.
    """

    AUTH = "auth"
    CONFLICT = "conflict"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    INTERNAL = "internal"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    RETENTION_WINDOW = "retention_window"
    SCHEMA = "schema"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AccountOutcome:
    """What one account did during one run, and why it failed if it did.

    The counters are this run's: `windows_completed` is windows whose last page was read,
    `pages` every page fetched (a page that was split rather than recorded included),
    `fills_seen` every fill handed to the insert, and `fills_inserted` the ones that were new.
    A failed account keeps the counts of the pages it committed before failing, because
    those fills are stored.

    `detail` never carries a trade id, a cursor, a symbol or an amount -- see
    `ExchangeSyncRunAccount`.
    """

    exchange_account_id: int
    exchange_key: ExchangeKey
    status: AccountOutcomeStatus
    windows_completed: int = 0
    pages: int = 0
    fills_seen: int = 0
    fills_inserted: int = 0
    error_kind: ExchangeSyncErrorKind | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ExchangeSyncRunSummary:
    """One exchange sync run as everything above this layer sees it, accounts included.

    A frozen snapshot rather than the ORM rows, for the reason `SyncRunSummary` is one.
    `finished_at` and `duration_ms` are `None` for a run in flight and for an interrupted one;
    `status` says which. `accounts` are sorted by `exchange_key`.
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
    accounts: tuple[AccountOutcome, ...]

    @property
    def fills_seen(self) -> int:
        """Every fill this run handed to an insert, summed over its accounts in Python."""
        return sum(account.fills_seen for account in self.accounts)

    @property
    def fills_inserted(self) -> int:
        """Every fill this run stored for the first time, summed over its accounts in Python."""
        return sum(account.fills_inserted for account in self.accounts)


def _outcome_of(row: ExchangeSyncRunAccount, exchange_key: str) -> AccountOutcome:
    """Turn an outcome row back into the vocabulary. A value the `CHECK`s refuse raises."""
    return AccountOutcome(
        exchange_account_id=row.exchange_account_id,
        exchange_key=ExchangeKey(exchange_key),
        status=AccountOutcomeStatus(row.status),
        windows_completed=row.windows_completed,
        pages=row.pages,
        fills_seen=row.fills_seen,
        fills_inserted=row.fills_inserted,
        error_kind=None if row.error_kind is None else ExchangeSyncErrorKind(row.error_kind),
        detail=row.detail,
    )


class ExchangeSyncRunRepository:
    """Every query this application makes against the exchange run log."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open_run(
        self,
        *,
        trigger: SyncTrigger,
        started_at: datetime,
        accounts_total: int,
    ) -> int:
        """Insert the run at `running`, with no end yet. Returns its id.

        Flushes rather than commits; the sync commits immediately, before any venue is called,
        for the reason `SyncRunRepository.open_run` gives. An id rather than the row, because
        the sync rolls back failed pages and a rollback expires the row.
        """
        run = ExchangeSyncRun(
            trigger=trigger,
            status=SyncRunStatus.RUNNING,
            started_at=started_at,
            finished_at=None,
            duration_ms=None,
            accounts_total=accounts_total,
            accounts_succeeded=0,
            accounts_failed=0,
            accounts_skipped=0,
        )
        self._session.add(run)
        await self._session.flush()
        return run.id

    async def record_outcome(self, run_id: int, outcome: AccountOutcome) -> None:
        """Write one account's outcome. The sync commits it with the account's status."""
        self._session.add(
            ExchangeSyncRunAccount(
                exchange_sync_run_id=run_id,
                exchange_account_id=outcome.exchange_account_id,
                status=outcome.status,
                windows_completed=outcome.windows_completed,
                pages=outcome.pages,
                fills_seen=outcome.fills_seen,
                fills_inserted=outcome.fills_inserted,
                error_kind=outcome.error_kind,
                detail=outcome.detail,
            )
        )
        await self._session.flush()

    async def finish_run(
        self,
        run_id: int,
        *,
        status: SyncRunStatus,
        finished_at: datetime,
        duration_ms: int,
        accounts_total: int,
        accounts_succeeded: int,
        accounts_failed: int,
        accounts_skipped: int,
    ) -> None:
        """Close the run out. An `UPDATE` by id, for the reason `open_run` returns an id.

        `accounts_total` is written again because it can change after the run opened: a run
        that finds no owner, or two, attempts nothing and records zero.
        """
        await self._session.execute(
            update(ExchangeSyncRun)
            .where(ExchangeSyncRun.id == run_id)
            .values(
                status=status,
                finished_at=finished_at,
                duration_ms=duration_ms,
                accounts_total=accounts_total,
                accounts_succeeded=accounts_succeeded,
                accounts_failed=accounts_failed,
                accounts_skipped=accounts_skipped,
            )
        )

    async def sweep_interrupted(self) -> int:
        """Mark every run still at `running` as `interrupted`. Returns how many.

        `finished_at` and `duration_ms` stay `NULL`. Safe only because one instance of this
        application runs; see `SyncRunRepository.sweep_interrupted`, whose two-statement shape
        this follows for the same typing reason.
        """
        running = list(
            await self._session.scalars(
                select(ExchangeSyncRun.id).where(ExchangeSyncRun.status == SyncRunStatus.RUNNING)
            )
        )
        if not running:
            return 0
        await self._session.execute(
            update(ExchangeSyncRun)
            .where(ExchangeSyncRun.id.in_(running))
            .values(status=SyncRunStatus.INTERRUPTED)
        )
        return len(running)

    async def latest_started_at(self) -> datetime | None:
        """When the newest run of **any** status started, or `None` if none ever has.

        The exchange timer's "last run" is an attempt, not a success, for the crash-loop
        reason `SyncRunRepository.latest_started_at` gives: a container that dies mid-sync
        must not sync again against the venue on every restart.
        """
        found: datetime | None = await self._session.scalar(
            select(ExchangeSyncRun.started_at).order_by(ExchangeSyncRun.id.desc()).limit(1)
        )
        return found

    async def list_runs(self, *, limit: int) -> list[ExchangeSyncRunSummary]:
        """The most recent runs, newest first by id, each with its accounts by `exchange_key`.

        Two queries whatever the page size: the runs, then every outcome belonging to them
        joined to its account for the venue's key.
        """
        runs = list(
            await self._session.scalars(
                select(ExchangeSyncRun).order_by(ExchangeSyncRun.id.desc()).limit(limit)
            )
        )
        if not runs:
            return []
        outcomes: dict[int, list[AccountOutcome]] = {run.id: [] for run in runs}
        rows = await self._session.execute(
            select(ExchangeSyncRunAccount, ExchangeAccount.exchange_key)
            .join(
                ExchangeAccount,
                ExchangeAccount.id == ExchangeSyncRunAccount.exchange_account_id,
            )
            .where(ExchangeSyncRunAccount.exchange_sync_run_id.in_(outcomes))
            .order_by(ExchangeSyncRunAccount.exchange_sync_run_id, ExchangeAccount.exchange_key)
        )
        for row, exchange_key in rows.tuples():
            outcomes[row.exchange_sync_run_id].append(_outcome_of(row, exchange_key))
        return [
            ExchangeSyncRunSummary(
                run_id=run.id,
                trigger=SyncTrigger(run.trigger),
                status=SyncRunStatus(run.status),
                started_at=run.started_at,
                finished_at=run.finished_at,
                duration_ms=run.duration_ms,
                accounts_total=run.accounts_total,
                accounts_succeeded=run.accounts_succeeded,
                accounts_failed=run.accounts_failed,
                accounts_skipped=run.accounts_skipped,
                accounts=tuple(outcomes[run.id]),
            )
            for run in runs
        ]

    async def latest_attempted_outcome(self, exchange_account_id: int) -> AccountOutcome | None:
        """The account's newest outcome that is **not** `skipped`, or `None`.

        What `last_error` is read from. A skipped outcome says nothing about the account --
        the run did not ask the venue anything -- so the error that put the account in
        `auth_failed` stays visible under every scheduled run that skips it afterwards.
        Newest by `id`, for the reason `list_runs` orders by it.
        """
        found = await self._session.execute(
            select(ExchangeSyncRunAccount, ExchangeAccount.exchange_key)
            .join(
                ExchangeAccount,
                ExchangeAccount.id == ExchangeSyncRunAccount.exchange_account_id,
            )
            .where(
                ExchangeSyncRunAccount.exchange_account_id == exchange_account_id,
                ExchangeSyncRunAccount.status != AccountOutcomeStatus.SKIPPED,
            )
            .order_by(ExchangeSyncRunAccount.id.desc())
            .limit(1)
        )
        row = found.tuples().first()
        if row is None:
            return None
        outcome, exchange_key = row
        return _outcome_of(outcome, exchange_key)
