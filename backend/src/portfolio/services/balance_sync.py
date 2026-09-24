"""Reading every active wallet's balance. **The only module in `services/` that may.**

That isolation is the same guarantee `services/price_refresh.py` states for prices, drawn
one seam over: this module imports a chain provider and `services/balances.py` -- the read
side, which a router imports -- does not. The difference between the two halves is that a
request *may* reach a chain provider here, deliberately, and may never reach a price
vendor. `POST /api/balances/sync` exists precisely so the owner can ask for a read and wait
for it; nobody asks for a price refresh, and every dashboard render would trigger one.

## Failure isolation is per chain, and the error kind says whose fault it was

Wallets are grouped by `chain_key` and each group is one coroutine under `asyncio.gather`.
**No coroutine raises** -- each returns a `ChainOutcome` -- so `return_exceptions=True` is
not used, and that is a decision rather than an omission: it would also absorb
`CancelledError` and turn a shutdown into a chain recorded as having failed.

Two catch clauses, deliberately not one:

* a `ProviderError` is recorded with **the vendor's own kind** -- `unavailable`,
  `rate_limited`, `response`, `unknown_chain` -- because that is what #6's vocabulary is
  for, and a caller has to be able to tell a broken vendor from a bad answer;
* **anything else is `internal`**, with the traceback logged. Our bug, and recording it as
  "Kaspa is unavailable" is how a code defect gets read as a vendor outage for months.

Catching only `ProviderError` and letting a `TypeError` fail the run was the alternative,
and it loses good Bitcoin data to a Kaspa parser bug -- the exact outcome the issue refuses.

**`detail` is written differently for the two clauses, and the asymmetry is a disclosure
control.** A `ProviderError`'s message is rendered in full, because every provider in this
application is written never to quote a body, a URL or an address into one. An arbitrary
exception has made no such promise: `KeyError` renders its key, and a key here would be an
address. So the internal clause records the exception's **type name and nothing else**, and
the message reaches the log rather than the database and the response body.

## The database is touched before the gather and after it, never inside it

An `AsyncSession` is not safe for concurrent use, so a coroutine that wrote its own
snapshots would be one `InvalidRequestError` away from taking down the chain that worked.
Each coroutine therefore does provider I/O only and hands back what it read; the writes
happen afterwards, one chain at a time, **with a commit per chain**. That granularity is
what keeps criterion 3 true all the way to disk: a snapshot write that fails needs a
rollback, and a rollback that spanned two chains would discard the balances of the chain
that had already succeeded.

## Every run writes its row before it does any work

The `sync_runs` row is inserted at `status='running'` and **committed** before the first
provider call. A row visible only inside an uncommitted transaction is not evidence that a
run started, and criterion 4 says *every* run writes one -- which a row written at the end
does not, for a run the process died in the middle of.

## `float` is banned here, so the two clocks are separate on purpose

`started_at` and `finished_at` are wall clock and answer *when*. `duration_ms` is an integer
of milliseconds from `providers.http.monotonic_ms` and answers *how long*. They are not
redundant: subtracting two wall-clock reads is wrong by however much the clock was stepped
between them, and a Raspberry Pi that syncs its clock mid-run would otherwise record a
negative duration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog

from portfolio.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
    UnknownChainError,
)
from portfolio.providers.http import monotonic_ms
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.sync_runs import (
    ChainOutcome,
    SyncErrorKind,
    SyncRunRepository,
    SyncRunStatus,
    SyncRunSummary,
    SyncTrigger,
)
from portfolio.repositories.wallets import WalletRepository

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import Wallet
    from portfolio.providers.base import AddressBalance, ChainProvider

__all__ = [
    "BalanceSyncService",
    "ChainProviderFor",
    "build_balance_sync_service",
    "error_kind_of",
    "utc_now",
]

_logger = structlog.get_logger(__name__)


def utc_now() -> datetime:
    """The wall clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


type ChainProviderFor = Callable[[str], ChainProvider]
"""How this service gets from a chain key to something that can read that chain.

A callable rather than the registry plus an `httpx.AsyncClient`, for the reason
`build_price_refresh_service` requires its sources to be handed in: the client is a
process-wide object whose lifetime belongs to whoever opened it, and a service that built
one would be deciding something it has no business deciding. The lifespan passes
`lambda key: get_chain_provider(key, client)`; a test passes a dict lookup.

It is also what keeps `httpx` out of `services/` entirely while this module is the one that
actually causes network traffic.

**It may raise `UnknownChainError`**, and that is the intended path for a wallet whose chain
has no registered provider: it is a `ProviderError`, so it is caught by the first clause and
recorded as `unknown_chain` against that chain alone.
"""

# Ordered, and the order is the whole content: `ProviderRateLimitedError` is a subclass of
# `ProviderUnavailableError`, so the general arm would swallow the specific one if it came
# first -- and being throttled is the one failure whose remedy is a configuration change
# rather than patience.
_PROVIDER_ERROR_KINDS: Final[tuple[tuple[type[ProviderError], SyncErrorKind], ...]] = (
    (ProviderRateLimitedError, SyncErrorKind.RATE_LIMITED),
    (ProviderUnavailableError, SyncErrorKind.UNAVAILABLE),
    (ProviderResponseError, SyncErrorKind.RESPONSE),
    (UnknownChainError, SyncErrorKind.UNKNOWN_CHAIN),
)


def error_kind_of(error: ProviderError) -> SyncErrorKind:
    """Which recorded kind a provider failure is, by its type rather than by its message.

    Branching on the type rather than on the wording is the point `ProviderError.status`
    already makes: a message is prose written for an operator, and coupling behaviour to it
    breaks the day somebody improves a sentence.

    **A `ProviderError` that is none of the four falls to `INTERNAL`, which reads wrong and
    is right.** The four subclasses are the vendor verdicts this application knows how to
    report; a fifth added without a line here is our omission, not a vendor's outage, and
    filing it under a vendor's name is exactly the confusion `INTERNAL` exists to prevent.
    The `CHECK` on `sync_run_chains.error_kind` admits five values, so an unmapped error has
    to become one of them regardless -- this chooses the one that points at us.
    """
    for error_type, kind in _PROVIDER_ERROR_KINDS:
        if isinstance(error, error_type):
            return kind
    return SyncErrorKind.INTERNAL


@dataclass(frozen=True, slots=True)
class _WalletReading:
    """One wallet's balance as it came back, before anything has been written.

    An intermediate rather than a row, because the coroutine that produced it must not touch
    the session -- see the module docstring. `wallet_id` rather than the `Wallet` for the
    same reason: an ORM object read in one place and used in another after a rollback is an
    implicit lazy load, which under an async session raises rather than querying.
    """

    wallet_id: int
    confirmed: int
    pending: int | None
    decimals: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _ChainRead:
    """What one chain's coroutine came back with: a verdict, and whatever it read.

    A failed chain carries an empty `readings`, which is not the same as a chain that read
    nothing: the verdict says which. Nothing infers one from the other.
    """

    outcome: ChainOutcome
    readings: tuple[_WalletReading, ...]


class BalanceSyncService:
    """Reads every active wallet's balance and records what happened. Owns the transaction.

    The caller -- the coordinator, through the scheduler or a request -- owns the session and
    closes it. **The session must not be a request's**: a manual sync is joined rather than
    refused, so a run outlives the request that started it and a session closed by a
    dependency would be pulled out from under it. `api/dependencies.py` hands the router a
    coordinator, never this service.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        wallets: WalletRepository,
        runs: SyncRunRepository,
        balances: BalanceRepository,
        provider_for: ChainProviderFor,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], int] = monotonic_ms,
    ) -> None:
        self._session = session
        self._wallets = wallets
        self._runs = runs
        self._balances = balances
        self._provider_for = provider_for
        self._clock = clock
        self._monotonic = monotonic

    async def sync(self, trigger: SyncTrigger) -> SyncRunSummary:
        """Read every active wallet once, chain by chain, and record the run.

        The order of the work, and each step is a decision:

        1. **The wallets are listed and the run row is written and committed**, before any
           provider is built. A run the process dies in the middle of leaves a `running`
           row, which the lifespan's sweep turns into `interrupted`.
        2. **Wallets are grouped by chain and the groups run concurrently.** One chain's
           failure is recorded and does not reach the others -- criterion 3.
        3. **The readings are written one chain at a time, committing per chain**, so that
           a write failure rolls back only the chain that failed.
        4. **The run is closed out and the transaction commits once more.** Counts come from
           the outcomes, the duration from the monotonic clock.

        A database with no active wallets is a `success` run with zero counts and no chain
        rows, not a crash and not a failure: nothing was asked because nothing was
        registered.

        Raises:
            Nothing for a vendor failure, a wrong-network address or a snapshot that could
            not be written -- all three are lines in the summary. A failure to write or
            close the run row itself does propagate, and leaves a `running` row for the
            sweep, because at that point there is nowhere left to record anything.

        Returns:
            The run, its counts, its timing and one entry per chain that was attempted.
        """
        started_at = self._clock()
        started_ms = self._monotonic()

        wallets = await self._wallets.list_all_active()
        run = await self._runs.open_run(
            trigger=trigger,
            started_at=started_at,
            wallets_total=len(wallets),
        )
        # Committed here, not at the end. The row's whole purpose is to be visible to
        # somebody who is not inside this transaction: a second connection asking what the
        # sync is doing, and the next process after this one is killed.
        await self._session.commit()
        run_id = run.id

        groups = _group_by_chain(wallets)
        reads = await asyncio.gather(
            *(self._read_chain(chain_key, group) for chain_key, group in groups)
        )

        outcomes = [await self._write_chain(run_id, read) for read in reads]
        succeeded = sum(
            outcome.wallets_read for outcome in outcomes if outcome.status is SyncRunStatus.SUCCESS
        )
        status = _run_status(outcomes)
        finished_at = self._clock()
        duration_ms = self._monotonic() - started_ms

        await self._runs.finish_run(
            run_id,
            status=status,
            finished_at=finished_at,
            duration_ms=duration_ms,
            wallets_succeeded=succeeded,
            wallets_failed=len(wallets) - succeeded,
            chains=outcomes,
        )
        await self._session.commit()

        _logger.info(
            "balance_sync_finished",
            run_id=run_id,
            trigger=trigger.value,
            status=status.value,
            duration_ms=duration_ms,
            wallets_total=len(wallets),
            wallets_succeeded=succeeded,
        )
        return SyncRunSummary(
            run_id=run_id,
            trigger=trigger,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            wallets_total=len(wallets),
            wallets_succeeded=succeeded,
            wallets_failed=len(wallets) - succeeded,
            chains=tuple(outcomes),
        )

    async def _read_chain(self, chain_key: str, wallets: Sequence[Wallet]) -> _ChainRead:
        """Read one chain's addresses. **Never raises**, which is what isolates the others.

        The clock is read *before* the request rather than after it, so a snapshot is dated
        no later than the moment it was true. That errs early by however long the chain took
        to answer, which is the only direction that cannot make a stale reading look fresh --
        the same argument `PriceRefreshService` makes about `as_of`.

        Addresses are de-duplicated before the provider is asked. Two accounts watching one
        address is the case, and both `align_balances` and each provider's own guard refuse a
        repeated address -- correctly, since a batch answering about the same thing twice is
        a correlation bug. De-duplicating here means the one read is fanned back out to both
        wallets instead of costing a chain its whole run.
        """
        observed_at = self._clock()
        try:
            provider = self._provider_for(chain_key)
            # `dict.fromkeys` rather than a `set`: it de-duplicates *and* keeps the order
            # the wallets were registered in, so the request a vendor receives is stable
            # between runs and a log of two syncs is comparable.
            addresses = tuple(dict.fromkeys(wallet.address_canonical for wallet in wallets))
            balances = await provider.fetch_balances(addresses)
            readings = _fan_out(balances, wallets, observed_at)
        except ProviderError as error:
            kind = error_kind_of(error)
            _logger.warning(
                "balance_sync_chain_failed",
                chain_key=chain_key,
                error_kind=kind.value,
                error_type=type(error).__name__,
            )
            return _ChainRead(
                outcome=ChainOutcome(
                    chain_key=chain_key,
                    status=SyncRunStatus.FAILED,
                    wallets_read=0,
                    error_kind=kind,
                    # Rendered in full: every provider in this application is written never
                    # to put a body, a URL or an address in one of these.
                    detail=str(error),
                ),
                readings=(),
            )
        except Exception as error:
            # **Our bug, not the vendor's**, and recorded as such so that a parser defect is
            # never read as an outage at the chain. The traceback goes to the log, the way
            # `api.errors.handle_unexpected_error` already handles an unexpected exception;
            # only the type name is recorded and served, because an arbitrary exception has
            # made no promise about what its message contains and a `KeyError` here would
            # render an address.
            _logger.exception(
                "balance_sync_chain_internal_error",
                chain_key=chain_key,
                error_type=type(error).__name__,
            )
            return _ChainRead(
                outcome=ChainOutcome(
                    chain_key=chain_key,
                    status=SyncRunStatus.FAILED,
                    wallets_read=0,
                    error_kind=SyncErrorKind.INTERNAL,
                    detail=type(error).__name__,
                ),
                readings=(),
            )
        return _ChainRead(
            outcome=ChainOutcome(
                chain_key=chain_key,
                status=SyncRunStatus.SUCCESS,
                wallets_read=len(readings),
            ),
            readings=readings,
        )

    async def _write_chain(self, run_id: int, read: _ChainRead) -> ChainOutcome:
        """Append one chain's snapshots and commit them, or downgrade the chain's outcome.

        **The commit is per chain, and that is what makes the isolation survive to disk.** A
        refused insert leaves the session needing a rollback, and a rollback spanning two
        chains would discard the balances of the one that had already succeeded -- turning
        "the other chain still worked" back into the failure criterion 3 forbids.

        A chain that read nothing -- because it failed, or because it has no wallets -- skips
        the write and keeps the verdict it arrived with.
        """
        if not read.readings:
            return read.outcome
        try:
            for reading in read.readings:
                await self._balances.record(
                    wallet_id=reading.wallet_id,
                    sync_run_id=run_id,
                    confirmed=reading.confirmed,
                    pending=reading.pending,
                    decimals=reading.decimals,
                    observed_at=reading.observed_at,
                )
            await self._session.commit()
        except Exception as error:
            await self._session.rollback()
            _logger.exception(
                "balance_sync_snapshot_write_failed",
                chain_key=read.outcome.chain_key,
                error_type=type(error).__name__,
            )
            return ChainOutcome(
                chain_key=read.outcome.chain_key,
                status=SyncRunStatus.FAILED,
                wallets_read=0,
                error_kind=SyncErrorKind.INTERNAL,
                detail=type(error).__name__,
            )
        return read.outcome


def _group_by_chain(wallets: Sequence[Wallet]) -> list[tuple[str, list[Wallet]]]:
    """Wallets by chain key, sorted by key.

    Sorted so that two runs over the same registry produce their chain rows in the same
    order and a transcript is diffable -- the insertion order would otherwise be whichever
    coroutine happened to finish first, which is not a fact worth recording.
    """
    groups: dict[str, list[Wallet]] = {}
    for wallet in wallets:
        groups.setdefault(wallet.chain_key, []).append(wallet)
    return sorted(groups.items())


def _fan_out(
    balances: Sequence[AddressBalance],
    wallets: Sequence[Wallet],
    observed_at: datetime,
) -> tuple[_WalletReading, ...]:
    """Turn one balance per distinct address into one reading per wallet.

    The provider contract is one result per requested address, in order, each carrying the
    address it is about. This correlates on the carried address rather than on position, for
    the reason `AddressBalance` carries it at all: a guarantee that is also checkable is
    worth more than one that is only promised.

    Raises:
        ProviderResponseError: a requested address is missing from the answer, which is the
            provider contract broken. The message says how many are missing and **never
            which** -- the addresses are the owner's holdings, and this message is destined
            for a column an endpoint renders.
    """
    by_address = {balance.address: balance for balance in balances}
    readings: list[_WalletReading] = []
    missing = 0
    for wallet in wallets:
        balance = by_address.get(wallet.address_canonical)
        if balance is None:
            missing += 1
            continue
        readings.append(
            _WalletReading(
                wallet_id=wallet.id,
                confirmed=balance.confirmed,
                pending=balance.pending,
                decimals=balance.decimals,
                observed_at=observed_at,
            )
        )
    if missing:
        message = (
            f"The provider answered about {missing} fewer address(es) than it was asked "
            "about, so the result cannot be matched to the request."
        )
        raise ProviderResponseError(message)
    return tuple(readings)


def _run_status(chains: Sequence[ChainOutcome]) -> SyncRunStatus:
    """`success`, `partial` or `failed`, from the chains that were actually attempted.

    **No chains at all is `success`, not `failed`.** A database with nothing registered has
    nothing to read, and reporting that as a failed sync would send an operator looking at
    vendors that were never called -- the same distinction `PriceUnavailable` draws between
    "nobody answered" and "nothing was asked".
    """
    if not chains:
        return SyncRunStatus.SUCCESS
    failed = sum(1 for chain in chains if chain.status is SyncRunStatus.FAILED)
    if failed == 0:
        return SyncRunStatus.SUCCESS
    if failed == len(chains):
        return SyncRunStatus.FAILED
    return SyncRunStatus.PARTIAL


def build_balance_sync_service(
    session: AsyncSession,
    *,
    provider_for: ChainProviderFor,
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], int] = monotonic_ms,
) -> BalanceSyncService:
    """Assemble the sync over one database session and a way to reach a chain.

    `provider_for` is required and has no default, for the reason `build_price_refresh_service`
    requires its sources: a default would have to build an `httpx.AsyncClient` in here, which
    is a process-wide object whose lifetime belongs to whoever opened it, and it would make
    the one thing this service must not decide -- which chains exist and over what connection
    pool -- a decision it makes silently when a caller forgets.

    Both clocks are injectable and they are separate arguments on purpose: a test that steps
    the wall clock backwards mid-run must still see a positive `duration_ms`.
    """
    return BalanceSyncService(
        session=session,
        wallets=WalletRepository(session),
        runs=SyncRunRepository(session),
        balances=BalanceRepository(session),
        provider_for=provider_for,
        clock=clock,
        monotonic=monotonic,
    )
