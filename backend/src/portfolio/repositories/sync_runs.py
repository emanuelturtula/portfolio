"""Reads and writes of `sync_runs` and `sync_run_chains`, and the vocabulary they hold.

Queries and nothing else: no clock, no policy about what a partial run means. The
repository is handed an `AsyncSession` and it does not commit -- the caller that opened the
unit of work decides when it ends.

## Why the vocabulary lives here rather than in the service that produces it

`SyncTrigger`, `SyncRunStatus`, `SyncErrorKind`, `ChainOutcome` and `SyncRunSummary` are
written down in this module, one layer below the service that fills them in. That is not
where a reader would look first, and the reason is a contract rather than taste.

`services/balance_sync.py` is the only module in `services/` that may import a chain
provider. `services/balances.py` is the read side and is imported by a router, and it needs
`SyncRunSummary` for `GET /api/balances/runs`. Had the summary been defined beside the
sync, the read side would have had to import the module that imports `providers`, and the
spec's rule that "`services/balances.py` imports no provider" would have been broken by a
type annotation. Putting the vocabulary under both of them costs one surprising import and
buys a layering guarantee that does not depend on anyone remembering it.

## Nothing here aggregates money, because there is no money here

Every number in these two tables is a count or a duration -- `INTEGER` columns, not
`NumericText` -- so `MAX`, `ORDER BY` and `COUNT` are all ordinary. The rule about
aggregating in Python is about a `TEXT` money column being coerced to a float, and neither
table has one. `balance_snapshots` holds integer base units and is the same story; see
`repositories/balances.py`.

Runs are listed newest first by **`id`**, an `INTEGER` primary key, rather than by
`started_at`. The coordinator guarantees one run at a time in this process, so identity
order is start order, and an integer sort needs no argument about how a `TEXT` datetime
collates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from portfolio.db.models import SyncRun, SyncRunChain

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ChainOutcome",
    "SyncErrorKind",
    "SyncRunRepository",
    "SyncRunStatus",
    "SyncRunSummary",
    "SyncTrigger",
]


class SyncTrigger(StrEnum):
    """What started a run. A `StrEnum` so the member is its own column value and wire form.

    Three, and `STARTUP` is separate from `SCHEDULED` on purpose: the run that happens when
    the process comes up is the one an operator is looking at when they ask "did the deploy
    work", and folding it into the interval's own ticks would make it invisible.
    """

    SCHEDULED = "scheduled"
    MANUAL = "manual"
    STARTUP = "startup"


class SyncRunStatus(StrEnum):
    """How a run ended, or that it has not.

    `RUNNING` is written before the first provider call and `INTERRUPTED` is what the
    lifespan's sweep leaves behind for a run whose process is gone. Neither is in the
    issue's wording, and without them a crashed run and a live run are the same row.

    `SUCCESS`, `PARTIAL` and `FAILED` also serve as a chain's own outcome within a run,
    where only the first and the last are legal -- a chain either produced balances or it
    raised, and there is no middle case until #54 isolates a single address's refusal.
    `sync_run_chains`'s `CHECK` is what enforces that, rather than a second enum whose two
    members would have to be kept spelled identically to two of these.
    """

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class SyncErrorKind(StrEnum):
    """Whose fault a chain's failure was, as a value an operator can act on.

    The first four are `providers/errors.py`'s vocabulary carried through unchanged: the
    vendor was unreachable, throttled us, answered with something unusable, or there is no
    provider registered for that chain at all.

    **`INTERNAL` is the one that is not about a vendor, and it is the reason this is an
    enumeration rather than a string.** An exception from our own code recorded as
    "unavailable" tells the owner their chain is down, on every sync, for as long as the
    defect survives -- and nothing anywhere mentions the traceback. Keeping it separate is
    what makes a parser bug look like a parser bug.
    """

    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    RESPONSE = "response"
    UNKNOWN_CHAIN = "unknown_chain"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ChainOutcome:
    """What one chain did during one run.

    `wallets_read` counts wallets rather than addresses, because a wallet is what the owner
    registered. A failed chain reads none: the provider contract aborts a whole batch on one
    address's refusal, which is #54's to change and not this layer's to paper over.

    `detail` is the provider's own message and **never names an address, a URL or a response
    body** -- the providers are written that way and this field is rendered by an endpoint,
    so it inherits the rule rather than trusting it.
    """

    chain_key: str
    status: SyncRunStatus
    wallets_read: int
    error_kind: SyncErrorKind | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SyncRunSummary:
    """One run as everything above this layer sees it, chains included.

    A frozen snapshot rather than the ORM rows, for the reason `WalletView` is one: the rows
    are attached to a session the caller closes, and a router serialising one would be
    reading a detached instance.

    `finished_at` and `duration_ms` are both `None` for a run still in flight **and for an
    interrupted one**. They are not two spellings of the same absence -- `status` says which
    -- and filling them in from the sweep's clock would record a duration that is mostly the
    time the process spent dead.
    """

    run_id: int
    trigger: SyncTrigger
    status: SyncRunStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    wallets_total: int
    wallets_succeeded: int
    wallets_failed: int
    chains: tuple[ChainOutcome, ...]


class SyncRunRepository:
    """Every query this application makes against `sync_runs` and `sync_run_chains`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open_run(
        self,
        *,
        trigger: SyncTrigger,
        started_at: datetime,
        wallets_total: int,
    ) -> SyncRun:
        """Insert the run at `running`, with no end time and no duration yet.

        Flushes rather than commits, so the caller's unit of work decides when the row
        becomes durable -- but the caller here commits immediately and on purpose. A row
        that is only visible inside an uncommitted transaction is not evidence that a run
        started; the whole point of writing it first is that a second connection, and a
        process that starts after this one died, can both see it.

        Returns:
            The row, with `id` populated by the flush.
        """
        run = SyncRun(
            trigger=trigger,
            status=SyncRunStatus.RUNNING,
            started_at=started_at,
            finished_at=None,
            duration_ms=None,
            wallets_total=wallets_total,
            wallets_succeeded=0,
            wallets_failed=0,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def finish_run(
        self,
        run_id: int,
        *,
        status: SyncRunStatus,
        finished_at: datetime,
        duration_ms: int,
        wallets_succeeded: int,
        wallets_failed: int,
        chains: Sequence[ChainOutcome],
    ) -> None:
        """Close the run out and record one row per chain that was attempted.

        The chain rows are written here rather than as each chain finishes, so that a run
        has either all of its outcomes or none of them: a half-written set would be
        indistinguishable from a run where the missing chain was never attempted.

        **Takes an `id` rather than the `SyncRun` the caller opened, and an `UPDATE` rather
        than an attribute assignment.** The sync commits per chain, so a chain whose
        snapshot write fails rolls that chain's work back -- and a rollback expires every
        persistent object on the session, including the run. Touching an expired attribute
        afterwards is an implicit lazy load, which under an async session is a
        `MissingGreenlet` rather than a query. An integer cannot be expired.
        """
        await self._session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(
                status=status,
                finished_at=finished_at,
                duration_ms=duration_ms,
                wallets_succeeded=wallets_succeeded,
                wallets_failed=wallets_failed,
            )
        )
        for outcome in chains:
            self._session.add(
                SyncRunChain(
                    sync_run_id=run_id,
                    chain_key=outcome.chain_key,
                    status=outcome.status,
                    wallets_read=outcome.wallets_read,
                    error_kind=outcome.error_kind,
                    detail=outcome.detail,
                )
            )
        await self._session.flush()

    async def sweep_interrupted(self) -> int:
        """Mark every run still at `running` as `interrupted`. Returns how many.

        **An orphan is a real state, not a defensive check.** Writing the row before the
        first provider call means a process that dies mid-run leaves one behind, and a table
        where a crashed run and a live run look identical is worse than one that never
        recorded the crash at all. The lifespan sweeps at startup, before the scheduler
        starts, and again at shutdown after a run that outlived the grace period was
        cancelled.

        Safe to run when there is nothing to sweep, which is the ordinary case: it updates
        no rows and returns zero. It is also safe to run at startup *because* there is one
        instance of this application -- a second process would sweep the first one's live
        run, and the single-instance deployment is what makes that impossible rather than
        merely unlikely.

        `finished_at` and `duration_ms` are deliberately left `NULL`. The run has no honest
        end time, and stamping this sweep's clock onto it would record a duration that is
        mostly however long the container was down.

        Two statements rather than one `UPDATE` reporting its own `rowcount`, because
        `Session.execute` is typed as returning a `Result` and only a `CursorResult` carries
        that attribute -- so the one-statement version needs a cast, which is a claim about
        a type rather than a check of one. The gap between the two statements is not a race
        here: there is one instance of this application, and both callers run with the
        scheduler stopped.
        """
        running = list(
            await self._session.scalars(
                select(SyncRun.id).where(SyncRun.status == SyncRunStatus.RUNNING)
            )
        )
        if not running:
            return 0
        await self._session.execute(
            update(SyncRun).where(SyncRun.id.in_(running)).values(status=SyncRunStatus.INTERRUPTED)
        )
        return len(running)

    async def latest_finished_at(self) -> datetime | None:
        """When the newest *finished* run ended, or `None` if none ever has.

        What the scheduler's startup condition reads: a fresh deployment syncs immediately
        rather than showing an empty dashboard for a whole interval, and a container that is
        crash-looping does not hit two public indexes on every restart.

        `ORDER BY id DESC` rather than `MAX(finished_at)`: the coordinator allows one run at
        a time in this process, so identity order is finish order, and an `INTEGER` sort
        needs no argument about a `TEXT` datetime's collation. A run still in flight and an
        interrupted one both carry `NULL` here and are skipped, which is what "finished"
        means.
        """
        found: datetime | None = await self._session.scalar(
            select(SyncRun.finished_at)
            .where(SyncRun.finished_at.is_not(None))
            .order_by(SyncRun.id.desc())
            .limit(1)
        )
        return found

    async def list_runs(self, *, limit: int) -> list[SyncRunSummary]:
        """The most recent runs, newest first, each with its chains attached.

        Two queries regardless of how many runs come back: one for the runs and one for
        every chain row belonging to them. One query per run would be the shape that looks
        fine at ten rows and is the reason a page takes a second at two hundred.
        """
        runs = list(
            await self._session.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(limit))
        )
        if not runs:
            return []

        chains_by_run: dict[int, list[ChainOutcome]] = {run.id: [] for run in runs}
        rows = await self._session.scalars(
            select(SyncRunChain)
            .where(SyncRunChain.sync_run_id.in_(chains_by_run))
            # Sorted by chain key so that two renderings of one run list its chains in the
            # same order; the insertion order is whichever coroutine finished first, which
            # is not a fact worth publishing.
            .order_by(SyncRunChain.sync_run_id, SyncRunChain.chain_key)
        )
        for row in rows:
            chains_by_run[row.sync_run_id].append(
                ChainOutcome(
                    chain_key=row.chain_key,
                    status=SyncRunStatus(row.status),
                    wallets_read=row.wallets_read,
                    error_kind=None if row.error_kind is None else SyncErrorKind(row.error_kind),
                    detail=row.detail,
                )
            )
        return [summary_of(run, tuple(chains_by_run[run.id])) for run in runs]


def summary_of(run: SyncRun, chains: tuple[ChainOutcome, ...]) -> SyncRunSummary:
    """Snapshot a run row and its outcomes while the session is still open.

    The enum constructors are what turn a column value back into the vocabulary above. A
    value the `CHECK` constraints admit always converts; one they do not could only come
    from a row written outside this application, and a `ValueError` naming it is a better
    outcome than a summary carrying a status nothing downstream recognises.
    """
    return SyncRunSummary(
        run_id=run.id,
        trigger=SyncTrigger(run.trigger),
        status=SyncRunStatus(run.status),
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        wallets_total=run.wallets_total,
        wallets_succeeded=run.wallets_succeeded,
        wallets_failed=run.wallets_failed,
        chains=chains,
    )
