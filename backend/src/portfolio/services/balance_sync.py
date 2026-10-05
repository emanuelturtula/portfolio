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

Three catch clauses, deliberately not one, because **three different parties can be at
fault** and collapsing them is how one of them gets blamed for another's mistake:

* a `ProviderError` is recorded with **the vendor's own kind** -- `unavailable`,
  `rate_limited`, `response`, `unknown_chain` -- because that is what #6's vocabulary is
  for, and a caller has to be able to tell a broken vendor from a bad answer;
* an `AddressInvalidError` is **`address_rejected`**, and it is the *owner's* configuration
  rather than anyone's failure. A provider validates every address before it builds a URL,
  and registration does not check the network, so a mainnet address configured against a
  testnet index is refused here on every tick forever. No traceback: there is nothing to
  debug;
* **anything else is `internal`**, with the traceback logged. Our bug, and recording it as
  "Kaspa is unavailable" is how a code defect gets read as a vendor outage for months.

Catching only `ProviderError` and letting a `TypeError` fail the run was the alternative,
and it loses good Bitcoin data to a Kaspa parser bug -- the exact outcome the issue refuses.
The third clause is a correction: it used to fall into `internal`, which reported a user's
configuration as a defect in this application and wrote a traceback for it four times an
hour.

**`detail` is written differently in each clause, and the asymmetry is a disclosure
control.** A `ProviderError`'s message is rendered in full, because every provider in this
application is written never to quote a body, a URL or an address into one. An
`AddressInvalidError` carries an `AddressRejection` member and a fixed sentence per member,
not one of which interpolates anything, so the reason and a count of the wallets that lost
their read are recorded and the address is not. An arbitrary exception has made no such
promise: `KeyError` renders its key, and a key here would be an address -- so the internal
clause records the exception's **type name and nothing else**, and the message reaches the
log rather than the database and the response body.

## The database is touched before the gather and after it, never inside it

An `AsyncSession` is not safe for concurrent use, so a coroutine that wrote its own
snapshots would be one `InvalidRequestError` away from taking down the chain that worked.
Each coroutine therefore does provider I/O only and hands back what it read; the writes
happen afterwards, one chain at a time, **with a commit per chain**. That granularity is
what keeps criterion 3 true all the way to disk: a snapshot write that fails needs a
rollback, and a rollback that spanned two chains would discard the balances of the chain
that had already succeeded.

## An extended-key wallet is one wallet, however many addresses it reads (spec 031)

A wallet whose `kind` is `extended_key` is scanned by the chain's provider, which derives its
addresses and reads every one of them (R5, R10). Its snapshot is the **sum** of what the scan
read (R7): `confirmed` summed, `pending` summed only when every address reported one. It
still counts as one wallet in every count of the run.

Its derived addresses are loaded **before** the gather, with the wallets, and handed to the
scan as plain records; the new ones and the newly used ones come back in the reading and are
written in **the chain's own commit**, with its snapshot (R6). So a scan that fails, or a
chain whose write fails, persists none of them, and the next sync starts again from what was
last committed. That costs time and never a wrong number.

A chain with an extended-key wallet whose provider is not an `ExtendedKeyScanner` fails as
`internal`. The domain refuses an extended key on any chain but Bitcoin, so this is
unreachable; it is checked anyway because the alternative to failing is skipping the wallet
silently, and a skipped wallet reads as a zero.

The per-wallet scan log line carries `wallet_id` and counts, never a key, an address or an
index. It is INFO when the scan persisted new addresses and DEBUG otherwise, so an unchanged
wallet does not add a line every fifteen minutes.

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

from portfolio.domain.addresses import AddressInvalidError
from portfolio.domain.chains import WalletKind
from portfolio.providers.base import ExtendedKeyScanner, KnownDerivedAddress
from portfolio.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
    UnknownChainError,
)
from portfolio.providers.http import monotonic_ms
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.derived_addresses import (
    DerivedAddressRecord,
    DerivedAddressRepository,
)
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
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import Wallet
    from portfolio.providers.base import AddressBalance, ChainProvider, ExtendedKeyScan

__all__ = [
    "BalanceSyncService",
    "ChainProviderFor",
    "ExtendedKeysUnsupportedError",
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


class ExtendedKeysUnsupportedError(TypeError):
    """A chain holds an extended-key wallet and its provider cannot scan one.

    Unreachable while the domain refuses an extended key on every chain but Bitcoin, and
    raised anyway: the alternative is to skip the wallet, and a skipped wallet is a balance
    that silently reads as nothing. It lands in `_read_chain`'s last clause, as `internal`,
    which is what it is -- a gap in this application, not a vendor's failure.
    """

    def __init__(self) -> None:
        super().__init__("This chain's provider cannot scan an extended public key.")


def error_kind_of(error: ProviderError) -> SyncErrorKind:
    """Which recorded kind a provider failure is, by its type rather than by its message.

    Branching on the type rather than on the wording is the point `ProviderError.status`
    already makes: a message is prose written for an operator, and coupling behaviour to it
    breaks the day somebody improves a sentence.

    **A `ProviderError` that is none of the four falls to `INTERNAL`, which reads wrong and
    is right.** The four subclasses are the vendor verdicts this application knows how to
    report; a fifth added without a line here is our omission, not a vendor's outage, and
    filing it under a vendor's name is exactly the confusion `INTERNAL` exists to prevent.
    The `CHECK` on `sync_run_chains.error_kind` admits a closed set, so an unmapped error has
    to become one of them regardless -- this chooses the one that points at us.

    `ADDRESS_REJECTED` is deliberately **not** reachable from here. It is not a provider
    failure at all, and `_read_chain` catches `AddressInvalidError` in its own clause.
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
    derived: _DerivedChanges | None = None


@dataclass(frozen=True, slots=True)
class _DerivedChanges:
    """What an extended-key wallet's scan changed, to be written with its snapshot (R6).

    `scanned` is a count for the log line, and the only thing about the scan that is logged.
    """

    new: tuple[DerivedAddressRecord, ...]
    newly_used: tuple[tuple[int, int], ...]
    scanned: int


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
        derived: DerivedAddressRepository | None = None,
    ) -> None:
        self._session = session
        self._wallets = wallets
        self._runs = runs
        self._balances = balances
        # Optional, and built over the same session when omitted, because the repository
        # has one implementation and every caller that predates spec 031 omits it.
        self._derived = derived if derived is not None else DerivedAddressRepository(session)
        self._provider_for = provider_for
        self._clock = clock
        self._monotonic = monotonic

    async def sync(self, trigger: SyncTrigger) -> SyncRunSummary:
        """Read every active wallet once, chain by chain, and record the run.

        The order of the work, and each step is a decision:

        1. **Orphans are swept, then the wallets are listed and the run row is written and
           committed**, before any provider is built. A run the process dies in the middle
           of leaves a `running` row, which the next run's sweep -- or the lifespan's, at
           startup and shutdown -- turns into `interrupted`.
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

        # **Any `running` row that exists now is not live**, so it is swept before this
        # run's own row is opened. Two guarantees make that true rather than hopeful: the
        # coordinator allows one run at a time in this process, and there is one process.
        # So a `running` row at this point belongs to a run whose close-out failed -- the
        # database was locked, the disk was full -- and without this it would stay
        # `running` until the next process start, which on the Pi can be weeks.
        #
        # **That second guarantee is load-bearing, and it is a constraint on the future.**
        # An entry point that ran a sync outside the coordinator while the server was up --
        # a `sync-balances` CLI command in another container, say -- would sweep the
        # server's live run. Such a command has to go through the server's endpoint or run
        # with the server stopped. The startup and shutdown sweeps in `main.py` rest on the
        # same guarantee.
        #
        # Swept before the insert, so this run's own row cannot be caught by it, and
        # committed in the same transaction as the insert.
        swept = await self._runs.sweep_interrupted()
        if swept:
            _logger.warning("balance_sync_runs_marked_interrupted", runs=swept)

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

        # Before the gather, for the reason the module docstring gives: nothing inside it
        # may touch the session. Frozen records, so nothing inside it can lazy-load either.
        known = await self._derived.list_for_wallets(
            [wallet.id for wallet in wallets if wallet.kind == WalletKind.EXTENDED_KEY.value]
        )

        groups = _group_by_chain(wallets)
        reads = await asyncio.gather(
            *(self._read_chain(chain_key, group, known) for chain_key, group in groups)
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

    async def _read_chain(
        self,
        chain_key: str,
        wallets: Sequence[Wallet],
        known: Mapping[int, Sequence[DerivedAddressRecord]],
    ) -> _ChainRead:
        """Read one chain's wallets. **Never raises**, which is what isolates the others.

        The clock is read *before* the request rather than after it, so a snapshot is dated
        no later than the moment it was true. That errs early by however long the chain took
        to answer, which is the only direction that cannot make a stale reading look fresh --
        the same argument `PriceRefreshService` makes about `as_of`.

        Addresses are de-duplicated before the provider is asked. Two accounts watching one
        address is the case, and both `align_balances` and each provider's own guard refuse a
        repeated address -- correctly, since a batch answering about the same thing twice is
        a correlation bug. De-duplicating here means the one read is fanned back out to both
        wallets instead of costing a chain its whole run.

        **Address wallets first, as one batch, then each extended-key wallet in turn**
        (spec 031). The scans are sequential for the reason the providers' reads are: they
        share one host's limiter, and a `gather` would turn its floor into a queue. A failure
        in any of them fails the chain, as a failed batch always has.
        """
        observed_at = self._clock()
        address_wallets = [w for w in wallets if w.kind != WalletKind.EXTENDED_KEY.value]
        key_wallets = [w for w in wallets if w.kind == WalletKind.EXTENDED_KEY.value]
        try:
            provider = self._provider_for(chain_key)
            readings: tuple[_WalletReading, ...] = ()
            if address_wallets:
                # `dict.fromkeys` rather than a `set`: it de-duplicates *and* keeps the
                # order the wallets were registered in, so the request a vendor receives is
                # stable between runs and a log of two syncs is comparable.
                addresses = tuple(
                    dict.fromkeys(wallet.address_canonical for wallet in address_wallets)
                )
                balances = await provider.fetch_balances(addresses)
                readings = _fan_out(balances, address_wallets, observed_at)
            if key_wallets:
                if not isinstance(provider, ExtendedKeyScanner):
                    raise ExtendedKeysUnsupportedError
                for wallet in key_wallets:
                    persisted = known.get(wallet.id, ())
                    scan = await provider.scan_extended_key(
                        wallet.address_canonical,
                        [
                            KnownDerivedAddress(
                                branch=record.branch,
                                index=record.child_index,
                                address=record.address,
                                used=record.used,
                            )
                            for record in persisted
                        ],
                    )
                    readings += (_reading_of_scan(wallet.id, persisted, scan, observed_at),)
        except AddressInvalidError as error:
            # **A third clause, and neither of the other two would have been right.** A
            # provider validates every address before it builds a URL, so a row the wallet
            # registry accepted can still be refused here -- most plausibly by the network
            # check, which registration does not perform: a mainnet address under
            # `PORTFOLIO_BITCOIN_NETWORK=testnet` is refused on every tick, forever.
            #
            # That is the owner's configuration, not a vendor outage and not our bug.
            # Recording it as `internal` filed a user's mistake under "a defect in this
            # application" and wrote a traceback for it every fifteen minutes, which is the
            # failure the `internal` kind exists to prevent, pointed the other way.
            #
            # **No traceback**, deliberately: there is nothing to debug, and the frame that
            # raised has the address in scope. `AddressRejection` is a closed set of fixed
            # words and `REJECTION_MESSAGES` is a fixed sentence per member -- not one of
            # them interpolates anything -- so the reason is safe to record and to serve
            # where the address is not. The count says how much of the chain was lost; which
            # wallet is a question the owner answers from the registry, where the addresses
            # already are.
            _logger.warning(
                "balance_sync_chain_address_rejected",
                chain_key=chain_key,
                reason=error.reason.value,
                wallets=len(wallets),
            )
            return _ChainRead(
                outcome=ChainOutcome(
                    chain_key=chain_key,
                    status=SyncRunStatus.FAILED,
                    wallets_read=0,
                    error_kind=SyncErrorKind.ADDRESS_REJECTED,
                    # "Before it was read", not "before any request": since spec 031 a chain's
                    # address wallets may have been read before one of its extended keys was
                    # refused. Nothing of the chain is kept either way.
                    detail=(
                        f"{len(wallets)} wallet(s) on this chain were not read: an address or "
                        f"an extended key was refused before it was read "
                        f"({error.reason.value}). {error.message}"
                    ),
                ),
                readings=(),
            )
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
                if reading.derived is not None:
                    # In this chain's commit, with the snapshot it produced (R6): both are
                    # written, or neither is.
                    await self._derived.apply(
                        reading.wallet_id,
                        new=reading.derived.new,
                        newly_used=reading.derived.newly_used,
                        created_at=reading.observed_at,
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
        for reading in read.readings:
            if reading.derived is not None:
                _log_scan(reading.wallet_id, reading.derived)
        return read.outcome


def _log_scan(wallet_id: int, changes: _DerivedChanges) -> None:
    """One line per committed extended-key wallet: its id and three counts, nothing else.

    INFO when the scan persisted a new address, DEBUG otherwise: a wallet that did not
    change would otherwise add an INFO line every fifteen minutes, forever. Written after
    the commit, so an INFO line is a statement about what is on disk.

    **No field name contains `address`**: `redact_sensitive` matches that fragment as a
    substring of the key and would print every count as `[REDACTED]`.
    """
    log = _logger.info if changes.new else _logger.debug
    log(
        "balance_sync_extended_key_scanned",
        wallet_id=wallet_id,
        derived_scanned=changes.scanned,
        derived_new=len(changes.new),
        derived_newly_used=len(changes.newly_used),
    )


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


def _reading_of_scan(
    wallet_id: int,
    persisted: Sequence[DerivedAddressRecord],
    scan: ExtendedKeyScan,
    observed_at: datetime,
) -> _WalletReading:
    """One extended-key wallet's snapshot, and what its scan changed (R6, R7).

    `confirmed` is the sum over every scanned address. `pending` is the sum when every one
    of them reported a figure and `None` when any did not -- the meaning a missing
    `mempool_stats` already has, carried through rather than read as zero. A scan with no
    used address is a real zero, not an unread wallet. Integers of base units throughout:
    nothing here is a float or a `Decimal` until `BalanceRepository` scales it.

    New is a position, `(branch, index)`, that was not persisted; newly used is one that
    was persisted unused and now reads used. Positions rather than address strings, because
    a position is what the table's unique constraint is on.

    Raises:
        ProviderResponseError: the scan did not read every persisted address, or read one
            position twice. Either would make the sum quietly wrong. The message counts and
            never names.
    """
    persisted_used = {(record.branch, record.child_index): record.used for record in persisted}
    positions = [(address.branch, address.index) for address in scan.addresses]
    distinct = set(positions)
    if len(distinct) != len(positions) or not distinct.issuperset(persisted_used):
        message = (
            f"The extended-key scan left {len(set(persisted_used) - distinct)} persisted "
            "address(es) unread, or read one position twice, so its sum cannot be trusted."
        )
        raise ProviderResponseError(message)

    new: list[DerivedAddressRecord] = []
    newly_used: list[tuple[int, int]] = []
    for address in scan.addresses:
        position = (address.branch, address.index)
        was_used = persisted_used.get(position)
        if was_used is None:
            new.append(
                DerivedAddressRecord(
                    branch=address.branch,
                    child_index=address.index,
                    address=address.address,
                    used=address.used,
                )
            )
        elif address.used and not was_used:
            newly_used.append(position)

    pendings = [address.pending for address in scan.addresses]
    reported = [figure for figure in pendings if figure is not None]
    return _WalletReading(
        wallet_id=wallet_id,
        confirmed=sum(address.confirmed for address in scan.addresses),
        pending=sum(reported) if len(reported) == len(pendings) else None,
        decimals=scan.decimals,
        observed_at=observed_at,
        derived=_DerivedChanges(
            new=tuple(new), newly_used=tuple(newly_used), scanned=len(scan.addresses)
        ),
    )


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
        derived=DerivedAddressRepository(session),
    )
