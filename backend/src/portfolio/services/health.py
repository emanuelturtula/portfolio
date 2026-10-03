"""How the application's own sources stand, for `GET /api/health/detail` (#23, spec 030).

`HealthService.detail` composes six sections: the backups (#22), the four timers, the balance
sync per chain, the exchange accounts, the prices, and the holdings check. Each reports what
its **last recorded attempt** says.

## No vendor is called, and nothing is configured into the answer

The Health page refetches every minute, and the dashboard shares the query, so a check that
called a chain index or a venue would multiply the calls their rate limits are budgeted for.
Every section here reads a table, an in-memory timer or the backup directory, and nothing
else; `ChainProvider.health()` stays unused by production code. No interval, path, URL, key,
tolerance or age limit is in the result.

## A section that fails does not fail the others

`chains`, `exchanges`, `prices` and `reconciliation` are each read inside their own `try`. One
that raises is logged as `health_section_failed`, with `section` and `error_type` -- the class
name, never the message, which is free text that may quote a row -- and is served as
`unavailable`, with every other field null or empty. After a failure the session is rolled
back, when the caller handed in a way to do it, so that a statement error does not leave the
next section reading through a transaction in an unknown state. `backup` and the timers never
raise: the first by `BackupService.status`'s own contract, the second because a timer's status
is held in memory.

## The sections, one by one

* **`schedulers`** -- the four timers, by `SchedulerName`, in a fixed order. A timer that was
  never built is `disabled`; one that was is what `IntervalScheduler.status` says.
* **`chains`** -- one entry per chain key that has an outcome in the latest finished balance
  run or is used by an active wallet, sorted by key. `state` is the newest finished run's
  outcome for the chain (`ok`, or `failing` when it failed or was partial), and `never` when no
  finished run has one. `last_success_at` is the newest successful outcome's run's
  `finished_at`; `last_error_kind` the newest outcome's kind while failing. **Never `detail`**:
  it is the provider's text, and the run log already serves it.
* **`exchanges`** -- one entry per exchange account, by `exchange_key`: where its fill sync
  stands, when it last synced, and its balance reading as `ok`, `failing` or `never`.
* **`prices`** -- `fresh`, `stale` past `services.prices.STALE_AFTER`, or `never`.
* **`reconciliation`** -- `domain.health.summarize_reconciliation` over
  `ReconciliationService.reconciliation`: a state and three counts, never a quantity.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey
from portfolio.domain.health import (
    PriceHealthState,
    ReconciliationHealthState,
    SchedulerName,
    SchedulerState,
    SectionState,
    SourceState,
    balances_state,
    price_state,
    source_state,
    summarize_reconciliation,
)
from portfolio.repositories.exchanges import ExchangeAccountRepository
from portfolio.repositories.prices import PriceRepository
from portfolio.repositories.sync_runs import SyncErrorKind, SyncRunRepository, SyncRunStatus
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.prices import STALE_AFTER
from portfolio.services.reconciliation import build_reconciliation_service

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.services.backup import BackupService, BackupStatus
    from portfolio.services.reconciliation import ReconciliationService
    from portfolio.services.scheduler import SchedulerStatus

__all__ = [
    "SCHEDULER_ORDER",
    "AccountSyncStatus",
    "ChainHealth",
    "ChainsHealth",
    "ExchangeHealth",
    "ExchangeKey",
    "ExchangesHealth",
    "HealthDetail",
    "HealthSection",
    "HealthService",
    "PriceHealthState",
    "PricesHealth",
    "ReconciliationHealth",
    "ReconciliationHealthState",
    "SchedulerHealth",
    "SchedulerName",
    "SchedulerState",
    "SectionState",
    "SourceState",
    "SyncErrorKind",
    "TimerLike",
    "build_health_service",
    "utc_now",
]
"""`AccountSyncStatus`, `ExchangeKey`, `SyncErrorKind` and the `domain.health` enums are
**re-exported**, for the reason `services/balances.py` re-exports its run vocabulary:
`api/schemas/health.py` renders them, and the service that produces a value is where the API
layer gets its type from."""

SCHEDULER_ORDER: Final[tuple[SchedulerName, ...]] = (
    SchedulerName.BALANCE_SYNC,
    SchedulerName.PRICE_REFRESH,
    SchedulerName.EXCHANGE_SYNC,
    SchedulerName.BACKUP,
)
"""The order the timers are served in: the order `main.lifespan` builds them."""

_logger = structlog.get_logger(__name__)


def utc_now() -> datetime:
    """The clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


class HealthSection(StrEnum):
    """The sections that can fail on their own: the `section` of `health_section_failed`."""

    CHAINS = "chains"
    EXCHANGES = "exchanges"
    PRICES = "prices"
    RECONCILIATION = "reconciliation"


class TimerLike(Protocol):
    """What the service needs of a timer. `IntervalScheduler` is one."""

    def status(self, now: datetime) -> SchedulerStatus:
        """How the timer stands at `now`. Never raises."""
        ...


@dataclass(frozen=True, slots=True)
class SchedulerHealth:
    """One timer, by name. `last_tick_at` and `last_tick_succeeded` are `None` before a tick
    has finished, and always for a `disabled` timer."""

    name: SchedulerName
    state: SchedulerState
    last_tick_at: datetime | None
    last_tick_succeeded: bool | None


@dataclass(frozen=True, slots=True)
class ChainHealth:
    """One chain: its newest outcome's state, its newest success, and the kind while failing."""

    chain_key: str
    state: SourceState
    last_success_at: datetime | None
    last_error_kind: SyncErrorKind | None


@dataclass(frozen=True, slots=True)
class ChainsHealth:
    """The balance sync per chain. `items` is empty when `state` is `unavailable`."""

    state: SectionState
    items: tuple[ChainHealth, ...]


@dataclass(frozen=True, slots=True)
class ExchangeHealth:
    """One exchange account: its fill sync and its balance reading."""

    exchange_key: ExchangeKey
    sync_state: AccountSyncStatus
    last_synced_at: datetime | None
    balances_state: SourceState
    balances_read_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExchangesHealth:
    """Every exchange account. `items` is empty when `state` is `unavailable`."""

    state: SectionState
    items: tuple[ExchangeHealth, ...]


@dataclass(frozen=True, slots=True)
class PricesHealth:
    """The newest price row's age. `latest_fetched_at` is `None` when `never` or `unavailable`."""

    state: PriceHealthState
    latest_fetched_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReconciliationHealth:
    """The holdings check, reduced. Every field but `state` is `None` when `unavailable`."""

    state: ReconciliationHealthState
    computed_at: datetime | None
    assets_compared: int | None
    assets_mismatched: int | None
    sources_not_compared: int | None


@dataclass(frozen=True, slots=True)
class HealthDetail:
    """Everything `GET /api/health/detail` serves."""

    backup: BackupStatus
    schedulers: tuple[SchedulerHealth, ...]
    chains: ChainsHealth
    exchanges: ExchangesHealth
    prices: PricesHealth
    reconciliation: ReconciliationHealth


_CHAINS_UNAVAILABLE: Final = ChainsHealth(state=SectionState.UNAVAILABLE, items=())
_EXCHANGES_UNAVAILABLE: Final = ExchangesHealth(state=SectionState.UNAVAILABLE, items=())
_PRICES_UNAVAILABLE: Final = PricesHealth(
    state=PriceHealthState.UNAVAILABLE, latest_fetched_at=None
)
_RECONCILIATION_UNAVAILABLE: Final = ReconciliationHealth(
    state=ReconciliationHealthState.UNAVAILABLE,
    computed_at=None,
    assets_compared=None,
    assets_mismatched=None,
    sources_not_compared=None,
)


class HealthService:
    """Reads how every source stands. Writes nothing and calls no vendor.

    `timers` maps each `SchedulerName` to its timer, or to `None` when the settings never
    built it; a name missing from the mapping is `None` too. `after_failure` is awaited after a
    section raised -- `build_health_service` passes the session's `rollback` -- and anything it
    raises is suppressed: the next section's own failure, if any, is logged on its own.
    """

    def __init__(
        self,
        *,
        backup: BackupService,
        timers: dict[SchedulerName, TimerLike | None],
        sync_runs: SyncRunRepository,
        wallets: WalletRepository,
        exchanges: ExchangeAccountRepository,
        prices: PriceRepository,
        reconciliation: ReconciliationService,
        after_failure: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._backup = backup
        self._timers = timers
        self._sync_runs = sync_runs
        self._wallets = wallets
        self._exchanges = exchanges
        self._prices = prices
        self._reconciliation = reconciliation
        self._after_failure = after_failure
        self._clock = clock

    async def detail(self, user_id: int) -> HealthDetail:
        """Every section, as of one instant. **Never raises** for a section's failure.

        The clock is read once, so the timers and the prices are judged against the same
        instant. The backup keeps its own clock, as it does on its own.
        """
        now = self._clock()
        backup = await self._backup.status()
        schedulers = self._schedulers(now)
        chains = await self._section(HealthSection.CHAINS, self._chains, user_id, now)
        exchanges = await self._section(HealthSection.EXCHANGES, self._exchanges_of, user_id, now)
        prices = await self._section(HealthSection.PRICES, self._prices_of, user_id, now)
        reconciliation = await self._section(
            HealthSection.RECONCILIATION, self._reconciliation_of, user_id, now
        )
        return HealthDetail(
            backup=backup,
            schedulers=schedulers,
            chains=chains if chains is not None else _CHAINS_UNAVAILABLE,
            exchanges=exchanges if exchanges is not None else _EXCHANGES_UNAVAILABLE,
            prices=prices if prices is not None else _PRICES_UNAVAILABLE,
            reconciliation=(
                reconciliation if reconciliation is not None else _RECONCILIATION_UNAVAILABLE
            ),
        )

    async def _section[T](
        self,
        name: HealthSection,
        read: Callable[[int, datetime], Awaitable[T]],
        user_id: int,
        now: datetime,
    ) -> T | None:
        """Run one section's read, or log its failure and answer `None`.

        `Exception` only: a cancellation is not a section's outcome, and propagates.
        """
        try:
            return await read(user_id, now)
        except Exception as exc:  # served as `unavailable`; the log says why
            _logger.error(
                "health_section_failed", section=name.value, error_type=type(exc).__name__
            )
        if self._after_failure is not None:
            with suppress(Exception):
                await self._after_failure()
        return None

    def _schedulers(self, now: datetime) -> tuple[SchedulerHealth, ...]:
        """The four timers, in `SCHEDULER_ORDER`; one never built is `disabled`."""
        healths: list[SchedulerHealth] = []
        for name in SCHEDULER_ORDER:
            timer = self._timers.get(name)
            if timer is None:
                healths.append(
                    SchedulerHealth(
                        name=name,
                        state=SchedulerState.DISABLED,
                        last_tick_at=None,
                        last_tick_succeeded=None,
                    )
                )
                continue
            status = timer.status(now)
            healths.append(
                SchedulerHealth(
                    name=name,
                    state=status.state,
                    last_tick_at=status.last_tick_at,
                    last_tick_succeeded=status.last_tick_succeeded,
                )
            )
        return tuple(healths)

    async def _chains(self, user_id: int, now: datetime) -> ChainsHealth:
        """One entry per chain in the latest finished run or among the active wallets.

        A chain with an outcome in some older finished run but in neither of those is not
        listed: no wallet uses it any more, and nothing will read it again.
        """
        del now  # A chain's state is its last outcome, whenever that was.
        latest_run = await self._sync_runs.latest_finished()
        wallets = await self._wallets.list_for_user(user_id)
        histories = await self._sync_runs.chain_histories()
        keys = {wallet.chain_key for wallet in wallets}
        if latest_run is not None:
            keys |= {outcome.chain_key for outcome in latest_run.chains}
        items: list[ChainHealth] = []
        for chain_key in sorted(keys):
            history = histories.get(chain_key)
            if history is None:
                items.append(
                    ChainHealth(
                        chain_key=chain_key,
                        state=SourceState.NEVER,
                        last_success_at=None,
                        last_error_kind=None,
                    )
                )
                continue
            state = source_state(history.latest.status is SyncRunStatus.SUCCESS)
            items.append(
                ChainHealth(
                    chain_key=chain_key,
                    state=state,
                    last_success_at=history.last_success_at,
                    last_error_kind=(
                        history.latest.error_kind if state is SourceState.FAILING else None
                    ),
                )
            )
        return ChainsHealth(state=SectionState.OK, items=tuple(items))

    async def _exchanges_of(self, user_id: int, now: datetime) -> ExchangesHealth:
        """Every exchange account of the owner, by `exchange_key`."""
        del now  # An account's state is its last attempt, whenever that was.
        accounts = await self._exchanges.list_for_user(user_id)
        return ExchangesHealth(
            state=SectionState.OK,
            items=tuple(
                ExchangeHealth(
                    exchange_key=account.exchange_key,
                    sync_state=account.sync_status,
                    last_synced_at=account.last_synced_at,
                    balances_state=balances_state(
                        read_at=account.balances_read_at,
                        failed=account.balances_error is not None,
                    ),
                    balances_read_at=account.balances_read_at,
                )
                for account in accounts
            ),
        )

    async def _prices_of(self, user_id: int, now: datetime) -> PricesHealth:
        """The newest price row's instant, judged against `STALE_AFTER`. Prices are global."""
        del user_id  # The price cache is not per owner.
        latest = await self._prices.latest_fetched_at()
        return PricesHealth(
            state=price_state(latest, now=now, stale_after=STALE_AFTER),
            latest_fetched_at=latest,
        )

    async def _reconciliation_of(self, user_id: int, now: datetime) -> ReconciliationHealth:
        """The holdings check, summarized. Its reading ages are its service's clock's."""
        del now  # `ReconciliationService` reads its own clock once, for every reading.
        summary = summarize_reconciliation(await self._reconciliation.reconciliation(user_id))
        return ReconciliationHealth(
            state=summary.state,
            computed_at=summary.computed_at,
            assets_compared=summary.assets_compared,
            assets_mismatched=summary.assets_mismatched,
            sources_not_compared=summary.sources_not_compared,
        )


def build_health_service(
    session: AsyncSession,
    *,
    backup: BackupService,
    timers: dict[SchedulerName, TimerLike | None],
    clock: Callable[[], datetime] = utc_now,
) -> HealthService:
    """Assemble the service over one read-only session.

    The repositories and the reconciliation service are built over `session`, and a failed
    section rolls it back before the next one reads. The clock is shared with the
    reconciliation service, so a test names one instant for both.
    """

    async def rollback() -> None:
        await session.rollback()

    return HealthService(
        backup=backup,
        timers=timers,
        sync_runs=SyncRunRepository(session),
        wallets=WalletRepository(session),
        exchanges=ExchangeAccountRepository(session),
        prices=PriceRepository(session),
        reconciliation=build_reconciliation_service(session, clock=clock),
        after_failure=rollback,
        clock=clock,
    )
