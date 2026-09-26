"""Importing spot fills from every configured venue: the one `services/` module that calls one.

The write side of #15. `services/exchanges.py`, the read side a router imports, imports no
provider, and the `api-never-reaches-an-exchange-provider` import contract keeps it that way:
the module that holds `Credentials` is reachable from a request only through the coordinator
`main.py` wired. (`services/exchange_sync_plan.py` imports `providers.exchanges.base` for
`FillWindow`, a value type, and calls nothing.)

## The shape of a run

```
sweep interrupted runs -> open the run (committed)
-> find the owner -> ensure one account row per configured venue (committed)
-> for each account, by exchange_key, sequentially:
     skip if auth_failed and the trigger is not manual (outcome committed)
     clamp -> normalise the pending queue -> plan new windows (committed)
     for each pending window, newest first:
         page loop: fetch -> insert fills + advance or delete the window (one commit per page)
     status ok + last_synced_at + outcome (committed)
-> close the run (committed)
```

Accounts run **sequentially over one session**. There are at most two venues, an
`AsyncSession` is not safe for concurrent use, and a sequential loop needs none of the
gather-then-write split `balance_sync.py` needed.

## The page is the transaction, and the queue is the checkpoint

Each page's fills are inserted and its window's cursor advanced -- or the window row deleted,
when the venue has nothing after the page -- **in one commit**. A crash before it loses that
page and nothing else; the window is re-read from the last committed cursor, and the unique
constraint makes the re-read insert nothing. A crash after it has already moved the cursor.

## No network call inside an open write transaction

SQLite has one write lock, held from a transaction's first write to its commit. A venue call
made after a write and before the commit -- with the rate-limit sleeps around it -- would hold
that lock for as long as the venue took, and a concurrent login or the balance sync would fail
with "database is locked". So every commit here closes the writes before the next venue call:
a page is fetched, then written and committed; a plan is computed and the venue asked for its
symbols, then every planning write is made and committed at once.

## Failure isolation is per account, and `Exception` is the boundary

`_sync_account` catches `Exception`, rolls back the page in flight, records the account's
status and outcome in one commit, and the next account runs. Everything committed before the
failure stays. **A `BaseException` that is not an `Exception` -- a cancellation, the process
being told to stop -- is not caught**: it is not a recorded failure, no outcome is written,
and the run row stays `running` for the sweep, which is the one honest record of a run the
process did not live to finish.

## What reaches a log, a column and a response

Counts, `exchange_key`, `error_kind` and the exception's type name. **Never a trade id, a
cursor (a trade id at Bitget), a symbol or an amount.** A stored `detail` is `str()` of an
exchange error -- a class summary, a status and a digits-only venue code, by construction --
the fixed count-only message of a `FillConflictError`, or, for anything else, the type name
alone: an arbitrary exception has made no promise about its message.

## `float` is banned here, so the waits are whole seconds

`seconds_to_wait` rounds up to an `int`, and `sleep` takes whole seconds -- the scheduler's
own `sleep_seconds` by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Final

import structlog

from portfolio.domain.exchanges import AccountSyncStatus
from portfolio.providers.exchanges.base import (
    CursorKind,
    FillWindow,
    clamp_to_retention,
    floor_to_millisecond,
)
from portfolio.providers.exchanges.errors import (
    ExchangeAuthError,
    ExchangeInsufficientScopeError,
    ExchangeInvalidRequestError,
    ExchangeRateLimitedError,
    ExchangeRetentionWindowError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
)
from portfolio.providers.http import monotonic_ms
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
    FillConflictError,
)
from portfolio.repositories.users import UserRepository
from portfolio.services.exchange_sync_plan import (
    HISTORY_GENESIS,
    MAX_RETENTION_STEPS,
    RATE_LIMIT_RETRIES,
    RETENTION_STEP,
    PendingWindow,
    cursor_survives_a_moved_since,
    normalise_pending,
    plan_account,
    seconds_to_wait,
    split_in_half,
)
from portfolio.services.scheduler import sleep_seconds

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.domain.exchanges import ExchangeKey
    from portfolio.providers.exchanges.base import (
        ExchangeCapabilities,
        ExchangeProvider,
        FillPage,
        RetentionClamp,
    )
    from portfolio.repositories.exchanges import ExchangeAccountState, SyncWindowRow

__all__ = [
    "AccountFailure",
    "ExchangeSyncService",
    "Sleep",
    "build_exchange_sync_service",
    "failure_of",
    "requested_since_for",
    "utc_now",
]

_logger = structlog.get_logger(__name__)

type Sleep = Callable[[int], Awaitable[None]]
"""How the sync waits out a rate limit, in whole seconds. Injected so no test waits."""

_CURSOR_CYCLE_DETAIL: Final = "the venue's cursor returned to one already visited"
_UNSPLITTABLE_DETAIL: Final = (
    "a window shorter than two milliseconds still filled a whole page, so it cannot be split"
)


def utc_now() -> datetime:
    """The wall clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


def requested_since_for(history_start: date | None) -> datetime:
    """What the owner asked for: the configured date at 00:00 UTC, or `HISTORY_GENESIS`."""
    if history_start is None:
        return HISTORY_GENESIS
    return datetime.combine(history_start, time.min, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class AccountFailure:
    """How one account's failure is recorded: its kind, the status it leaves, the detail."""

    error_kind: ExchangeSyncErrorKind
    status: AccountSyncStatus
    detail: str


# Ordered, and the order is the content: a subclass comes before its parent, the way
# `balance_sync._PROVIDER_ERROR_KINDS` is ordered. Scope before auth, retention before invalid
# request. `ExchangeRateLimitedError` is not an `ExchangeUnavailableError` -- both are
# `ProviderUnavailableError`s -- but it is listed first anyway, so that reordering the
# hierarchy one day cannot swallow it.
_FAILURE_KINDS: Final[
    tuple[tuple[type[Exception], ExchangeSyncErrorKind, AccountSyncStatus], ...]
] = (
    (
        ExchangeInsufficientScopeError,
        ExchangeSyncErrorKind.INSUFFICIENT_SCOPE,
        AccountSyncStatus.AUTH_FAILED,
    ),
    (ExchangeAuthError, ExchangeSyncErrorKind.AUTH, AccountSyncStatus.AUTH_FAILED),
    (ExchangeRateLimitedError, ExchangeSyncErrorKind.RATE_LIMITED, AccountSyncStatus.ERROR),
    (
        ExchangeRetentionWindowError,
        ExchangeSyncErrorKind.RETENTION_WINDOW,
        AccountSyncStatus.ERROR,
    ),
    (ExchangeUnavailableError, ExchangeSyncErrorKind.UNAVAILABLE, AccountSyncStatus.ERROR),
    (ExchangeInvalidRequestError, ExchangeSyncErrorKind.INVALID_REQUEST, AccountSyncStatus.ERROR),
    (ExchangeSchemaError, ExchangeSyncErrorKind.SCHEMA, AccountSyncStatus.ERROR),
    (FillConflictError, ExchangeSyncErrorKind.CONFLICT, AccountSyncStatus.ERROR),
)


def failure_of(error: Exception) -> AccountFailure:
    """How a failure out of one account's sync is recorded, by its type.

    Each exchange error class is its own kind; both auth classes leave the account
    `auth_failed`, everything else `error`. **Anything else is `internal`** -- ours -- and its
    detail is the type name only. That includes an `ExchangeError` that is none of the seven:
    an eighth class added without a line here is our omission, not the venue's.

    The detail of an exchange error is its `str()`, safe by construction: every class but
    `ExchangeSchemaError` renders a fixed summary, a status and a digits-only venue code, and
    a schema error's detail names a field and a rule. A `FillConflictError`'s is a count.
    """
    for error_type, kind, status in _FAILURE_KINDS:
        if isinstance(error, error_type):
            return AccountFailure(error_kind=kind, status=status, detail=str(error))
    return AccountFailure(
        error_kind=ExchangeSyncErrorKind.INTERNAL,
        status=AccountSyncStatus.ERROR,
        detail=type(error).__name__,
    )


@dataclass(slots=True)
class _Progress:
    """This run's counters for one account. Mutable: the page loop adds to it as it commits.

    Incremented only after a page's commit, so a failed account reports what it stored.
    """

    windows_completed: int = 0
    pages: int = 0
    fills_seen: int = 0
    fills_inserted: int = 0

    def outcome(
        self,
        account: ExchangeAccountState,
        status: AccountOutcomeStatus,
        failure: AccountFailure | None = None,
    ) -> AccountOutcome:
        """The account's outcome, with these counters and the failure, if any."""
        return AccountOutcome(
            exchange_account_id=account.id,
            exchange_key=account.exchange_key,
            status=status,
            windows_completed=self.windows_completed,
            pages=self.pages,
            fills_seen=self.fills_seen,
            fills_inserted=self.fills_inserted,
            error_kind=None if failure is None else failure.error_kind,
            detail=None if failure is None else failure.detail,
        )


@dataclass(slots=True)
class _RetentionRecovery:
    """This run's retention recovery, per window id: re-clamped already, and steps spent.

    Mutable, and scoped to one account in one run: the limits are "once per window per run"
    and "`MAX_RETENTION_STEPS` per window per run". Keyed by id, which a move keeps.
    """

    reclamped: set[int] = field(default_factory=set)
    steps: dict[int, int] = field(default_factory=dict)


class ExchangeSyncService:
    """Imports every configured venue's fills and records what happened. Owns the transaction.

    The caller -- the coordinator's runner in `main.py` -- owns the session and closes it, and
    **the session must not be a request's**, for the reason `BalanceSyncService` gives: a
    manual sync is joined rather than refused, so a run outlives the request that started it.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        accounts: ExchangeAccountRepository,
        windows: ExchangeSyncWindowRepository,
        fills: ExchangeFillRepository,
        runs: ExchangeSyncRunRepository,
        providers: Mapping[ExchangeKey, ExchangeProvider],
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], int] = monotonic_ms,
        sleep: Sleep = sleep_seconds,
        history_start: date | None = None,
    ) -> None:
        self._session = session
        self._users = users
        self._accounts = accounts
        self._windows = windows
        self._fills = fills
        self._runs = runs
        self._providers = providers
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._requested_since = requested_since_for(history_start)

    async def sync(self, trigger: SyncTrigger) -> ExchangeSyncRunSummary:
        """Sync every configured venue once, account by account, and record the run.

        1. **Orphaned `running` rows are swept, then this run's row is written and
           committed**, before any venue is called -- the reasoning of
           `BalanceSyncService.sync`, including its constraint on the future: nothing may run
           a sync outside the coordinator while the server is up.
        2. **The owner is found.** None: the run succeeds with zero accounts, because nothing
           can be attached to anyone. More than one: the run fails with zero accounts and no
           venue is called, because attaching fills to a guessed owner is worse than not
           importing them.
        3. **One account row per configured venue is ensured and committed.** A venue with a
           row and no credentials any more is not synced, and is not counted.
        4. **Each account is synced in turn**, its failure recorded as its outcome.
        5. **The run is closed out**, its status computed over the accounts it attempted.

        Raises:
            Nothing for a venue's failure, a conflicting fill or a defect in one account's
            sync -- each is an outcome. A failure to write the run row or an outcome itself
            propagates and leaves a `running` row for the sweep, as does any `BaseException`
            that is not an `Exception`.
        """
        started_at = self._clock()
        started_ms = self._monotonic()

        swept = await self._runs.sweep_interrupted()
        if swept:
            _logger.warning("exchange_sync_runs_marked_interrupted", runs=swept)
        run_id = await self._runs.open_run(
            trigger=trigger,
            started_at=started_at,
            accounts_total=len(self._providers),
        )
        await self._session.commit()

        owners = await self._users.list_all()
        if not owners:
            _logger.warning("exchange_sync_no_owner")
            return await self._finish(
                run_id, trigger, started_at, started_ms, SyncRunStatus.SUCCESS, ()
            )
        if len(owners) > 1:
            _logger.error("exchange_sync_multiple_owners", users=len(owners))
            return await self._finish(
                run_id, trigger, started_at, started_ms, SyncRunStatus.FAILED, ()
            )
        owner_id = owners[0].id

        accounts = [
            await self._accounts.ensure(
                user_id=owner_id,
                exchange_key=exchange_key,
                created_at=self._clock(),
            )
            for exchange_key in sorted(self._providers)
        ]
        await self._session.commit()

        outcomes = [await self._sync_account(run_id, account, trigger) for account in accounts]
        return await self._finish(
            run_id, trigger, started_at, started_ms, _run_status(outcomes), tuple(outcomes)
        )

    async def _finish(
        self,
        run_id: int,
        trigger: SyncTrigger,
        started_at: datetime,
        started_ms: int,
        status: SyncRunStatus,
        outcomes: tuple[AccountOutcome, ...],
    ) -> ExchangeSyncRunSummary:
        """Close the run out, commit, log it, and hand back its summary."""
        finished_at = self._clock()
        duration_ms = self._monotonic() - started_ms
        succeeded = _count(outcomes, AccountOutcomeStatus.SUCCESS)
        failed = _count(outcomes, AccountOutcomeStatus.FAILED)
        skipped = _count(outcomes, AccountOutcomeStatus.SKIPPED)
        await self._runs.finish_run(
            run_id,
            status=status,
            finished_at=finished_at,
            duration_ms=duration_ms,
            accounts_total=len(outcomes),
            accounts_succeeded=succeeded,
            accounts_failed=failed,
            accounts_skipped=skipped,
        )
        await self._session.commit()
        summary = ExchangeSyncRunSummary(
            run_id=run_id,
            trigger=trigger,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            accounts_total=len(outcomes),
            accounts_succeeded=succeeded,
            accounts_failed=failed,
            accounts_skipped=skipped,
            accounts=outcomes,
        )
        _logger.info(
            "exchange_sync_finished",
            run_id=run_id,
            trigger=trigger.value,
            status=status.value,
            duration_ms=duration_ms,
            accounts_total=len(outcomes),
            accounts_succeeded=succeeded,
            accounts_failed=failed,
            accounts_skipped=skipped,
            fills_seen=summary.fills_seen,
            fills_inserted=summary.fills_inserted,
        )
        return summary

    async def _sync_account(
        self,
        run_id: int,
        account: ExchangeAccountState,
        trigger: SyncTrigger,
    ) -> AccountOutcome:
        """Sync one account and record its outcome. **Catches `Exception` only.**

        An `auth_failed` account is skipped by a scheduled or startup run without calling the
        venue -- retrying on a timer asks a venue to refuse the same key every interval, which
        some answer with an IP ban -- and retried by a manual one, which is how the owner
        recovers after fixing the key and restarting.

        On failure the page in flight is rolled back, and the status and the outcome are
        committed together, so an account's status and the outcome `last_error` is read from
        cannot disagree.
        """
        if (
            account.sync_status is AccountSyncStatus.AUTH_FAILED
            and trigger is not SyncTrigger.MANUAL
        ):
            _logger.info(
                "exchange_sync_account_skipped",
                exchange_key=account.exchange_key.value,
                reason=AccountSyncStatus.AUTH_FAILED.value,
            )
            skipped = _Progress().outcome(account, AccountOutcomeStatus.SKIPPED)
            await self._runs.record_outcome(run_id, skipped)
            await self._session.commit()
            return skipped

        provider = self._providers[account.exchange_key]
        progress = _Progress()
        try:
            await self._read_account(account, provider, progress)
        except Exception as error:
            await self._session.rollback()
            failure = failure_of(error)
            self._report_failure(account, failure, error)
            outcome = progress.outcome(account, AccountOutcomeStatus.FAILED, failure)
            await self._accounts.set_status(account.id, failure.status)
            await self._runs.record_outcome(run_id, outcome)
            await self._session.commit()
            return outcome

        await self._accounts.mark_synced(account.id, synced_at=self._clock())
        outcome = progress.outcome(account, AccountOutcomeStatus.SUCCESS)
        await self._runs.record_outcome(run_id, outcome)
        await self._session.commit()
        _logger.info(
            "exchange_sync_account_finished",
            exchange_key=account.exchange_key.value,
            windows_completed=progress.windows_completed,
            pages=progress.pages,
            fills_seen=progress.fills_seen,
            fills_inserted=progress.fills_inserted,
        )
        return outcome

    @staticmethod
    def _report_failure(
        account: ExchangeAccountState,
        failure: AccountFailure,
        error: Exception,
    ) -> None:
        """Log a failed account: a warning for a known kind, a traceback for our own defect."""
        if failure.error_kind is ExchangeSyncErrorKind.INTERNAL:
            _logger.exception(
                "exchange_sync_account_internal_error",
                exchange_key=account.exchange_key.value,
                error_type=type(error).__name__,
            )
            return
        _logger.warning(
            "exchange_sync_account_failed",
            exchange_key=account.exchange_key.value,
            error_kind=failure.error_kind.value,
            error_type=type(error).__name__,
        )

    def _clamp_at(self, now: datetime, capabilities: ExchangeCapabilities) -> RetentionClamp:
        """The requested start moved inside the venue's retention, as of `now`.

        `min(requested, now)`: a clock stepped back behind a configured date must not make the
        clamp raise. The configured value is still what gets recorded.
        """
        return clamp_to_retention(
            min(self._requested_since, now),
            now=now,
            capabilities=capabilities,
        )

    async def _read_account(
        self,
        account: ExchangeAccountState,
        provider: ExchangeProvider,
        progress: _Progress,
    ) -> None:
        """Plan the account's history, persist the plan, then drain its queue newest first.

        **Three phases, and their order is the write-lock rule in the module docstring:**

        1. read the queue, and compute the re-clamp and the plan -- both pure;
        2. ask the venue for its candidate symbols, only when it requires them and the plan
           has new windows;
        3. make every write -- moved and replaced windows, new windows, the account's history
           -- and commit once.

        So no write is open while the venue answers or the rate limit is waited out, and a
        failure in phase 2 has written nothing. The plan is committed **before any fetch**, so
        a crash after planning resumes the plan rather than planning over it.

        A clock behind the plan -- one that once ran ahead, then was corrected -- is logged
        here, as `exchange_sync_clock_behind_plan`; `plan_account` pulls the ceiling back.
        """
        capabilities = provider.capabilities
        now = floor_to_millisecond(self._clock())
        clamp = self._clamp_at(now, capabilities)
        if account.planned_until is not None and now < account.planned_until:
            _logger.warning(
                "exchange_sync_clock_behind_plan",
                exchange_key=account.exchange_key.value,
            )

        rows = await self._windows.list_for_account(account.id)
        queue = normalise_pending(
            [_pending_of(row) for row in rows],
            floor=clamp.effective_since,
            max_window=capabilities.max_query_window,
        )
        effective_since = account.effective_since
        if queue.truncated:
            _logger.warning(
                "exchange_sync_history_truncated",
                exchange_key=account.exchange_key.value,
                windows_dropped=sum(1 for item in queue.replaced if not item.windows),
            )
            if effective_since is None or clamp.effective_since > effective_since:
                effective_since = clamp.effective_since

        # The recorded start goes through the same `min(..., now)` the clamp's did, so the two
        # are compared like with like: a clock stepped back behind a configured date would
        # otherwise look like the owner asking for earlier history.
        recorded = account.requested_since
        plan = plan_account(
            requested_since=None if recorded is None else min(recorded, now),
            effective_since=effective_since,
            planned_until=account.planned_until,
            clamp=clamp,
            now=now,
            max_window=capabilities.max_query_window,
        )
        symbols: tuple[str | None, ...] = (None,)
        if capabilities.requires_symbol:
            symbols = await self._candidate_symbols(account, provider) if plan.windows else ()

        keep_cursor = cursor_survives_a_moved_since(capabilities.cursor_kind)
        for moved in queue.moved:
            await self._windows.set_since(
                moved.original.window_id,
                moved.since,
                keep_cursor=keep_cursor,
            )
        for replacement in queue.replaced:
            await self._windows.delete(replacement.original.window_id)
            for window in replacement.windows:
                await self._windows.add(
                    account.id,
                    since=window.since,
                    until=window.until,
                    symbol=replacement.original.symbol,
                )
        for window in plan.windows:
            for symbol in symbols:
                await self._windows.add(
                    account.id,
                    since=window.since,
                    until=window.until,
                    symbol=symbol,
                )
        await self._accounts.set_history(
            account.id,
            requested_since=self._requested_since,
            effective_since=plan.effective_since,
            planned_until=plan.planned_until,
        )
        await self._session.commit()

        await self._drain_queue(account, provider, progress)

    async def _candidate_symbols(
        self,
        account: ExchangeAccountState,
        provider: ExchangeProvider,
    ) -> tuple[str, ...]:
        """The symbols to plan windows for, de-duplicated and sorted, with the page's retry."""
        found = await self._with_rate_limit_retry(account, provider.candidate_symbols)
        return tuple(sorted(set(found)))

    async def _drain_queue(
        self,
        account: ExchangeAccountState,
        provider: ExchangeProvider,
        progress: _Progress,
    ) -> None:
        """Read pending windows newest first until none is left.

        The queue is re-read after every window, because reading one can change the rest: a
        split adds two windows, and a retention step drops the ones below it. Newest is the
        greatest `(until, since, id)`, compared in Python -- a `TEXT` datetime is never
        ordered in SQL. Each pass either deletes a window, splits one into two strictly
        shorter halves, moves one's start forward, or raises, so the loop ends.
        """
        retention = _RetentionRecovery()
        while True:
            rows = await self._windows.list_for_account(account.id)
            if not rows:
                return
            newest = max(rows, key=lambda row: (row.until, row.since, row.id))
            await self._drain_window(account, provider, newest, progress, retention)

    async def _drain_window(
        self,
        account: ExchangeAccountState,
        provider: ExchangeProvider,
        row: SyncWindowRow,
        progress: _Progress,
        retention: _RetentionRecovery,
    ) -> None:
        """Page through one window from its committed cursor, one commit per page.

        Returns when the window is finished and deleted, split, or moved after a retention
        refusal.
        `sent` holds every cursor sent for this window in this run, so a `next_cursor` seen
        before -- A -> B -> A -- is refused as a schema error before anything is written;
        `require_cursor_advanced` already refuses A -> A.

        A venue with no cursor (`CursorKind.NONE`) that returns a full page has truncated the
        window: the page is not inserted, and the window is replaced by its two halves.
        """
        capabilities = provider.capabilities
        window = FillWindow(since=row.since, until=row.until)
        cursor = row.cursor
        sent: set[str] = set() if cursor is None else {cursor}
        while True:
            try:
                page = await self._fetch_page(account, provider, window, cursor, row.symbol)
            except ExchangeRetentionWindowError:
                if await self._recover_from_retention(
                    account, capabilities, row, window, retention
                ):
                    return
                raise
            progress.pages += 1
            if (
                capabilities.cursor_kind is CursorKind.NONE
                and len(page.fills) >= capabilities.page_size
            ):
                await self._split(account, row, window)
                return
            next_cursor = page.next_cursor
            if next_cursor is not None and next_cursor in sent:
                raise ExchangeSchemaError(_CURSOR_CYCLE_DETAIL)
            result = await self._fills.insert_page(
                account.id, page.fills, ingested_at=self._clock()
            )
            if next_cursor is None:
                await self._windows.delete(row.id)
            else:
                await self._windows.advance(row.id, next_cursor)
            # The one commit that makes the page durable: its fills and its checkpoint
            # together, or neither.
            await self._session.commit()
            progress.fills_seen += result.seen
            progress.fills_inserted += result.inserted
            if next_cursor is None:
                progress.windows_completed += 1
                return
            sent.add(next_cursor)
            cursor = next_cursor

    async def _fetch_page(
        self,
        account: ExchangeAccountState,
        provider: ExchangeProvider,
        window: FillWindow,
        cursor: str | None,
        symbol: str | None,
    ) -> FillPage:
        """One page, with the rate-limit retry."""

        async def fetch() -> FillPage:
            return await provider.fetch_fill_page(window, cursor=cursor, symbol=symbol)

        return await self._with_rate_limit_retry(account, fetch)

    async def _with_rate_limit_retry[T](
        self,
        account: ExchangeAccountState,
        call: Callable[[], Awaitable[T]],
    ) -> T:
        """Make one venue call, retrying a rate limit up to `RATE_LIMIT_RETRIES` times.

        The wait is the venue's `Retry-After` if it gave one, otherwise two, four, then eight
        seconds, rounded up to whole seconds. A wait over `MAX_RATE_LIMIT_WAIT_SECONDS` is not
        slept at all: the error propagates and the account fails `rate_limited` for this run.
        Only a rate limit is retried here -- the shared transport has already retried an
        outage, and every other error needs a person or a different request.
        """
        attempt = 0
        while True:
            try:
                return await call()
            except ExchangeRateLimitedError as error:
                attempt += 1
                if attempt > RATE_LIMIT_RETRIES:
                    raise
                wait = seconds_to_wait(error.retry_after_ms, attempt=attempt)
                _logger.warning(
                    "exchange_sync_rate_limited",
                    exchange_key=account.exchange_key.value,
                    attempt=attempt,
                    wait_seconds=wait,
                )
                if wait is None:
                    raise
                await self._sleep(wait)

    async def _split(
        self,
        account: ExchangeAccountState,
        row: SyncWindowRow,
        window: FillWindow,
    ) -> None:
        """Replace a truncated window by its two halves and commit; the newer is read next."""
        try:
            newer, older = split_in_half(window)
        except ValueError:
            raise ExchangeSchemaError(_UNSPLITTABLE_DETAIL) from None
        await self._windows.delete(row.id)
        for half in (newer, older):
            await self._windows.add(
                account.id,
                since=half.since,
                until=half.until,
                symbol=row.symbol,
            )
        await self._session.commit()
        _logger.info("exchange_sync_window_split", exchange_key=account.exchange_key.value)

    async def _recover_from_retention(
        self,
        account: ExchangeAccountState,
        capabilities: ExchangeCapabilities,
        row: SyncWindowRow,
        window: FillWindow,
        retention: _RetentionRecovery,
    ) -> bool:
        """Move a refused window's start forward, or report that the moves are spent.

        Two moves, in this order:

        1. **A fresh edge, once per window per run, spending no step.** Newest first means the
           oldest window is read last, and a backfill that takes longer than
           `RETENTION_MARGIN` reaches it after the edge it was planned at has aged past what
           the venue keeps. That refusal is elapsed time, not a short retention, and the right
           answer is the edge as it stands now: `clamp_to_retention` over a fresh clock read.
           If that edge is later than the window's `since`, the window moves to it -- rather
           than a whole `RETENTION_STEP`, which would discard up to a day of history the venue
           still has.
        2. **A `RETENTION_STEP`**, when the fresh edge does not move the window, or the request
           it produced is refused again: the venue keeps less than it declares. At most
           `MAX_RETENTION_STEPS` per window per run.

        Returns:
            `True` if the window moved; `False` when this window's moves are spent in this run,
            and the caller re-raises, so the account fails `retention_window`. Everything
            moved before that is committed, so the next run continues from the moved point.
        """
        if row.id not in retention.reclamped:
            retention.reclamped.add(row.id)
            now = floor_to_millisecond(self._clock())
            edge = self._clamp_at(now, capabilities).effective_since
            if edge > window.since:
                _logger.warning(
                    "exchange_sync_retention_reclamped",
                    exchange_key=account.exchange_key.value,
                )
                await self._move_since(account, capabilities, row, window, edge)
                return True
        steps = retention.steps.get(row.id, 0) + 1
        if steps > MAX_RETENTION_STEPS:
            return False
        retention.steps[row.id] = steps
        _logger.warning(
            "exchange_sync_retention_step",
            exchange_key=account.exchange_key.value,
            step=steps,
        )
        await self._move_since(account, capabilities, row, window, window.since + RETENTION_STEP)
        return True

    async def _move_since(
        self,
        account: ExchangeAccountState,
        capabilities: ExchangeCapabilities,
        row: SyncWindowRow,
        window: FillWindow,
        new_since: datetime,
    ) -> None:
        """Move a refused window's start to `new_since`, raise the account's floor, and commit.

        Every pending window lying wholly below `new_since` is dropped -- this one too, if the
        move emptied it. The window keeps its row, and keeps its cursor when
        `cursor_survives_a_moved_since` vouches for the venue's kind: a trade-id cursor is a
        bound on ids, and means the same thing over the narrower range.

        The account's `effective_since` rises to **where the held history now really begins**:
        `new_since` if the window survives the move, or the refused window's own `until` if the
        move emptied it -- the windows above it were read in this run or an earlier one, and
        claiming less than that would put the wrong date on the truncation banner. A venue
        keeping a day, read in six-hour windows, is the case: a day's step empties the oldest
        window, and `new_since` alone would lie past everything held.

        **Also capped at `planned_until`**, so the planned range can never invert. That is
        implied for a window planned under a correct clock, whose `until` is never past the
        ceiling; it is not for one planned while the clock ran ahead, after `plan_account`
        pulled the ceiling back.
        """
        for pending in await self._windows.list_for_account(account.id):
            if pending.until <= new_since:
                await self._windows.delete(pending.id)
        if window.until > new_since:
            await self._windows.set_since(
                row.id,
                new_since,
                keep_cursor=cursor_survives_a_moved_since(capabilities.cursor_kind),
            )
        current = await self._accounts.get(account.id)
        if current is not None:
            floor = min(new_since, window.until)
            if current.planned_until is not None:
                floor = min(floor, current.planned_until)
            if current.effective_since is None or floor > current.effective_since:
                await self._accounts.set_effective_since(account.id, floor)
        await self._session.commit()


def _pending_of(row: SyncWindowRow) -> PendingWindow:
    """A queued row as the planner sees it. `FillWindow` refuses an inverted or off-grid row."""
    return PendingWindow(
        window_id=row.id,
        window=FillWindow(since=row.since, until=row.until),
        symbol=row.symbol,
        cursor=row.cursor,
    )


def _count(outcomes: Sequence[AccountOutcome], status: AccountOutcomeStatus) -> int:
    """How many outcomes have this status."""
    return sum(1 for outcome in outcomes if outcome.status is status)


def _run_status(outcomes: Sequence[AccountOutcome]) -> SyncRunStatus:
    """`success`, `partial` or `failed`, over the accounts that were **attempted**.

    A skipped account was not attempted, so a run that skipped every account is a `success`
    that did nothing -- the same reading `balance_sync._run_status` gives an empty registry.
    """
    attempted = [
        outcome for outcome in outcomes if outcome.status is not AccountOutcomeStatus.SKIPPED
    ]
    if not attempted:
        return SyncRunStatus.SUCCESS
    failed = _count(attempted, AccountOutcomeStatus.FAILED)
    if failed == 0:
        return SyncRunStatus.SUCCESS
    if failed == len(attempted):
        return SyncRunStatus.FAILED
    return SyncRunStatus.PARTIAL


def build_exchange_sync_service(
    session: AsyncSession,
    *,
    providers: Mapping[ExchangeKey, ExchangeProvider],
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], int] = monotonic_ms,
    sleep: Sleep = sleep_seconds,
    history_start: date | None = None,
) -> ExchangeSyncService:
    """Assemble the sync over one session and the venues this process has credentials for.

    `providers` is required and has no default, for the reason `build_balance_sync_service`
    requires `provider_for`: a default would have to build an HTTP client and read the
    credentials in here. The lifespan builds the mapping once with `exchange_providers` and
    hands it in; a test hands in fakes.

    `history_start` is `PORTFOLIO_EXCHANGE_HISTORY_START`; `None` means `HISTORY_GENESIS`. The
    three clocks are separate arguments so a test can step each one on its own.
    """
    return ExchangeSyncService(
        session=session,
        users=UserRepository(session),
        accounts=ExchangeAccountRepository(session),
        windows=ExchangeSyncWindowRepository(session),
        fills=ExchangeFillRepository(session),
        runs=ExchangeSyncRunRepository(session),
        providers=providers,
        clock=clock,
        monotonic=monotonic,
        sleep=sleep,
        history_start=history_start,
    )
