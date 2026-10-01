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
* **A wallet's reading is current** when it is at most `MAX_READING_AGE` old. One that is
  older adds nothing and is counted as `stale`; a wallet no run has read adds nothing and is
  counted as `unread`.

A reading dated after the clock -- the clock stepped back since -- is current: its age is not
positive, and refusing it would discard the newest reading there is.

**What remains is the gap between two current readings.** A venue's reading and a wallet's
are taken by two different syncs, so coins moved between them are counted twice, or not at
all, until both sources have been read again. That gap is minutes while both syncs are
running. It is **up to `MAX_READING_AGE`** when a source has stopped being read without a
recorded failure, because its last reading stays current until it reaches the limit: a wallet
whose chain is failing (#116 will leave it out), a venue whose credentials were removed or
whose timer is off, and a balance read whose failure could not be recorded
(`exchange_balances_failure_not_recorded`), whose previous reading stays compared until a read
succeeds or it is that old. The view carries each reading's age, and no rule here removes the
gap, which is why a `history_short` is a prompt to look and not a verdict.

## Not computed is not "everything is unaccounted for"

With no snapshot yet, `computed_at` is `None` and `assets` is **empty**: comparing the
balances with a history of nothing would report every coin the owner holds as missing from it.
The sources are still answered, because they do not depend on the snapshot.

## The history can be older than the balances, and the view cannot tell by itself

The accounting snapshot is read consistently; the wallets' and the venues' readings are read
after it, in separate statements. So the sides are not one snapshot of the database, and one
window is worth naming. An exchange sync commits each account's fills and the balances it
read as it goes, and the snapshot is recomputed only after the whole run has closed
(`main.exchange_sync_runner`). **A request served inside that window compares new balances
with the old history**, and an asset bought in that sync can show as `history_short` until
the recompute commits. The accounts are synced one after the other, so with two venues the
window opened by the first spans the second venue's whole sync.

If that recompute **fails**, the window does not close: the previous snapshot stays, and every
asset bought since shows as missing from the history. Nothing in this view says so. The
endpoint therefore serves the trigger's `last_recompute` beside it, as the positions endpoint
does, and a failed one is the reader's signal that the comparison is of a stale history. It is
`None` after a restart until the startup recompute ends, and the stored snapshot is compared
meanwhile.
"""

from __future__ import annotations

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
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.accounting import build_accounting_service

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import BalanceSnapshot, Wallet
    from portfolio.domain.accounting import AssetReconciliation
    from portfolio.domain.exchanges import ExchangeKey
    from portfolio.repositories.exchange_balances import AccountBalances
    from portfolio.services.accounting import AccountingService

__all__ = [
    "MAX_READING_AGE",
    "MAX_READING_AGE_HOURS",
    "ExchangeBalanceSource",
    "ExchangeSyncErrorKind",
    "NotComparedReason",
    "ReconciliationService",
    "ReconciliationView",
    "WalletSources",
    "build_reconciliation_service",
    "not_compared_reason",
    "utc_now",
]
"""`ExchangeSyncErrorKind` is **re-exported** from `repositories/exchange_sync_runs.py`, for the
reason `services/balances.py` re-exports its run vocabulary: `api/schemas/accounting.py`
renders `balances_error` and may not import a repository."""

MAX_READING_AGE_HOURS: Final = 24
"""`MAX_READING_AGE` as the whole number of hours the endpoint publishes."""

MAX_READING_AGE: Final = timedelta(hours=MAX_READING_AGE_HOURS)
"""How old a wallet's or a venue's reading may be and still be compared (spec 025, R9).

Both syncs run on timers of minutes, so a reading a day old means a source that has stopped
being read -- a chain failing on every run, a venue whose account is skipped, a timer switched
off -- and its coins may have moved since. A day is far above any healthy interval, so a
reading is never dropped for being a few runs late, and short enough that a source that
stopped being read stops being counted the next day.
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


@dataclass(frozen=True, slots=True)
class WalletSources:
    """How the owner's active wallets stand as a source. The three counts add up to all of them.

    * `compared` -- wallets whose latest snapshot is current, and is in the comparison.
    * `stale` -- wallets whose latest snapshot is more than `MAX_READING_AGE` old. They add
      nothing: their coins may have moved since.
    * `unread` -- wallets no run has ever read. They add nothing either.

    `oldest_observed_at` is the oldest `observed_at` among the **compared** wallets, `None`
    when none is compared -- the oldest rather than the newest, unlike `CurrentBalances.as_of`,
    because here it bounds how old the comparison's wallet side can be.
    """

    compared: int
    stale: int
    unread: int
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
        exchange_balances: ExchangeBalanceRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._accounting = accounting
        self._wallets = wallets
        self._balances = balances
        self._exchange_balances = exchange_balances
        self._clock = clock

    async def reconciliation(self, user_id: int) -> ReconciliationView:
        """The owner's replayed quantities beside the current balances, with every source's state.

        Reads only what is stored: no recompute, no chain and no venue is asked. Four reads
        whatever the number of wallets and accounts: the snapshot, the active wallets, their
        latest balance snapshots, and the exchange accounts with their rows. The clock is read
        once, so every reading is held to the same instant.

        Raises:
            SnapshotReadError: the accounting snapshot changed during every read attempt.
            ValueError: a stored quantity is negative, which no writer here produces: a
                position never goes below zero, `ck_balance_snapshots_confirmed` refuses a
                negative confirmed balance, and `AssetBalance` refuses a negative total.
        """
        now = self._clock()
        snapshot = await self._accounting.read_snapshot(user_id)
        wallets = await self._wallets.list_for_user(user_id)
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
        current = {
            wallet_id: reading
            for wallet_id, reading in latest.items()
            if now - reading.observed_at <= MAX_READING_AGE
        }
        observed = [reading.observed_at for reading in current.values()]
        wallet_sources = WalletSources(
            compared=len(current),
            stale=len(latest) - len(current),
            unread=len(wallets) - len(latest),
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
    `current` -- unread, or read too long ago -- contributes nothing at all** rather than a
    zero or its old balance: it is counted as unread or stale instead.
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
        exchange_balances=ExchangeBalanceRepository(session),
        clock=clock,
    )
