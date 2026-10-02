"""Criterion 4's storage: the run row, its chain outcomes, and the orphan sweep.

`sync_runs` is the table that answers "has this thing been running at all", which is the
question #23 will be built on and the one an operator asks first when a balance looks old.
Three of its properties are worth more than the rest and each has its own section below:

* **a run is opened before any work happens**, so a process that dies mid-run leaves a row
  rather than leaving nothing;
* **an orphan is a real state**, swept to `interrupted` at startup -- without which a
  crashed run and a live run are the same row;
* **`finished_at` stays `NULL` for an interrupted run**, because a sweep's own clock
  reading would record a duration that is mostly however long the container was down.

The `CHECK` constraints are exercised with real inserts rather than only reflected. A
constraint whose text matches the model and which the migration never actually created
would pass a reflection test on the *model* and fail on the Pi; going through the migrated
file and watching an insert be refused is what closes that gap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.engine import create_session_factory
from portfolio.db.models import (
    SyncRun,
)
from portfolio.repositories.sync_runs import (
    ChainOutcome,
    SyncErrorKind,
    SyncRunRepository,
    SyncRunStatus,
    SyncRunSummary,
    SyncTrigger,
)
from tests.balance_harness import sqlite_timestamp

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

STARTED_AT: Final = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
FINISHED_AT: Final = STARTED_AT + timedelta(seconds=3)
DURATION_MS: Final = 3128

BITCOIN: Final = "bitcoin"
KASPA: Final = "kaspa"


@pytest.fixture
async def session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session from the application's own factory, over the migrated file."""
    factory = create_session_factory(migrated_engine)
    async with factory() as opened:
        yield opened


@pytest.fixture
def repository(session: AsyncSession) -> SyncRunRepository:
    return SyncRunRepository(session)


async def row_of(session: AsyncSession, run_id: int) -> dict[str, object]:
    """One `sync_runs` row as SQLite has it, around the ORM and its type decorators."""
    result = await session.execute(
        text(
            "SELECT trigger, status, started_at, finished_at, duration_ms, "
            "wallets_total, wallets_succeeded, wallets_failed FROM sync_runs WHERE id = :id"
        ),
        {"id": run_id},
    )
    return dict(result.mappings().one())


async def chain_rows(session: AsyncSession) -> list[dict[str, object]]:
    result = await session.execute(
        text(
            "SELECT sync_run_id, chain_key, status, wallets_read, error_kind, detail "
            "FROM sync_run_chains ORDER BY sync_run_id, chain_key"
        )
    )
    return [dict(row) for row in result.mappings().all()]


async def open_and_commit(
    session: AsyncSession,
    repository: SyncRunRepository,
    *,
    trigger: SyncTrigger = SyncTrigger.MANUAL,
    started_at: datetime = STARTED_AT,
    wallets_total: int = 2,
) -> int:
    """Open a run and make it durable, which is what the sync itself does immediately."""
    run = await repository.open_run(
        trigger=trigger, started_at=started_at, wallets_total=wallets_total
    )
    await session.commit()
    return run.id


# --------------------------------------------------------------------------------------
# A run is opened first, and completed later
# --------------------------------------------------------------------------------------


async def test_a_run_is_opened_at_running_with_no_end_time_and_no_duration(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """Criterion 4's first half: the row exists before the work does, and says so.

    `running` is a real status rather than a placeholder, and `finished_at IS NULL` is what
    the sweep looks for. A row opened at `success` would make an orphan indistinguishable
    from a run that worked.
    """
    run_id = await open_and_commit(session, repository, wallets_total=5)
    session.expunge_all()

    row = await row_of(session, run_id)

    assert row["status"] == SyncRunStatus.RUNNING
    assert row["trigger"] == SyncTrigger.MANUAL
    assert row["started_at"] == sqlite_timestamp(STARTED_AT)
    assert row["finished_at"] is None
    assert row["duration_ms"] is None
    assert row["wallets_total"] == 5
    assert (row["wallets_succeeded"], row["wallets_failed"]) == (0, 0)


async def test_finishing_writes_the_status_the_counts_and_both_clocks(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """Criterion 4's second half, read off the row rather than off the return value.

    `duration_ms` is handed in rather than computed here, because it comes from a monotonic
    counter the service owns -- and it is deliberately *not* `finished_at - started_at`:
    3128 milliseconds against a three-second wall-clock gap, so a repository that quietly
    recomputed it from the timestamps would fail this.
    """
    run_id = await open_and_commit(session, repository)

    await repository.finish_run(
        run_id,
        status=SyncRunStatus.PARTIAL,
        finished_at=FINISHED_AT,
        duration_ms=DURATION_MS,
        wallets_succeeded=1,
        wallets_failed=1,
        chains=(),
    )
    await session.commit()
    session.expunge_all()

    row = await row_of(session, run_id)
    assert row["status"] == SyncRunStatus.PARTIAL
    assert row["finished_at"] == sqlite_timestamp(FINISHED_AT)
    assert row["duration_ms"] == DURATION_MS
    assert (row["wallets_succeeded"], row["wallets_failed"]) == (1, 1)


async def test_finishing_writes_one_row_per_chain_and_each_belongs_to_its_run(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """Two runs, two sets of chains, and nothing pooled between them.

    Written at the end rather than as each chain finishes, so a run has either all of its
    outcomes or none: a half-written set is indistinguishable from a run where the missing
    chain was never attempted, and an operator reading it would conclude Kaspa was skipped
    rather than that the process died.
    """
    first = await open_and_commit(session, repository)
    await repository.finish_run(
        first,
        status=SyncRunStatus.PARTIAL,
        finished_at=FINISHED_AT,
        duration_ms=DURATION_MS,
        wallets_succeeded=1,
        wallets_failed=1,
        chains=(
            ChainOutcome(
                chain_key=BITCOIN,
                status=SyncRunStatus.SUCCESS,
                wallets_read=1,
                error_kind=None,
                detail=None,
            ),
            ChainOutcome(
                chain_key=KASPA,
                status=SyncRunStatus.FAILED,
                wallets_read=0,
                error_kind=SyncErrorKind.RATE_LIMITED,
                detail="the vendor asked us to slow down",
            ),
        ),
    )
    second = await open_and_commit(session, repository)
    await repository.finish_run(
        second,
        status=SyncRunStatus.SUCCESS,
        finished_at=FINISHED_AT,
        duration_ms=1,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=(
            ChainOutcome(
                chain_key=BITCOIN,
                status=SyncRunStatus.SUCCESS,
                wallets_read=1,
                error_kind=None,
                detail=None,
            ),
        ),
    )
    await session.commit()

    rows = await chain_rows(session)

    assert [(row["sync_run_id"], row["chain_key"]) for row in rows] == [
        (first, BITCOIN),
        (first, KASPA),
        (second, BITCOIN),
    ]
    kaspa = rows[1]
    assert kaspa["status"] == SyncRunStatus.FAILED
    assert kaspa["error_kind"] == SyncErrorKind.RATE_LIMITED
    assert kaspa["detail"] == "the vendor asked us to slow down"
    assert rows[0]["error_kind"] is None
    assert rows[0]["detail"] is None


async def test_one_run_may_not_record_a_chain_twice(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """`UNIQUE (sync_run_id, chain_key)`. Two rows for one chain is two verdicts on it.

    Which one an operations view renders would then be whichever the query returned first,
    and "Kaspa succeeded" and "Kaspa failed" would both be in the table for one run.
    """
    run_id = await open_and_commit(session, repository)
    outcome = ChainOutcome(
        chain_key=BITCOIN,
        status=SyncRunStatus.SUCCESS,
        wallets_read=1,
        error_kind=None,
        detail=None,
    )

    with pytest.raises(IntegrityError):
        await repository.finish_run(
            run_id,
            status=SyncRunStatus.SUCCESS,
            finished_at=FINISHED_AT,
            duration_ms=1,
            wallets_succeeded=1,
            wallets_failed=0,
            chains=(outcome, outcome),
        )


async def test_deleting_a_run_takes_its_chain_rows_with_it(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """`ON DELETE CASCADE`, so pruning old runs cannot leave outcomes pointing at nothing."""
    run_id = await open_and_commit(session, repository)
    await repository.finish_run(
        run_id,
        status=SyncRunStatus.SUCCESS,
        finished_at=FINISHED_AT,
        duration_ms=1,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=(
            ChainOutcome(
                chain_key=BITCOIN,
                status=SyncRunStatus.SUCCESS,
                wallets_read=1,
                error_kind=None,
                detail=None,
            ),
        ),
    )
    await session.commit()

    await session.execute(text("DELETE FROM sync_runs WHERE id = :id"), {"id": run_id})
    await session.commit()

    assert await chain_rows(session) == []


# --------------------------------------------------------------------------------------
# The orphan sweep
# --------------------------------------------------------------------------------------


async def test_a_running_row_from_a_dead_process_is_swept_to_interrupted(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """The spec's extra status, and the reason it exists.

    A run the process died in the middle of leaves a `running` row that nothing will ever
    close, and a table where that row and a live run look identical cannot answer "is a sync
    happening right now". The sweep runs at startup, before the scheduler, and turns the
    stale one into a state somebody can read.
    """
    orphan = await open_and_commit(session, repository)

    swept = await repository.sweep_interrupted()
    await session.commit()
    session.expunge_all()

    assert swept == 1
    assert (await row_of(session, orphan))["status"] == SyncRunStatus.INTERRUPTED


async def test_an_interrupted_run_keeps_a_null_end_time_and_no_duration(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """The sweep does not invent a finish time, and that is a decision rather than an omission.

    Stamping the sweep's own clock would record a `duration_ms` that is mostly however long
    the container was down -- a number that looks like a measurement of the sync and is a
    measurement of the outage. `NULL` says "we do not know", which is true.
    """
    orphan = await open_and_commit(session, repository)

    await repository.sweep_interrupted()
    await session.commit()
    session.expunge_all()

    row = await row_of(session, orphan)
    assert row["finished_at"] is None
    assert row["duration_ms"] is None


async def test_the_sweep_leaves_a_finished_run_alone(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """The control: a sweep that rewrote every row would pass the test above perfectly."""
    finished = await open_and_commit(session, repository)
    await repository.finish_run(
        finished,
        status=SyncRunStatus.SUCCESS,
        finished_at=FINISHED_AT,
        duration_ms=DURATION_MS,
        wallets_succeeded=2,
        wallets_failed=0,
        chains=(),
    )
    await session.commit()
    orphan = await open_and_commit(session, repository)

    swept = await repository.sweep_interrupted()
    await session.commit()
    session.expunge_all()

    assert swept == 1
    assert (await row_of(session, finished))["status"] == SyncRunStatus.SUCCESS
    assert (await row_of(session, finished))["duration_ms"] == DURATION_MS
    assert (await row_of(session, orphan))["status"] == SyncRunStatus.INTERRUPTED


async def test_sweeping_a_table_with_nothing_to_sweep_reports_zero(
    repository: SyncRunRepository,
) -> None:
    """The ordinary case, on every clean restart for the life of the deployment."""
    assert await repository.sweep_interrupted() == 0


# --------------------------------------------------------------------------------------
# What the scheduler's startup condition reads
# --------------------------------------------------------------------------------------


async def test_the_latest_attempt_is_none_on_a_fresh_database(
    repository: SyncRunRepository,
) -> None:
    """`None` is what makes a fresh deployment sync immediately instead of waiting."""
    assert await repository.latest_started_at() is None


@pytest.mark.parametrize(
    "newest_status",
    ["interrupted", "running", "failed", "success"],
)
async def test_the_latest_attempt_counts_a_run_of_any_status(
    session: AsyncSession,
    repository: SyncRunRepository,
    newest_status: str,
) -> None:
    """An attempt is an attempt, whatever became of it -- which is what review changed.

    The old condition read the newest *finished* run, so a container that crashed mid-sync
    on every start never finished one: each restart found the last success days old,
    decided a sync was due, and started another one straight into the same crash. Counting
    attempts is what stops a crash loop from also being a loop of vendor calls.

    Every status is driven, the one run that finished long ago is older than all of them,
    and the answer is always the newest run's own `started_at`.
    """
    long_ago = await open_and_commit(session, repository, started_at=STARTED_AT - timedelta(days=2))
    await repository.finish_run(
        long_ago,
        status=SyncRunStatus.SUCCESS,
        finished_at=STARTED_AT - timedelta(days=2),
        duration_ms=1,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=(),
    )
    await session.commit()
    await insert_run_with(session, trigger="startup", status=newest_status)
    await session.commit()

    assert await repository.latest_started_at() == STARTED_AT


async def test_the_latest_attempt_is_the_newest_by_identity_not_by_clock(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """Newest by `id`, so a wall clock stepped backwards between two runs cannot reorder them.

    The later run is given the *earlier* `started_at`. Resolved by `MAX(started_at)` the
    answer would be the older run's, which is the hazard `duration_ms` exists to sidestep,
    arriving here through a different column.
    """
    await open_and_commit(session, repository, started_at=STARTED_AT)
    await open_and_commit(session, repository, started_at=STARTED_AT - timedelta(hours=1))

    assert await repository.latest_started_at() == STARTED_AT - timedelta(hours=1)


# --------------------------------------------------------------------------------------
# The listing the runs endpoint is built on
# --------------------------------------------------------------------------------------


async def test_listing_runs_is_newest_first_and_honours_the_limit(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """Newest first, because the question an operator asks is "what happened last"."""
    identifiers = []
    for _ in range(3):
        run_id = await open_and_commit(session, repository)
        await repository.finish_run(
            run_id,
            status=SyncRunStatus.SUCCESS,
            finished_at=FINISHED_AT,
            duration_ms=1,
            wallets_succeeded=1,
            wallets_failed=0,
            chains=(),
        )
        await session.commit()
        identifiers.append(run_id)

    everything = await repository.list_runs(limit=10)
    bounded = await repository.list_runs(limit=2)

    assert [summary.run_id for summary in everything] == list(reversed(identifiers))
    assert [summary.run_id for summary in bounded] == list(reversed(identifiers))[:2]


async def test_listing_runs_attaches_each_run_only_its_own_chains(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """One query for the chains, and the join still has to be right.

    Two runs with different chain sets, so a listing that attached every chain row to every
    run -- the shape a missing `sync_run_id` grouping produces -- reports Kaspa as having
    failed in a run it was not part of.
    """
    first = await open_and_commit(session, repository)
    await repository.finish_run(
        first,
        status=SyncRunStatus.PARTIAL,
        finished_at=FINISHED_AT,
        duration_ms=1,
        wallets_succeeded=1,
        wallets_failed=1,
        chains=(
            ChainOutcome(
                chain_key=KASPA,
                status=SyncRunStatus.FAILED,
                wallets_read=0,
                error_kind=SyncErrorKind.INTERNAL,
                detail="our own bug",
            ),
            ChainOutcome(
                chain_key=BITCOIN,
                status=SyncRunStatus.SUCCESS,
                wallets_read=1,
                error_kind=None,
                detail=None,
            ),
        ),
    )
    await session.commit()
    second = await open_and_commit(session, repository)
    await repository.finish_run(
        second,
        status=SyncRunStatus.SUCCESS,
        finished_at=FINISHED_AT,
        duration_ms=1,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=(
            ChainOutcome(
                chain_key=BITCOIN,
                status=SyncRunStatus.SUCCESS,
                wallets_read=1,
                error_kind=None,
                detail=None,
            ),
        ),
    )
    await session.commit()

    runs = await repository.list_runs(limit=10)

    assert [summary.run_id for summary in runs] == [second, first]
    assert [chain.chain_key for chain in runs[0].chains] == [BITCOIN]
    # Sorted by chain key rather than by insertion order, so two renderings agree.
    assert [chain.chain_key for chain in runs[1].chains] == [BITCOIN, KASPA]
    assert runs[1].chains[1].error_kind is SyncErrorKind.INTERNAL


async def test_listing_an_empty_table_is_an_empty_list(repository: SyncRunRepository) -> None:
    """The first day, again: no runs is not an error."""
    assert await repository.list_runs(limit=50) == []


# --------------------------------------------------------------------------------------
# What the holdings check reads: the latest run that ran to its end (spec 028)
# --------------------------------------------------------------------------------------

READ: Final = ChainOutcome(chain_key=BITCOIN, status=SyncRunStatus.SUCCESS, wallets_read=2)
KASPA_DOWN: Final = ChainOutcome(
    chain_key=KASPA,
    status=SyncRunStatus.FAILED,
    wallets_read=0,
    error_kind=SyncErrorKind.UNAVAILABLE,
    detail="no configured endpoint answered",
)


async def finish(
    session: AsyncSession,
    repository: SyncRunRepository,
    status: SyncRunStatus,
    chains: tuple[ChainOutcome, ...] = (),
    *,
    started_at: datetime = STARTED_AT,
    finished_at: datetime = FINISHED_AT,
) -> int:
    """One run opened and closed out by the repository's own writers, as the sync does."""
    run_id = await open_and_commit(
        session, repository, trigger=SyncTrigger.SCHEDULED, started_at=started_at
    )
    await repository.finish_run(
        run_id,
        status=status,
        finished_at=finished_at,
        duration_ms=DURATION_MS,
        wallets_succeeded=sum(chain.wallets_read for chain in chains),
        wallets_failed=sum(1 for chain in chains if chain.status is SyncRunStatus.FAILED),
        chains=chains,
    )
    await session.commit()
    return run_id


async def leave_unfinished(
    session: AsyncSession, repository: SyncRunRepository, status: str
) -> int:
    """A run that never ran to its end: still `running`, or swept to `interrupted`."""
    run_id = await open_and_commit(session, repository)
    if status == "interrupted":
        assert await repository.sweep_interrupted() == 1
        await session.commit()
    assert (await row_of(session, run_id))["status"] == status
    return run_id


async def test_the_latest_finished_run_is_none_on_a_fresh_database(
    repository: SyncRunRepository,
) -> None:
    """No run has finished, so no chain is known to have failed."""
    assert await repository.latest_finished() is None


@pytest.mark.parametrize("status", ["running", "interrupted"])
async def test_a_run_that_never_ran_to_its_end_is_not_a_finished_one(
    session: AsyncSession, repository: SyncRunRepository, status: str
) -> None:
    """The only runs there are did not finish: two of them, so "the newest" is not `None`
    by there being one row too few."""
    await leave_unfinished(session, repository, status)
    await leave_unfinished(session, repository, status)

    assert await repository.latest_finished() is None
    assert await repository.latest_started_at() == STARTED_AT, (
        "the control: the timer's question counts those same runs"
    )


@pytest.mark.parametrize(
    "status", [SyncRunStatus.SUCCESS, SyncRunStatus.PARTIAL, SyncRunStatus.FAILED]
)
async def test_each_status_of_a_run_that_ended_is_finished_and_is_answered_whole(
    session: AsyncSession, repository: SyncRunRepository, status: SyncRunStatus
) -> None:
    """`success`, `partial` and `failed` alike, with every field of the run and its chains."""
    run_id = await finish(session, repository, status, (KASPA_DOWN, READ))
    session.expunge_all()

    found = await repository.latest_finished()

    assert found == SyncRunSummary(
        run_id=run_id,
        trigger=SyncTrigger.SCHEDULED,
        status=status,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        duration_ms=DURATION_MS,
        wallets_total=2,
        wallets_succeeded=2,
        wallets_failed=1,
        chains=(READ, KASPA_DOWN),
    )
    assert found is not None
    assert isinstance(found.chains, tuple)
    assert found.chains[1].status is SyncRunStatus.FAILED
    assert found.chains[1].error_kind is SyncErrorKind.UNAVAILABLE
    assert found.started_at.tzinfo is not None


@pytest.mark.parametrize("newest", ["running", "interrupted"])
@pytest.mark.parametrize(
    "older", [SyncRunStatus.SUCCESS, SyncRunStatus.PARTIAL, SyncRunStatus.FAILED]
)
async def test_a_newer_run_that_did_not_finish_does_not_hide_the_finished_one_before_it(
    session: AsyncSession, repository: SyncRunRepository, older: SyncRunStatus, newest: str
) -> None:
    """Criterion 4 of spec 028, at the query: the run before a `running` or an `interrupted`
    one still stands, whichever of the three finished statuses it has."""
    finished_run = await finish(session, repository, older, (KASPA_DOWN,))
    unfinished = await leave_unfinished(session, repository, newest)
    assert unfinished > finished_run

    found = await repository.latest_finished()

    assert found is not None
    assert found.run_id == finished_run
    assert found.status is older
    assert found.chains == (KASPA_DOWN,)
    assert (await repository.list_runs(limit=1))[0].run_id == unfinished, (
        "the control: the newest run of any status is the other one"
    )


@pytest.mark.parametrize("newest", ["running", "interrupted"])
async def test_a_chain_row_under_an_unfinished_run_does_not_make_it_finished(
    session: AsyncSession, repository: SyncRunRepository, newest: str
) -> None:
    """No writer leaves one. The run is chosen by its status, so a row planted there by hand
    changes nothing: the chains answered are the finished run's own."""
    finished_run = await finish(session, repository, SyncRunStatus.SUCCESS, (READ,))
    await insert_run_with(session, trigger="scheduled", status=newest)
    unfinished = finished_run + 1
    await insert_chain_with(
        session, unfinished, chain_key=BITCOIN, status="failed", error_kind="unavailable"
    )
    await session.commit()

    found = await repository.latest_finished()

    assert found is not None
    assert found.run_id == finished_run
    assert found.chains == (READ,)


async def test_the_latest_finished_run_is_the_newest_by_identity_not_by_either_clock(
    session: AsyncSession, repository: SyncRunRepository
) -> None:
    """The later run is given the earlier `started_at` and the earlier `finished_at`.

    Both are `TEXT` in SQLite. Resolved by either of them, the answer would be the older
    run and its verdict on Kaspa the opposite one.
    """
    older = await finish(session, repository, SyncRunStatus.SUCCESS, (READ,))
    newer = await finish(
        session,
        repository,
        SyncRunStatus.FAILED,
        (KASPA_DOWN,),
        started_at=STARTED_AT - timedelta(days=3),
        finished_at=FINISHED_AT - timedelta(days=3),
    )
    assert newer > older

    found = await repository.latest_finished()

    assert found is not None
    assert found.run_id == newer
    assert found.status is SyncRunStatus.FAILED
    assert found.chains == (KASPA_DOWN,)


async def test_the_latest_finished_run_follows_each_run_that_finishes(
    session: AsyncSession, repository: SyncRunRepository
) -> None:
    """Three runs finishing one after the other: the answer is the last one each time, with
    its own chains and none of the others'."""
    seen: list[tuple[int, tuple[str, ...]]] = []
    expected: list[tuple[int, tuple[str, ...]]] = []
    for status, chains in (
        (SyncRunStatus.PARTIAL, (READ, KASPA_DOWN)),
        (SyncRunStatus.SUCCESS, (READ,)),
        (SyncRunStatus.FAILED, (KASPA_DOWN,)),
    ):
        run_id = await finish(session, repository, status, chains)
        found = await repository.latest_finished()
        assert found is not None
        seen.append((found.run_id, tuple(chain.chain_key for chain in found.chains)))
        expected.append((run_id, tuple(chain.chain_key for chain in chains)))

    assert seen == expected
    assert [chains for _run, chains in seen] == [(BITCOIN, KASPA), (BITCOIN,), (KASPA,)]


async def test_a_finished_run_over_no_wallet_has_no_chains(
    session: AsyncSession, repository: SyncRunRepository
) -> None:
    """A run with nothing to read records no chain, and it is still the latest finished one:
    the run before it, which failed Kaspa, no longer decides anything."""
    await finish(session, repository, SyncRunStatus.FAILED, (KASPA_DOWN,))
    empty = await finish(session, repository, SyncRunStatus.SUCCESS)

    found = await repository.latest_finished()

    assert found is not None
    assert found.run_id == empty
    assert found.chains == ()


async def test_the_latest_finished_run_is_the_same_summary_the_listing_gives(
    session: AsyncSession, repository: SyncRunRepository
) -> None:
    """One reading of a run, whichever method is asked: the endpoint the owner is sent to
    (`GET /api/balances/runs`) and the holdings check cannot disagree about a chain."""
    await finish(session, repository, SyncRunStatus.PARTIAL, (KASPA_DOWN, READ))
    await leave_unfinished(session, repository, "running")

    listed = await repository.list_runs(limit=10)

    assert await repository.latest_finished() == listed[1]


async def test_the_latest_finished_run_is_two_statements_on_status_and_identity(
    session: AsyncSession, repository: SyncRunRepository
) -> None:
    """Criterion 8 of spec 028: the run is filtered on `status` and ordered on `id`.

    `started_at` and `finished_at` are `TEXT`. Neither is compared, ordered on or
    aggregated, and the request stays at two statements with five runs in the table.
    """
    for status in (SyncRunStatus.SUCCESS, SyncRunStatus.PARTIAL, SyncRunStatus.FAILED):
        await finish(session, repository, status, (READ, KASPA_DOWN))
    await leave_unfinished(session, repository, "interrupted")
    await leave_unfinished(session, repository, "running")
    statements: list[tuple[str, Any]] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append((" ".join(statement.upper().split()), parameters))

    engine = session.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        found = await repository.latest_finished()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert found is not None
    assert found.status is SyncRunStatus.FAILED
    assert len(statements) == 2, statements
    (the_run, run_parameters), (the_chains, chain_parameters) = statements
    assert " FROM SYNC_RUNS " in the_run
    assert "SYNC_RUNS.STATUS IN" in the_run
    assert sorted(run_parameters[:3]) == ["failed", "partial", "success"]
    assert the_run.split(" ORDER BY ", 1)[1].startswith("SYNC_RUNS.ID DESC")
    assert " LIMIT " in the_run
    assert " FROM SYNC_RUN_CHAINS " in the_chains
    assert "SYNC_RUN_CHAINS.SYNC_RUN_ID = ?" in the_chains
    assert tuple(chain_parameters) == (found.run_id,)
    assert the_chains.split(" ORDER BY ", 1)[1] == "SYNC_RUN_CHAINS.CHAIN_KEY"
    for statement, _parameters in statements:
        tail = statement.split(" FROM ", 1)[1]
        for instant in ("STARTED_AT", "FINISHED_AT"):
            assert instant not in tail, f"{instant} is compared or ordered on: {statement}"
        for aggregate in ("SUM(", "TOTAL(", "AVG(", "MIN(", "MAX(", "COUNT("):
            assert aggregate not in statement, statement


# --------------------------------------------------------------------------------------
# The constraints, exercised against the migrated file rather than only reflected
# --------------------------------------------------------------------------------------


async def insert_run_with(session: AsyncSession, *, trigger: str, status: str) -> None:
    await session.execute(
        text(
            "INSERT INTO sync_runs (trigger, status, started_at, wallets_total, "
            "wallets_succeeded, wallets_failed) "
            "VALUES (:trigger, :status, :started_at, 0, 0, 0)"
        ),
        {"trigger": trigger, "status": status, "started_at": sqlite_timestamp(STARTED_AT)},
    )


@pytest.mark.parametrize("trigger", ["scheduled", "manual", "startup"])
async def test_the_trigger_check_admits_each_member(
    session: AsyncSession,
    trigger: str,
) -> None:
    """Every `SyncTrigger` member has to be storable, or one caller cannot record its run."""
    await insert_run_with(session, trigger=trigger, status="running")
    await session.commit()

    assert (await session.scalars(select(SyncRun.trigger))).all() == [trigger]


@pytest.mark.parametrize("trigger", ["Manual", "cron", "", "manual "])
async def test_the_trigger_check_refuses_anything_else(
    session: AsyncSession,
    trigger: str,
) -> None:
    """Case and whitespace matter: the column holds one spelling of each trigger.

    A `"Manual"` that got in would render beside `"manual"` in the runs endpoint as a
    different trigger entirely, and no query filtering on one would find the other.
    """
    with pytest.raises(IntegrityError):
        await insert_run_with(session, trigger=trigger, status="running")


@pytest.mark.parametrize(
    "status",
    ["running", "success", "partial", "failed", "interrupted"],
)
async def test_the_status_check_admits_each_member(session: AsyncSession, status: str) -> None:
    """All five, including the two the issue did not ask for and the spec argued into being."""
    await insert_run_with(session, trigger="manual", status=status)
    await session.commit()

    assert (await session.scalars(select(SyncRun.status))).all() == [status]


@pytest.mark.parametrize("status", ["pending", "ok", "SUCCESS"])
async def test_the_status_check_refuses_anything_else(
    session: AsyncSession,
    status: str,
) -> None:
    """A sixth status invented in a later change is a migration, not a string literal."""
    with pytest.raises(IntegrityError):
        await insert_run_with(session, trigger="manual", status=status)


async def insert_chain_with(
    session: AsyncSession,
    run_id: int,
    *,
    chain_key: str = BITCOIN,
    status: str = "success",
    error_kind: str | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO sync_run_chains (sync_run_id, chain_key, status, wallets_read, "
            "error_kind, detail) VALUES (:run_id, :chain_key, :status, 0, :error_kind, NULL)"
        ),
        {
            "run_id": run_id,
            "chain_key": chain_key,
            "status": status,
            "error_kind": error_kind,
        },
    )


@pytest.mark.parametrize(
    "error_kind",
    [
        None,
        "unavailable",
        "rate_limited",
        "response",
        "unknown_chain",
        # The owner's mistake, added to the vocabulary after implementation found that a
        # wrong-network wallet was being filed as a defect in this application.
        "address_rejected",
        "internal",
    ],
)
async def test_the_error_kind_check_admits_null_and_each_member(
    session: AsyncSession,
    repository: SyncRunRepository,
    error_kind: str | None,
) -> None:
    """The six kinds plus `NULL`, which is what a chain that succeeded carries.

    Three groups, and the split is the whole reason the column exists: four of them name a
    vendor, `address_rejected` names the owner, and `internal` names us. A `TypeError` filed
    as an outage and a wrong-network wallet filed as a defect are the same mistake in
    opposite directions, and both are invisible until somebody reads a status page for a
    vendor that was fine.
    """
    run_id = await open_and_commit(session, repository)

    await insert_chain_with(session, run_id, error_kind=error_kind)
    await session.commit()

    stored: str = (
        await session.execute(text("SELECT error_kind FROM sync_run_chains"))
    ).scalar_one()
    assert stored == error_kind


@pytest.mark.parametrize("error_kind", ["timeout", "unknown", "INTERNAL", ""])
async def test_the_error_kind_check_refuses_a_vocabulary_of_its_own(
    session: AsyncSession,
    repository: SyncRunRepository,
    error_kind: str,
) -> None:
    """A kind nothing can branch on is the failure the enum exists to prevent."""
    run_id = await open_and_commit(session, repository)

    with pytest.raises(IntegrityError):
        await insert_chain_with(session, run_id, error_kind=error_kind)


@pytest.mark.parametrize("chain_key", ["ethereum", "Bitcoin", "btc"])
async def test_the_chain_key_check_refuses_a_chain_this_product_has_no_provider_for(
    session: AsyncSession,
    repository: SyncRunRepository,
    chain_key: str,
) -> None:
    """The same `CHECK` text `wallets` carries, so the two tables cannot drift apart.

    A run recording an outcome for a chain no wallet can be registered on would be an
    outcome about nothing, and adding a chain stays a migration rather than a string.
    """
    run_id = await open_and_commit(session, repository)

    with pytest.raises(IntegrityError):
        await insert_chain_with(session, run_id, chain_key=chain_key)


@pytest.mark.parametrize("status", ["running", "partial", "interrupted"])
async def test_a_chain_is_only_ever_success_or_failed(
    session: AsyncSession,
    repository: SyncRunRepository,
    status: str,
) -> None:
    """`partial` is a property of a run, not of a chain, and the column says so.

    A chain that raised produced nothing at all; #54 owns the per-address case that would
    make a chain itself partial, and until then admitting the word here would invite a
    status nothing knows how to render.
    """
    run_id = await open_and_commit(session, repository)

    with pytest.raises(IntegrityError):
        await insert_chain_with(session, run_id, status=status)
