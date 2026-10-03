"""Response models for `GET /api/health/detail`: how the application's own sources stand.

`backup` came first (#22, spec 029). #23 (spec 030) adds the others beside it: the four timers,
the balance sync per chain, the exchange accounts, the prices and the holdings check. That is
why the payload is an object keyed by source rather than the backup's fields at the top.

## No configuration value is served

Not the backup directory, not an interval, a retention, a URL, a key, a tolerance or an age
limit. They are the operator's, they are in the operator's environment file, and an endpoint is
not where anybody needs to read them back. What is served is what the owner can act on: what
each source's last recorded attempt says, and when.

## Every state is an enum whose members are the wire form

So the generated TypeScript types are unions of exactly these strings, and a page's wording
table can be total. The enums are the service's; this module only renders them.

## A section that could not be read

`chains`, `exchanges`, `prices` and `reconciliation` each have an `unavailable` state, served
when its read raised: `items` is then empty and every other field null. The log says why, as
`health_section_failed`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

# Runtime imports, not `TYPE_CHECKING` ones: Pydantic resolves a field's type when the model
# class is created.
from portfolio.services.backup import BackupErrorKind, BackupState, BackupStatus
from portfolio.services.health import (
    AccountSyncStatus,
    ChainHealth,
    ChainsHealth,
    ExchangeHealth,
    ExchangeKey,
    ExchangesHealth,
    HealthDetail,
    PriceHealthState,
    PricesHealth,
    ReconciliationHealth,
    ReconciliationHealthState,
    SchedulerHealth,
    SchedulerName,
    SchedulerState,
    SectionState,
    SourceState,
    SyncErrorKind,
)


class BackupStatusResponse(BaseModel):
    """How the scheduled copies of the database stand.

    `latest_at` is the newest copy's instant, `null` with none; `count` is how many copies
    there are. Both are `null` when `state` is `unreadable`: the backup directory cannot be
    listed, so they are unknown, which is not the same as none. `last_attempt_at` and
    `last_error_kind` describe the timer's most recent attempt in this process -- both `null`
    before one, and `last_error_kind` `null` after a success. They are held in memory, so a
    restart clears them.
    """

    state: BackupState
    latest_at: datetime | None
    count: int | None
    last_attempt_at: datetime | None
    last_error_kind: BackupErrorKind | None

    @classmethod
    def of(cls, status: BackupStatus) -> BackupStatusResponse:
        """Render the service's status."""
        return cls(
            state=status.state,
            latest_at=status.latest_at,
            count=status.count,
            last_attempt_at=status.last_attempt_at,
            last_error_kind=status.last_error_kind,
        )


class SchedulerStatusResponse(BaseModel):
    """One of the four timers.

    `last_tick_at` is when its last tick finished and `last_tick_succeeded` whether that tick
    returned without raising; both `null` before a tick has finished, and always for a
    `disabled` timer. Held in memory, so a restart clears them.
    """

    name: SchedulerName
    state: SchedulerState
    last_tick_at: datetime | None
    last_tick_succeeded: bool | None

    @classmethod
    def of(cls, health: SchedulerHealth) -> SchedulerStatusResponse:
        """Render one timer."""
        return cls(
            name=health.name,
            state=health.state,
            last_tick_at=health.last_tick_at,
            last_tick_succeeded=health.last_tick_succeeded,
        )


class ChainHealthResponse(BaseModel):
    """One chain the balance sync reads.

    `state` is its outcome in the newest finished run that has one: `ok`, `failing` when it
    failed, or `never`. `last_success_at` is when the newest run that read it successfully
    finished. `last_error_kind` is the newest outcome's kind while `failing`, else `null`. The
    provider's message is not served; the balance run log has it.
    """

    chain_key: str
    state: SourceState
    last_success_at: datetime | None
    last_error_kind: SyncErrorKind | None

    @classmethod
    def of(cls, health: ChainHealth) -> ChainHealthResponse:
        """Render one chain."""
        return cls(
            chain_key=health.chain_key,
            state=health.state,
            last_success_at=health.last_success_at,
            last_error_kind=health.last_error_kind,
        )


class ChainsHealthResponse(BaseModel):
    """The balance sync per chain, sorted by `chain_key`. Empty `items` when `unavailable`."""

    state: SectionState
    items: list[ChainHealthResponse]

    @classmethod
    def of(cls, health: ChainsHealth) -> ChainsHealthResponse:
        """Render the section."""
        return cls(
            state=health.state, items=[ChainHealthResponse.of(item) for item in health.items]
        )


class ExchangeHealthResponse(BaseModel):
    """One exchange account: where its fill sync stands, and its balance reading.

    `sync_state` is the account's sync status and `last_synced_at` when a run last left it
    with nothing pending. `balances_state` is `ok` with a reading and no error, `failing` after
    a failed read, `never` with neither; `balances_read_at` is when a read last succeeded.
    """

    exchange_key: ExchangeKey
    sync_state: AccountSyncStatus
    last_synced_at: datetime | None
    balances_state: SourceState
    balances_read_at: datetime | None

    @classmethod
    def of(cls, health: ExchangeHealth) -> ExchangeHealthResponse:
        """Render one account."""
        return cls(
            exchange_key=health.exchange_key,
            sync_state=health.sync_state,
            last_synced_at=health.last_synced_at,
            balances_state=health.balances_state,
            balances_read_at=health.balances_read_at,
        )


class ExchangesHealthResponse(BaseModel):
    """Every exchange account, by `exchange_key`. Empty `items` when `unavailable`."""

    state: SectionState
    items: list[ExchangeHealthResponse]

    @classmethod
    def of(cls, health: ExchangesHealth) -> ExchangesHealthResponse:
        """Render the section."""
        return cls(
            state=health.state, items=[ExchangeHealthResponse.of(item) for item in health.items]
        )


class PricesHealthResponse(BaseModel):
    """The stored prices: `fresh`, `stale`, `never` or `unavailable`, and the newest row's
    instant -- `null` when there is none or it could not be read."""

    state: PriceHealthState
    latest_fetched_at: datetime | None

    @classmethod
    def of(cls, health: PricesHealth) -> PricesHealthResponse:
        """Render the section."""
        return cls(state=health.state, latest_fetched_at=health.latest_fetched_at)


class ReconciliationHealthResponse(BaseModel):
    """The holdings check, reduced to a state and three counts. No quantity and no asset.

    `computed_at` is the accounting snapshot's instant. `assets_compared` and
    `assets_mismatched` count the compared assets and those not `match`; `sources_not_compared`
    counts the exchange accounts and wallets left out. All four are `null` when `unavailable`.
    The reconciliation view has the detail.
    """

    state: ReconciliationHealthState
    computed_at: datetime | None
    assets_compared: int | None
    assets_mismatched: int | None
    sources_not_compared: int | None

    @classmethod
    def of(cls, health: ReconciliationHealth) -> ReconciliationHealthResponse:
        """Render the section."""
        return cls(
            state=health.state,
            computed_at=health.computed_at,
            assets_compared=health.assets_compared,
            assets_mismatched=health.assets_mismatched,
            sources_not_compared=health.sources_not_compared,
        )


class HealthDetailResponse(BaseModel):
    """The state of each source the application owns."""

    backup: BackupStatusResponse
    schedulers: list[SchedulerStatusResponse]
    chains: ChainsHealthResponse
    exchanges: ExchangesHealthResponse
    prices: PricesHealthResponse
    reconciliation: ReconciliationHealthResponse

    @classmethod
    def of(cls, detail: HealthDetail) -> HealthDetailResponse:
        """Render every section."""
        return cls(
            backup=BackupStatusResponse.of(detail.backup),
            schedulers=[SchedulerStatusResponse.of(timer) for timer in detail.schedulers],
            chains=ChainsHealthResponse.of(detail.chains),
            exchanges=ExchangesHealthResponse.of(detail.exchanges),
            prices=PricesHealthResponse.of(detail.prices),
            reconciliation=ReconciliationHealthResponse.of(detail.reconciliation),
        )
