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
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.engine import create_session_factory
from portfolio.db.models import (
    _SYNC_RUN_CHAIN_ERROR_KIND_CHECK,
    _SYNC_RUN_CHAIN_STATUS_CHECK,
    _SYNC_RUN_STATUS_CHECK,
    _SYNC_RUN_TRIGGER_CHECK,
    _WALLET_CHAIN_KEY_CHECK,
    SyncRun,
)
from portfolio.repositories.sync_runs import (
    ChainOutcome,
    SyncErrorKind,
    SyncRunRepository,
    SyncRunStatus,
    SyncTrigger,
)
from tests.balance_harness import sqlite_timestamp

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import Engine
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


async def test_the_latest_finish_time_is_none_on_a_fresh_database(
    repository: SyncRunRepository,
) -> None:
    """`None` is what makes a fresh deployment sync immediately instead of waiting."""
    assert await repository.latest_finished_at() is None


async def test_a_run_in_flight_and_an_interrupted_one_are_not_finish_times(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """Only a run that ended counts, which is what "finished" has to mean here.

    A `running` row treated as a finish time would suppress the startup sync forever after
    one crash: the newest row would always look recent and the schedule would never run
    again until somebody deleted it by hand.
    """
    done = await open_and_commit(session, repository)
    await repository.finish_run(
        done,
        status=SyncRunStatus.SUCCESS,
        finished_at=FINISHED_AT,
        duration_ms=1,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=(),
    )
    await session.commit()
    await open_and_commit(session, repository)  # still running, newer
    await repository.sweep_interrupted()
    await open_and_commit(session, repository)  # and one genuinely in flight
    await session.commit()

    assert await repository.latest_finished_at() == FINISHED_AT


async def test_the_latest_finish_time_is_the_newest_run_that_ended(
    session: AsyncSession,
    repository: SyncRunRepository,
) -> None:
    """Newest by identity order, which is finish order because one run happens at a time.

    The later run is given the **earlier** wall-clock finish time, so a query resolved by
    `MAX(finished_at)` returns a different answer from one resolved by `id`. That is not a
    contrived case: it is exactly what a clock stepped backwards between two runs produces,
    which is the same hazard `duration_ms` exists to sidestep.
    """
    older = await open_and_commit(session, repository)
    await repository.finish_run(
        older,
        status=SyncRunStatus.SUCCESS,
        finished_at=FINISHED_AT,
        duration_ms=1,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=(),
    )
    await session.commit()
    newer = await open_and_commit(session, repository)
    await repository.finish_run(
        newer,
        status=SyncRunStatus.SUCCESS,
        finished_at=STARTED_AT,
        duration_ms=1,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=(),
    )
    await session.commit()

    assert await repository.latest_finished_at() == STARTED_AT


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

    stored = (await session.execute(text("SELECT error_kind FROM sync_run_chains"))).scalar_one()
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


# --------------------------------------------------------------------------------------
# Reflection, in the idiom `tests/db/test_migrations.py` already uses
# --------------------------------------------------------------------------------------


def normalise(expression: str) -> str:
    """Collapse runs of whitespace and nothing else; see `test_migrations.py`."""
    return " ".join(expression.split())


def test_the_new_check_constraints_match_the_models(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """Every `CHECK` #10 adds, compared off the migrated file against the model's constant.

    Alembic's autogenerate has no check-constraint comparator, so the drift test in
    `test_migrations.py` is blind to all five of these. Editing one of the constants without
    editing `v0005_balances.py` passes ruff, mypy, the layering contract and the drift check,
    and then fails on the Pi with `CHECK constraint failed`.

    `sync_run_chains.chain_key` reuses `_WALLET_CHAIN_KEY_CHECK` -- one constant for one
    fact -- and that reuse is asserted rather than assumed, because a second copy of the
    chain list is exactly how the two tables would come to admit different chains.
    """
    del migrated_database_url  # Ordering only: the schema has to exist before reflection.
    inspector = inspect(sync_engine)
    expected = {
        "sync_runs": {
            "ck_sync_runs_trigger": _SYNC_RUN_TRIGGER_CHECK,
            "ck_sync_runs_status": _SYNC_RUN_STATUS_CHECK,
        },
        "sync_run_chains": {
            "ck_sync_run_chains_chain_key": _WALLET_CHAIN_KEY_CHECK,
            "ck_sync_run_chains_status": _SYNC_RUN_CHAIN_STATUS_CHECK,
            "ck_sync_run_chains_error_kind": _SYNC_RUN_CHAIN_ERROR_KIND_CHECK,
        },
    }

    for table, constraints in expected.items():
        reflected = {
            str(found["name"]): normalise(str(found["sqltext"]))
            for found in inspector.get_check_constraints(table)
        }
        assert set(reflected) == set(constraints), table
        for name, text_of in constraints.items():
            assert reflected[name] == normalise(text_of), f"{table}.{name}"


def test_the_started_at_index_is_in_the_migrated_schema(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """The index an operator's run history is read through, named as the spec writes it."""
    del migrated_database_url
    indexes = {
        index["name"]: list(index["column_names"])
        for index in inspect(sync_engine).get_indexes("sync_runs")
    }

    assert indexes == {"ix_sync_runs_started_at": ["started_at"]}


def test_the_constraint_comparison_discriminates() -> None:
    """Whitespace is normalised; content is not. Without this the comparison is hollow."""
    assert normalise("status  IN\n ('success', 'failed')") == normalise(
        _SYNC_RUN_CHAIN_STATUS_CHECK
    )
    assert normalise("status IN ('success')") != normalise(_SYNC_RUN_CHAIN_STATUS_CHECK)
    assert normalise("trigger IN ('scheduled', 'manual')") != normalise(_SYNC_RUN_TRIGGER_CHECK)
