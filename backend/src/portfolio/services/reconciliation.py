"""The holdings check: the replayed quantities beside the balances actually read (#104).

A buy older than a venue keeps, whose coins are still held, leaves no trace in the events:
every later sale fits inside the recorded pool, the engine warns about nothing, and the
average cost and the profit are computed without it. What shows the gap is a comparison, per
asset, of what the replay says is held with what is held. This module gathers the three sides
of that comparison from what is **stored**, hands them to `domain.accounting.reconcile`, and
reports the state of every source beside the answer.

**No provider here, and none reachable.** A router imports this module, so it reads the
balances the syncs stored and never asks a chain or a venue: the
`api-never-reaches-an-exchange-provider` and `prices-are-never-fetched-in-a-request` import
contracts hold for it without an edit to `.importlinter`. The venue balances are written by
`services/exchange_sync.py`, the one module that may call a venue.

## The three sides

* **History** -- the stored snapshot's `Position.quantity` per asset, through
  `AccountingService.read_snapshot`, the one implementation of the consistent snapshot read
  (spec 021, R5).
* **Wallets** -- each **active** wallet's latest snapshot, **when it is current**,
  `from_base_units(confirmed, decimals)`, summed per `ChainKey.asset_symbol`. Confirmed units
  only, as `services/balances.py` values them.
* **Exchanges** -- the stored `exchange_balances` rows of the owner's accounts **whose reading
  is current**, summed per asset.

Every sum is `domain.money.add`, over rows loaded whole: `SUM()` on a `TEXT` money column
coerces it to a float in SQLite, and `+` under a decimal context could round.

## Only a current reading is a lower bound (spec 025, R9)

The balances read are never everything the owner holds, so the held side is a lower bound, and
that is what makes "more is held than the history accounts for" a finding. **A source that
contributes nothing keeps it one**: leaving a wallet or a venue out can hide a
`history_short`, and cannot produce one.

**A reading that is out of date does not.** Coins moved after a reading was taken are counted
where they were, by the old reading, and where they are now, by a newer one. A venue whose
balance read failed last week, summed at what it held then, beside the wallet the coins were
withdrawn to since, is a `history_short` for units that do not exist. So a reading is compared
only while it is current, and one that is not contributes **nothing**:

* **A venue's reading is current** when its last balance read succeeded, its last fill sync
  succeeded, and the reading is at most `MAX_READING_AGE` old. Otherwise its rows are not
  summed and its source says why, with the first `NotComparedReason` that applies.
  The fill sync is part of the rule because balances are read only after a successful one: an
  account that has since failed, or been skipped, has stopped being read, and its reading is
  ageing with nothing saying so until the age limit is reached.
* **A wallet's reading is current** when it is at most `MAX_READING_AGE` old, and its chain
  did not fail in the latest finished balance run or a later run wrote the reading (below).
  Otherwise it adds nothing and the wallet is counted under the first
  `WalletNotComparedReason` that applies: `chain_failed`, then `unread` (no run has read
  it), then `stale` (the reading is older than the limit).

A reading dated after the clock -- the clock stepped back since -- is current: its age is not
positive, and refusing it would discard the newest reading there is.

## A wallet whose chain failed is left out at once (spec 028)

The age limit alone kept a wallet's last reading in the comparison for a day after its chain
stopped answering. Coins sent from it to a venue in that time are read at the venue by its
next sync and were still counted in the wallet: a `history_short` for units that do not
exist, with nothing naming the wallet. `sync_run_chains` already records, per balance run,
which chains failed, so the rule reads it.

**The latest finished balance run** is the newest `sync_runs` row, by `id`, whose status is
`success`, `partial` or `failed` (`SyncRunRepository.latest_finished`). A `running` or
`interrupted` run has no chain rows -- `finish_run` writes them with the final status -- so
it cannot say which chains failed, and the run before it still stands.

**A wallet is left out as `chain_failed`** when that run recorded the wallet's chain as
`failed` and the wallet's latest reading was not written by a later run: it has no reading,
or `BalanceSnapshot.sync_run_id <= run_id`. A wallet with no reading whose chain failed is
`chain_failed` and not `unread`: the failure is the fact the owner can act on.

**A reading a later run wrote is kept.** Snapshots are committed per chain before a run
closes, so a run still in flight, or one interrupted after it read the chain, leaves a
reading newer than the verdict of the latest finished run. That reading is the newest there
is. The Value section agrees with it only once that later run has been swept to
`interrupted`: while the run is in flight, or has left an orphan `running` row, the Value
section still shows the previous finished run's failure for that wallet. The comparison is
of two `INTEGER` ids, in Python.

A chain with no row in the latest finished run -- no wallet was active on it then -- and a
database with no finished run are both "did not fail", and the other rules decide.

The source reports the wallets left out this way as a count, and names each chain with how
many of the owner's wallets it left out (`WalletSources.failed_chains`). It gives no reason
for the failure. **The run log always has it**: `GET /api/balances/runs`, the failed chain's
`error_kind` and `detail` in the latest finished run. The Value section's wallet rows
usually show it too, and not always: after a later run was interrupted the row says the sync
was interrupted, and a wallet never read has no failure to show.

**What remains is the gap between two current readings.** A venue's reading and a wallet's
are taken by two different syncs, so coins moved between them are counted twice, or not at
all, until both sources have been read again. That gap is minutes while both syncs are
running. It is **up to `MAX_READING_AGE`** when a source has stopped being read without a
recorded failure, because its last reading stays current until it reaches the limit, with
nothing naming the source until then. These are the residuals:

* a wallet when the balance timer is switched off, or when no balance run finishes: the
  latest finished run is then an old one, and says nothing about what happened since;
* a venue whose credentials were removed after a read;
* a venue when the exchange timer is switched off;
* a balance read whose failure could not be recorded
  (`exchange_balances_failure_not_recorded`), whose previous reading stays compared until a
  read succeeds or it is that old.

**The chain rule leaves two windows of its own**, each bounded by one balance interval while
the balance timer runs:

* A chain that starts failing between two balance runs is not known to have failed until
  the next run finishes, so a wallet on it stays compared until then.
* A run attempts only the chains that have an active wallet, so a chain it did not attempt
  has no verdict in it. When a chain's only wallets were archived while the latest finished
  run ran and were restored afterwards, their previous readings are compared, while under
  `MAX_READING_AGE` old, even if the run before recorded the chain as failed, until the
  next run finishes.

The view carries each reading's age, and no rule here removes the gap, which is why a
`history_short` is a prompt to look and not a verdict.

## Not computed is not "everything is unaccounted for"

With no snapshot yet, `computed_at` is `None` and `assets` is **empty**: comparing the
balances with a history of nothing would report every coin the owner holds as missing from it.
The sources are still answered, because they do not depend on the snapshot.

## The history can be older than the balances, and the view cannot tell by itself

The accounting snapshot is read consistently; the wallets' and the venues' readings, and the
latest finished balance run, are read after it, in separate statements. So the sides are not
one snapshot of the database, and one window is worth naming. An exchange sync commits each
account's fills and the balances it read as it goes, and the snapshot is recomputed only after
the whole run has closed (`main.exchange_sync_runner`). **A request served inside that window
compares new balances with the old history**, and an asset bought in that sync can show as
`history_short` until the recompute commits. The accounts are synced one after the other, so
with two venues the window opened by the first spans the second venue's whole sync.

If that recompute **fails**, the window does not close: the previous snapshot stays, and every
asset bought since shows as missing from the history. Nothing in this view says so. The
endpoint therefore serves the trigger's `last_recompute` beside it, as the positions endpoint
does, and a failed one is the reader's signal that the comparison is of a stale history. It is
`None` after a restart until the startup recompute ends, and the stored snapshot is compared
meanwhile.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.accounting import RECONCILIATION_TOLERANCE_PCT, reconcile
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import AccountSyncStatus
from portfolio.domain.money import add, from_base_units
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.exchange_balances import ExchangeBalanceRepository
from portfolio.repositories.exchange_sync_runs import ExchangeSyncErrorKind
from portfolio.repositories.sync_runs import SyncRunRepository, SyncRunStatus
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.accounting import build_accounting_service

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import BalanceSnapshot, Wallet
    from portfolio.domain.accounting import AssetReconciliation
    from portfolio.domain.exchanges import ExchangeKey
    from portfolio.repositories.exchange_balances import AccountBalances
    from portfolio.repositories.sync_runs import SyncRunSummary
    from portfolio.services.accounting import AccountingService

__all__ = [
    "MAX_READING_AGE",
    "MAX_READING_AGE_HOURS",
    "ExchangeBalanceSource",
    "ExchangeSyncErrorKind",
    "FailedChain",
    "NotComparedReason",
    "ReconciliationService",
    "ReconciliationView",
    "WalletNotComparedReason",
    "WalletSources",
    "build_reconciliation_service",
    "not_compared_reason",
    "utc_now",
    "wallet_not_compared_reason",
]
"""`ExchangeSyncErrorKind` is **re-exported** from `repositories/exchange_sync_runs.py`, for the
reason `services/balances.py` re-exports its run vocabulary: `api/schemas/accounting.py`
renders `balances_error` and may not import a repository."""

MAX_READING_AGE_HOURS: Final = 24
"""`MAX_READING_AGE` as the whole number of hours the endpoint publishes."""

MAX_READING_AGE: Final = timedelta(hours=MAX_READING_AGE_HOURS)
"""How old a wallet's or a venue's reading may be and still be compared (spec 025, R9).

Both syncs run on timers of minutes, so a reading a day old means a source that has stopped
being read with nothing recorded against it -- a timer switched off, a balance run that never
finishes, a venue whose credentials were removed -- and its coins may have moved since. A day
is far above any healthy interval, so a reading is never dropped for being a few runs late,
and short enough that a source that stopped being read stops being counted the next day.

**A recorded failure does not wait for this limit.** A venue whose balance read or fill sync
failed is left out by `not_compared_reason`, and a wallet whose chain failed in the latest
finished balance run by `wallet_not_compared_reason` (spec 028), whatever the reading's age,
unless a later run wrote the reading.
"""

_NOTHING: Final = Decimal(0)
"""What a sum starts from. `add` keeps the smaller exponent, so the first quantity sets the
scale, and `reconcile` carries every figure at eighteen places whatever it is handed."""


def utc_now() -> datetime:
    """The clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


class NotComparedReason(StrEnum):
    """Why an exchange account's balances are left out of the comparison. Its wire form.

    Declared in the order they are tested, and the first that applies is the answer:

    * `READ_FAILED` -- the last balance read failed (`balances_error` is set). The rows kept
      are the reading before the failure, and nothing says the coins are still there.
    * `NEVER_READ` -- no balance read has ever succeeded, so there are no rows.
    * `SYNC_FAILED` -- the account's fill sync is not `ok`: it failed, or the key was refused
      and scheduled runs skip the account. Balances are read only after a successful fill
      sync, so the reading has stopped being refreshed.
    * `OUT_OF_DATE` -- the reading is more than `MAX_READING_AGE` old with nothing above to
      explain it: the timer is off, or the credentials were removed after the read.
    """

    READ_FAILED = "read_failed"
    NEVER_READ = "never_read"
    SYNC_FAILED = "sync_failed"
    OUT_OF_DATE = "out_of_date"


@dataclass(frozen=True, slots=True)
class ExchangeBalanceSource:
    """How one exchange account's balances stand as a source: when read, and whether compared.

    * `balances_read_at` -- when a read last succeeded, `None` when none ever has.
    * `balances_error` -- the kind the last attempt failed with, `None` when it succeeded or
      none was made.
    * `not_compared_reason` -- `None` when the account's rows are in the comparison, and
      otherwise why they are not. **A reason means the account contributes nothing**, whatever
      rows it still has.

    No asset and no amount: the comparison carries those, per asset and summed.
    """

    exchange_key: ExchangeKey
    balances_read_at: datetime | None
    balances_error: ExchangeSyncErrorKind | None
    not_compared_reason: NotComparedReason | None


class WalletNotComparedReason(StrEnum):
    """Why a wallet's reading is left out of the comparison.

    Declared in the order they are tested, and the first that applies is the answer:

    * `CHAIN_FAILED` -- the latest finished balance run could not read the wallet's chain, and
      no later run has read the wallet since (spec 028). It comes before `UNREAD`: a wallet
      with no reading whose chain failed is one the owner can act on.
    * `UNREAD` -- no run has ever read the wallet.
    * `STALE` -- the reading is more than `MAX_READING_AGE` old with nothing above to explain
      it: the balance timer is off, or no balance run finishes.

    Not a wire form, unlike `NotComparedReason`: a venue is listed with its reason, and the
    wallets are served as one count per member, in the `WalletSources` field of the same name.
    """

    CHAIN_FAILED = "chain_failed"
    UNREAD = "unread"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class FailedChain:
    """A chain the latest finished balance run could not read, and what that left out.

    `wallets` is how many of the owner's active wallets on `chain_key` are left out of the
    comparison as `chain_failed`. It is never zero: a chain that failed and left none of the
    owner's wallets out -- a later run has read them all, or the owner has none on it -- is
    not listed.

    No reason for the failure, as a venue's `sync_failed` gives none either. The run log
    always has it: `GET /api/balances/runs`, the chain's `error_kind` and `detail` in the
    latest finished run.
    """

    chain_key: str
    wallets: int


@dataclass(frozen=True, slots=True)
class WalletSources:
    """How the owner's active wallets stand as a source. The four counts add up to all of them.

    * `compared` -- wallets whose latest snapshot is current, and is in the comparison.
    * `stale` -- wallets whose latest snapshot is more than `MAX_READING_AGE` old. They add
      nothing: their coins may have moved since.
    * `unread` -- wallets no run has ever read. They add nothing either.
    * `chain_failed` -- wallets whose chain failed in the latest finished balance run, with
      no reading from a later run. They add nothing, whatever their last reading says and
      however recent it is.

    `failed_chains` names the chains behind `chain_failed`, sorted by `chain_key`, each with
    how many wallets it left out. Only a chain with at least one wallet left out is listed,
    so the entries' `wallets` add up to `chain_failed`.

    `oldest_observed_at` is the oldest `observed_at` among the **compared** wallets, `None`
    when none is compared -- the oldest rather than the newest, unlike `CurrentBalances.as_of`,
    because here it bounds how old the comparison's wallet side can be.
    """

    compared: int
    stale: int
    unread: int
    chain_failed: int
    failed_chains: tuple[FailedChain, ...]
    oldest_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReconciliationView:
    """Everything the service contributes to `GET /api/accounting/reconciliation`.

    `computed_at` is the accounting snapshot's, and `None` when none has been written yet --
    and then `assets` is empty: "not computed", never "every balance is unaccounted for".
    `exchanges` and `wallets` are answered either way. `assets` is sorted by asset and leaves
    the cash assets out; `exchanges` lists every account row, by `exchange_key`, compared or
    not. `max_reading_age_hours` is the age limit the sources were held to.
    """

    computed_at: datetime | None
    tolerance_pct: Decimal
    max_reading_age_hours: int
    assets: tuple[AssetReconciliation, ...]
    exchanges: tuple[ExchangeBalanceSource, ...]
    wallets: WalletSources


def not_compared_reason(account: AccountBalances, now: datetime) -> NotComparedReason | None:
    """Why `account`'s reading is not current as of `now`, or `None` when it is. Pure.

    The one statement of the venue rule: the last balance read succeeded, a reading exists,
    the fill sync is `ok`, and the reading is at most `MAX_READING_AGE` old. The checks run in
    `NotComparedReason`'s order and the first that fails is the reason, so an account whose
    read failed is `read_failed` even when it has never been read at all.

    The age is `now - balances_read_at`, two aware datetimes compared in Python. A reading
    dated after `now` has a negative age and is current.
    """
    if account.balances_error is not None:
        return NotComparedReason.READ_FAILED
    if account.balances_read_at is None:
        return NotComparedReason.NEVER_READ
    if account.sync_status is not AccountSyncStatus.OK:
        return NotComparedReason.SYNC_FAILED
    if now - account.balances_read_at > MAX_READING_AGE:
        return NotComparedReason.OUT_OF_DATE
    return None


def wallet_not_compared_reason(
    chain_key: str,
    reading: BalanceSnapshot | None,
    latest_finished_run: SyncRunSummary | None,
    now: datetime,
) -> WalletNotComparedReason | None:
    """Why a wallet's reading is not current as of `now`, or `None` when it is. Pure.

    The one statement of the wallet rule (spec 028). `chain_key` is the wallet's, `reading`
    its latest balance snapshot, `None` when no run has read it, and `latest_finished_run`
    what `SyncRunRepository.latest_finished` answered, `None` when no run has finished. The
    checks run in `WalletNotComparedReason`'s order and the first that applies is the reason:

    1. `chain_failed` -- that run has a `failed` outcome for `chain_key`, and the wallet has
       no reading or its reading's `sync_run_id` is not above that run's id. A reading a
       **later** run wrote is never `chain_failed`: a run still in flight, or one interrupted
       after it read the chain, has committed a reading newer than that verdict.
    2. `unread` -- no reading.
    3. `stale` -- the reading is more than `MAX_READING_AGE` old.

    A chain with no outcome in the run, and no finished run at all, are both "did not fail".

    The ids are two integers and the age is `now - observed_at`, two aware datetimes, all
    compared in Python. A reading dated after `now` has a negative age and is not stale.
    """
    if (
        latest_finished_run is not None
        and _failed_in(latest_finished_run, chain_key)
        and (reading is None or reading.sync_run_id <= latest_finished_run.run_id)
    ):
        return WalletNotComparedReason.CHAIN_FAILED
    if reading is None:
        return WalletNotComparedReason.UNREAD
    if now - reading.observed_at > MAX_READING_AGE:
        return WalletNotComparedReason.STALE
    return None


def _failed_in(run: SyncRunSummary, chain_key: str) -> bool:
    """Whether `run` recorded `chain_key` as failed. A chain it has no outcome for did not."""
    return any(
        outcome.chain_key == chain_key and outcome.status is SyncRunStatus.FAILED
        for outcome in run.chains
    )


class ReconciliationService:
    """Reads the three stored sides of the holdings check and compares them. Writes nothing.

    It takes no session, for the reason `BalanceService` does not: every method here reads.
    `build_reconciliation_service` takes one, because the repositories need it. The clock is
    what a reading's age is measured against, and is injected so a test can name the instant.
    """

    def __init__(
        self,
        *,
        accounting: AccountingService,
        wallets: WalletRepository,
        balances: BalanceRepository,
        sync_runs: SyncRunRepository,
        exchange_balances: ExchangeBalanceRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._accounting = accounting
        self._wallets = wallets
        self._balances = balances
        self._sync_runs = sync_runs
        self._exchange_balances = exchange_balances
        self._clock = clock

    async def reconciliation(self, user_id: int) -> ReconciliationView:
        """The owner's replayed quantities beside the current balances, with every source's state.

        Reads only what is stored: no recompute, no chain and no venue is asked. Five reads,
        and the number of statements does not grow with the wallets or the accounts: the
        snapshot, through `read_snapshot`, which repeats its statements while the snapshot
        changes under it; the active wallets, in one statement; the latest finished balance
        run and then its chains, in two; the wallets' latest balance snapshots, in one; the
        exchange accounts and then their rows, in two. The clock is read once, so every
        reading is held to the same instant, and the run is read once, so every wallet is
        held to the same run.

        **The run is read before the snapshots, and the order is the rule's** (spec 028, R3):
        see the comment at the two reads.

        Raises:
            SnapshotReadError: the accounting snapshot changed during every read attempt.
            ValueError: a stored quantity is negative, which no writer here produces: a
                position never goes below zero, `ck_balance_snapshots_confirmed` refuses a
                negative confirmed balance, and `AssetBalance` refuses a negative total.
        """
        now = self._clock()
        snapshot = await self._accounting.read_snapshot(user_id)
        wallets = await self._wallets.list_for_user(user_id)
        # The run first, then the snapshots, in that order (spec 028, R3). The two are separate
        # statements, and a balance run can write a chain's snapshots and finish between them.
        # Read the other way round, that pairs the reading from before that run with the
        # verdict of that run: a chain that had failed, and that the run in between read,
        # leaves the wallet's old reading in the comparison under a `success` verdict, which
        # is the false `history_short` this rule exists to remove, for one response.
        #
        # In this order every interleaving equals a state that held at some instant. A reading
        # written after the run was read carries a higher `sync_run_id` than that run, so it
        # is kept as the reading of a later run. A run that fails the chain after the run was
        # read writes no reading, and the wallet is judged as it stood when the run was read.
        latest_finished_run = await self._sync_runs.latest_finished()
        latest = await self._balances.latest_for_wallets([wallet.id for wallet in wallets])
        accounts = await self._exchange_balances.list_for_user(user_id)

        reasons = [not_compared_reason(account, now) for account in accounts]
        exchanges = tuple(
            ExchangeBalanceSource(
                exchange_key=account.exchange_key,
                balances_read_at=account.balances_read_at,
                balances_error=account.balances_error,
                not_compared_reason=reason,
            )
            for account, reason in zip(accounts, reasons, strict=True)
        )
        wallet_reasons = {
            wallet.id: wallet_not_compared_reason(
                wallet.chain_key, latest.get(wallet.id), latest_finished_run, now
            )
            for wallet in wallets
        }
        current = {
            wallet_id: reading
            for wallet_id, reading in latest.items()
            if wallet_reasons[wallet_id] is None
        }
        left_out = Counter(wallet_reasons.values())
        failed_chains = Counter(
            wallet.chain_key
            for wallet in wallets
            if wallet_reasons[wallet.id] is WalletNotComparedReason.CHAIN_FAILED
        )
        observed = [reading.observed_at for reading in current.values()]
        wallet_sources = WalletSources(
            compared=len(current),
            stale=left_out[WalletNotComparedReason.STALE],
            unread=left_out[WalletNotComparedReason.UNREAD],
            chain_failed=left_out[WalletNotComparedReason.CHAIN_FAILED],
            failed_chains=tuple(
                FailedChain(chain_key=chain_key, wallets=count)
                for chain_key, count in sorted(failed_chains.items())
            ),
            oldest_observed_at=min(observed) if observed else None,
        )
        if snapshot is None:
            return ReconciliationView(
                computed_at=None,
                tolerance_pct=RECONCILIATION_TOLERANCE_PCT,
                max_reading_age_hours=MAX_READING_AGE_HOURS,
                assets=(),
                exchanges=exchanges,
                wallets=wallet_sources,
            )
        compared_accounts = [
            account for account, reason in zip(accounts, reasons, strict=True) if reason is None
        ]
        return ReconciliationView(
            computed_at=snapshot.header.computed_at,
            tolerance_pct=RECONCILIATION_TOLERANCE_PCT,
            max_reading_age_hours=MAX_READING_AGE_HOURS,
            assets=reconcile(
                {position.asset: position.quantity for position in snapshot.positions},
                _wallet_quantities(wallets, current),
                _exchange_quantities(compared_accounts),
            ),
            exchanges=exchanges,
            wallets=wallet_sources,
        )


def _wallet_quantities(
    wallets: Sequence[Wallet],
    current: Mapping[int, BalanceSnapshot],
) -> dict[str, Decimal]:
    """What the wallets with a current reading hold, per asset symbol, summed exactly.

    The quantity is `from_base_units(confirmed, decimals)` with the exponent the reading
    itself recorded, the conversion `services/balances.py` makes. **A wallet absent from
    `current` -- unread, read too long ago, or on a chain that failed -- contributes nothing
    at all** rather than a zero or its old balance: it is counted under its reason instead.
    """
    totals: dict[str, Decimal] = {}
    for wallet in wallets:
        reading = current.get(wallet.id)
        if reading is None:
            continue
        symbol = ChainKey(wallet.chain_key).asset_symbol
        quantity = from_base_units(reading.confirmed, reading.decimals)
        totals[symbol] = add(totals.get(symbol, _NOTHING), quantity)
    return totals


def _exchange_quantities(accounts: Iterable[AccountBalances]) -> dict[str, Decimal]:
    """What the given accounts' stored balances hold, per asset, summed exactly.

    The caller passes only the accounts whose reading is current. Keyed by the asset as each
    venue names it, which is how that venue's fills name it too: the name a position carries.
    """
    totals: dict[str, Decimal] = {}
    for account in accounts:
        for balance in account.balances:
            totals[balance.asset] = add(totals.get(balance.asset, _NOTHING), balance.quantity)
    return totals


def build_reconciliation_service(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> ReconciliationService:
    """Assemble the read-side service over one database session.

    The accounting service is built over the same session and used for `read_snapshot` alone;
    the repositories are built here rather than injected because there is exactly one
    implementation of each. The clock is injectable, and shared with the accounting service,
    so that a test can name the instant a reading turns out of date.
    """
    return ReconciliationService(
        accounting=build_accounting_service(session, clock=clock),
        wallets=WalletRepository(session),
        balances=BalanceRepository(session),
        sync_runs=SyncRunRepository(session),
        exchange_balances=ExchangeBalanceRepository(session),
        clock=clock,
    )
