"""Spec 030 (#23): `SyncRunRepository.chain_histories`, per chain its newest outcome and success.

What `GET /api/health/detail` serves per chain is read here, so each property the endpoint
relies on is pinned against a real migrated file:

* **newest by run id, never by a `TEXT` datetime.** The runs below are planted with
  `finished_at` values whose order contradicts their ids, so a read that sorted or compared
  the datetime -- or took `MAX(finished_at)` -- answers the wrong run;
* **finished runs only.** A chain row under a run that is `running` or `interrupted` is not an
  outcome anybody finished, and it is left out;
* **the newest success is separate from the newest outcome**, so a chain failing now still
  says when it last worked; and `None` when it never has;
* **`detail` is carried, because it is the run log's vocabulary** -- the service is what
  refuses to serve it, and `tests/services/test_health_service.py` pins that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import event, text

from portfolio.db.engine import create_session_factory
from portfolio.repositories.sync_runs import (
    ChainHistory,
    ChainOutcome,
    SyncErrorKind,
    SyncRunRepository,
    SyncRunStatus,
    SyncTrigger,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

T0: Final = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
BITCOIN: Final = "bitcoin"
KASPA: Final = "kaspa"


@pytest.fixture
async def session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = create_session_factory(migrated_engine)
    async with factory() as opened:
        yield opened


def ok(chain_key: str) -> ChainOutcome:
    return ChainOutcome(chain_key=chain_key, status=SyncRunStatus.SUCCESS, wallets_read=1)


def failed(
    chain_key: str, kind: SyncErrorKind = SyncErrorKind.UNAVAILABLE, detail: str = "down"
) -> ChainOutcome:
    return ChainOutcome(
        chain_key=chain_key,
        status=SyncRunStatus.FAILED,
        wallets_read=0,
        error_kind=kind,
        detail=detail,
    )


async def run(
    session: AsyncSession,
    *,
    finished_at: datetime,
    chains: Sequence[ChainOutcome],
    status: SyncRunStatus = SyncRunStatus.SUCCESS,
) -> int:
    """One run, opened and finished with `chains`, committed. Returns its id."""
    repository = SyncRunRepository(session)
    opened = await repository.open_run(
        trigger=SyncTrigger.SCHEDULED,
        started_at=finished_at - timedelta(seconds=3),
        wallets_total=1,
    )
    run_id = opened.id
    await session.commit()
    await repository.finish_run(
        run_id,
        status=status,
        finished_at=finished_at,
        duration_ms=3000,
        wallets_succeeded=1,
        wallets_failed=0,
        chains=chains,
    )
    await session.commit()
    return run_id


async def histories(session: AsyncSession) -> dict[str, ChainHistory]:
    return await SyncRunRepository(session).chain_histories()


async def test_no_run_at_all_is_an_empty_mapping(session: AsyncSession) -> None:
    assert await histories(session) == {}


async def test_one_successful_run_is_the_newest_outcome_and_the_newest_success(
    session: AsyncSession,
) -> None:
    await run(session, finished_at=T0, chains=[ok(BITCOIN)])

    assert await histories(session) == {
        BITCOIN: ChainHistory(chain_key=BITCOIN, latest=ok(BITCOIN), last_success_at=T0)
    }


async def test_a_chain_that_never_succeeded_has_no_last_success(session: AsyncSession) -> None:
    await run(session, finished_at=T0, chains=[failed(KASPA)], status=SyncRunStatus.FAILED)

    found = await histories(session)

    assert found[KASPA].latest == failed(KASPA)
    assert found[KASPA].last_success_at is None


async def test_failing_now_still_says_when_it_last_worked(session: AsyncSession) -> None:
    await run(session, finished_at=T0, chains=[ok(BITCOIN)])
    await run(
        session,
        finished_at=T0 + timedelta(hours=1),
        chains=[failed(BITCOIN, SyncErrorKind.RATE_LIMITED, "throttled")],
        status=SyncRunStatus.FAILED,
    )

    found = await histories(session)

    assert found[BITCOIN].latest == failed(BITCOIN, SyncErrorKind.RATE_LIMITED, "throttled")
    assert found[BITCOIN].last_success_at == T0


async def test_newest_is_by_run_id_not_by_the_text_datetime(session: AsyncSession) -> None:
    """The second run has the larger id and the *earlier* `finished_at`. Id order wins.

    A clock stepped back between two runs produces exactly this, and the rule the repository
    states is that identity order is finish order with one run at a time. A read that
    ordered by -- or took the maximum of -- the `TEXT` column answers the first run instead.
    """
    await run(session, finished_at=T0 + timedelta(days=2), chains=[ok(BITCOIN)])
    second = T0 + timedelta(days=1)
    await run(session, finished_at=second, chains=[failed(BITCOIN)], status=SyncRunStatus.FAILED)
    third = T0
    await run(session, finished_at=third, chains=[ok(KASPA)])

    found = await histories(session)

    assert found[BITCOIN].latest.status is SyncRunStatus.FAILED
    assert found[BITCOIN].last_success_at == T0 + timedelta(days=2)
    assert found[KASPA].last_success_at == third


async def test_the_newest_success_is_the_newest_by_id_among_several(session: AsyncSession) -> None:
    """Three successes, finished out of order: the last *by id* is the one served."""
    await run(session, finished_at=T0 + timedelta(days=5), chains=[ok(BITCOIN)])
    await run(session, finished_at=T0 + timedelta(days=9), chains=[ok(BITCOIN)])
    newest_by_id = T0 + timedelta(days=1)
    await run(session, finished_at=newest_by_id, chains=[ok(BITCOIN)])

    assert (await histories(session))[BITCOIN].last_success_at == newest_by_id


@pytest.mark.parametrize("status", [SyncRunStatus.RUNNING, SyncRunStatus.INTERRUPTED])
async def test_a_chain_row_under_a_run_that_did_not_finish_is_not_an_outcome(
    session: AsyncSession, status: SyncRunStatus
) -> None:
    """Planted by hand: `finish_run` never writes chains for such a run, and this says the
    read would not believe them if something did."""
    await run(session, finished_at=T0, chains=[ok(BITCOIN)])
    later = await run(
        session,
        finished_at=T0 + timedelta(hours=1),
        chains=[failed(BITCOIN), ok(KASPA)],
        status=SyncRunStatus.FAILED,
    )
    await session.execute(
        text("UPDATE sync_runs SET status = :status WHERE id = :id"),
        {"status": str(status), "id": later},
    )
    await session.commit()

    found = await histories(session)

    assert set(found) == {BITCOIN}, "kaspa's only row is under a run that did not finish"
    assert found[BITCOIN].latest == ok(BITCOIN)
    assert found[BITCOIN].last_success_at == T0


async def test_a_partial_run_is_finished_and_its_chains_count(session: AsyncSession) -> None:
    await run(
        session,
        finished_at=T0,
        chains=[ok(BITCOIN), failed(KASPA, SyncErrorKind.RESPONSE)],
        status=SyncRunStatus.PARTIAL,
    )

    found = await histories(session)

    assert found[BITCOIN].last_success_at == T0
    assert found[KASPA].latest.error_kind is SyncErrorKind.RESPONSE
    assert found[KASPA].last_success_at is None


async def test_each_chain_is_judged_by_its_own_newest_run(session: AsyncSession) -> None:
    """Kaspa's newest outcome is in an older run than bitcoin's: a run without it is not news."""
    await run(session, finished_at=T0, chains=[ok(BITCOIN), failed(KASPA)])
    await run(session, finished_at=T0 + timedelta(hours=1), chains=[failed(BITCOIN)])

    found = await histories(session)

    assert found[KASPA].latest == failed(KASPA)
    assert found[BITCOIN].latest == failed(BITCOIN)
    assert found[BITCOIN].last_success_at == T0


async def test_the_mapping_is_sorted_by_chain_key(session: AsyncSession) -> None:
    await run(session, finished_at=T0, chains=[ok(KASPA), ok(BITCOIN)])

    assert list(await histories(session)) == [BITCOIN, KASPA]


async def test_the_read_is_three_statements_whatever_the_number_of_runs(
    session: AsyncSession, migrated_engine: AsyncEngine
) -> None:
    """Not one query per run or per chain: the endpoint runs this once a minute per page."""
    for hour in range(6):
        await run(session, finished_at=T0 + timedelta(hours=hour), chains=[ok(BITCOIN), ok(KASPA)])
    statements: list[str] = []

    def count(*arguments: object) -> None:
        statements.append(str(arguments[2]))

    event.listen(migrated_engine.sync_engine, "before_cursor_execute", count)
    try:
        await histories(session)
    finally:
        event.remove(migrated_engine.sync_engine, "before_cursor_execute", count)

    selects = [
        statement for statement in statements if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 3, selects
    assert all("ORDER BY sync_runs.finished_at" not in statement for statement in selects)
    assert all("max(sync_runs.finished_at)" not in statement.lower() for statement in selects)
