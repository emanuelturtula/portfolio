"""Response models for `GET /api/health/detail`: how the application's own sources stand.

`backup` came first (#22, spec 029). #23 (spec 030) adds the others beside it: the four
timers, the balance sync per chain and the prices. That is why the payload is an object keyed
by source rather than the backup's fields at the top.

## No configuration value is served

Not the backup directory, not an interval, a retention, a URL, a key or an age limit. They are
the operator's, they are in the operator's environment file, and an endpoint is not where
anybody needs to read them back. What is served is what the owner can act on: what
each source's last recorded attempt says, and when.

## Every state is an enum whose members are the wire form

So the generated TypeScript types are unions of exactly these strings, and a page's wording
table can be total. The enums are the service's; this module only renders them.

## A section that could not be read

`chains` and `prices` each have an `unavailable` state, served when its read raised: `items`
is then empty and every other field null. The log says why, as
`health_section_failed`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

# Runtime imports, not `TYPE_CHECKING` ones: Pydantic resolves a field's type when the model
# class is created.
from portfolio.services.backup import BackupErrorKind, BackupState, BackupStatus
from portfolio.services.health import (
    ChainHealth,
    ChainsHealth,
    HealthDetail,
    PriceHealthState,
    PricesHealth,
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
    """One of the five timers.

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


class PricesHealthResponse(BaseModel):
    """The stored prices: `fresh`, `stale`, `never` or `unavailable`, and the newest row's
    instant -- `null` when there is none or it could not be read."""

    state: PriceHealthState
    latest_fetched_at: datetime | None

    @classmethod
    def of(cls, health: PricesHealth) -> PricesHealthResponse:
        """Render the section."""
        return cls(state=health.state, latest_fetched_at=health.latest_fetched_at)


class HealthDetailResponse(BaseModel):
    """The state of each source the application owns."""

    backup: BackupStatusResponse
    schedulers: list[SchedulerStatusResponse]
    chains: ChainsHealthResponse
    prices: PricesHealthResponse

    @classmethod
    def of(cls, detail: HealthDetail) -> HealthDetailResponse:
        """Render every section."""
        return cls(
            backup=BackupStatusResponse.of(detail.backup),
            schedulers=[SchedulerStatusResponse.of(timer) for timer in detail.schedulers],
            chains=ChainsHealthResponse.of(detail.chains),
            prices=PricesHealthResponse.of(detail.prices),
        )
