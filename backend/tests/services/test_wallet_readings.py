"""The wallet reading rule: `services.wallet_readings.wallet_reading_problem` (spec 028).

Pure: every case is a reading and a latest finished run built in memory, judged against one
named instant. What the dashboard does with the answer is `test_portfolio_summary.py`'s.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.db.models import BalanceSnapshot
from portfolio.domain.portfolio import MAX_READING_AGE
from portfolio.repositories.sync_runs import (
    ChainOutcome,
    SyncErrorKind,
    SyncRunStatus,
    SyncRunSummary,
    SyncTrigger,
)
from portfolio.services.wallet_readings import WalletReadingProblem, wallet_reading_problem

if TYPE_CHECKING:
    from collections.abc import Mapping

NOW: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
A_DAY: Final = timedelta(hours=24)
ONE_MICROSECOND: Final = timedelta(microseconds=1)
TEN_MINUTES: Final = timedelta(minutes=10)

BITCOIN: Final = "bitcoin"
KASPA: Final = "kaspa"

FAILED: Final = SyncRunStatus.FAILED
SUCCESS: Final = SyncRunStatus.SUCCESS


def reading(run_id: int, *, age: timedelta = TEN_MINUTES) -> BalanceSnapshot:
    """A wallet's latest snapshot, written by run `run_id`, observed `age` before `NOW`.

    Built in memory and attached to no session: the rule reads `sync_run_id` and
    `observed_at` and nothing else.
    """
    return BalanceSnapshot(
        wallet_id=1,
        sync_run_id=run_id,
        confirmed=40_000_000,
        pending=None,
        decimals=8,
        observed_at=NOW - age,
    )


def finished(
    run_id: int,
    chains: Mapping[str, SyncRunStatus] | None = None,
    *,
    status: SyncRunStatus = SyncRunStatus.PARTIAL,
) -> SyncRunSummary:
    """A finished balance run as `SyncRunRepository.latest_finished` answers it."""
    outcomes = tuple(
        ChainOutcome(
            chain_key=chain_key,
            status=outcome,
            wallets_read=0 if outcome is FAILED else 1,
            error_kind=SyncErrorKind.UNAVAILABLE if outcome is FAILED else None,
            detail="the index did not answer" if outcome is FAILED else None,
        )
        for chain_key, outcome in sorted((chains or {}).items())
    )
    return SyncRunSummary(
        run_id=run_id,
        trigger=SyncTrigger.SCHEDULED,
        status=status,
        started_at=NOW - timedelta(minutes=5),
        finished_at=NOW - timedelta(minutes=4),
        duration_ms=60_000,
        wallets_total=len(outcomes),
        wallets_succeeded=sum(1 for outcome in outcomes if outcome.status is SUCCESS),
        wallets_failed=sum(1 for outcome in outcomes if outcome.status is FAILED),
        chains=outcomes,
    )


def reason_value(found: WalletReadingProblem | None) -> str | None:
    return None if found is None else found.value


def test_the_three_problems_in_the_order_they_are_tested() -> None:
    """`chain_failed` first: the failure is the fact the owner can act on (spec 028)."""
    assert [(reason.name, reason.value) for reason in WalletReadingProblem] == [
        ("CHAIN_FAILED", "chain_failed"),
        ("UNREAD", "unread"),
        ("STALE", "stale"),
    ]


BITCOIN_FAILED: Final = finished(7, {BITCOIN: FAILED, KASPA: SUCCESS})
BITCOIN_READ: Final = finished(7, {BITCOIN: SUCCESS, KASPA: FAILED})
LONG_AGO: Final = timedelta(days=30)

WALLET_REASONS: Final = [
    # Compared: nothing applies.
    pytest.param(reading(7), BITCOIN_READ, None, id="chain read, reading recent: compared"),
    pytest.param(reading(3), None, None, id="no finished run, reading recent: compared"),
    # Each reason by itself.
    pytest.param(
        reading(6), BITCOIN_FAILED, "chain_failed", id="chain failed, reading from the run before"
    ),
    pytest.param(None, BITCOIN_READ, "unread", id="chain read, no reading: unread"),
    pytest.param(
        reading(7, age=LONG_AGO), BITCOIN_READ, "stale", id="chain read, reading a month old"
    ),
    # Precedence: the first that applies, in the order the reasons are declared.
    pytest.param(
        None, BITCOIN_FAILED, "chain_failed", id="chain failed and no reading: chain_failed"
    ),
    pytest.param(
        reading(2, age=LONG_AGO),
        BITCOIN_FAILED,
        "chain_failed",
        id="chain failed and reading a month old: chain_failed",
    ),
    # A reading a later run wrote is not chain_failed, and the other rules still decide.
    pytest.param(
        reading(8), BITCOIN_FAILED, None, id="chain failed, reading from a later run: compared"
    ),
    pytest.param(
        reading(8, age=LONG_AGO),
        BITCOIN_FAILED,
        "stale",
        id="chain failed, reading from a later run and a month old: stale",
    ),
    # "Did not fail": no finished run, and a run with no row for the chain.
    pytest.param(None, None, "unread", id="no finished run, no reading: unread"),
    pytest.param(reading(1, age=LONG_AGO), None, "stale", id="no finished run, old reading"),
    pytest.param(reading(3), finished(7, {KASPA: FAILED}), None, id="no row for the chain"),
    pytest.param(
        None, finished(7, {KASPA: FAILED}), "unread", id="no row for the chain, no reading"
    ),
    pytest.param(
        reading(3, age=LONG_AGO),
        finished(7, {KASPA: FAILED}),
        "stale",
        id="no row for the chain, old reading",
    ),
    pytest.param(reading(3), finished(7), None, id="a run with no chain rows at all"),
    pytest.param(None, finished(7), "unread", id="a run with no chain rows, no reading"),
]


@pytest.mark.parametrize(("latest", "run", "reason"), WALLET_REASONS)
def test_the_problem_with_a_reading_is_the_first_that_applies(
    latest: BalanceSnapshot | None, run: SyncRunSummary | None, reason: str | None
) -> None:
    """The rule for a Bitcoin wallet: `chain_failed`, then `unread`, then `stale`."""
    assert reason_value(wallet_reading_problem(BITCOIN, latest, run, NOW)) == reason


@pytest.mark.parametrize(
    ("written_by", "reason"),
    [
        pytest.param(1, "chain_failed", id="the first run there was"),
        pytest.param(6, "chain_failed", id="the run before the latest finished one"),
        pytest.param(7, "chain_failed", id="the latest finished run itself"),
        pytest.param(8, None, id="the run after it"),
        pytest.param(9_000, None, id="a run long after it"),
    ],
)
def test_a_reading_is_marked_up_to_the_latest_finished_runs_id_and_current_above_it(
    written_by: int, reason: str | None
) -> None:
    """Spec 028, condition 2, at its boundary: `sync_run_id <= run.id` is `chain_failed`.

    The latest finished run is 7 and it failed the chain. A reading written by run 7 or an
    earlier one is not newer than that verdict; one written by run 8 is, and is the newest
    reading there is.
    """
    found = wallet_reading_problem(BITCOIN, reading(written_by), BITCOIN_FAILED, NOW)

    assert reason_value(found) == reason


@pytest.mark.parametrize("status", [SyncRunStatus.PARTIAL, SyncRunStatus.FAILED])
def test_a_chain_failed_in_a_partial_run_and_in_a_failed_one_alike(status: SyncRunStatus) -> None:
    """A run is `partial` when another chain was read and `failed` when none was. The wallet's
    chain failed in both, and that is what the rule goes by."""
    run = finished(7, {BITCOIN: FAILED}, status=status)

    assert wallet_reading_problem(BITCOIN, reading(6), run, NOW) is (
        WalletReadingProblem.CHAIN_FAILED
    )
    assert wallet_reading_problem(BITCOIN, None, run, NOW) is (WalletReadingProblem.CHAIN_FAILED)


def test_only_the_wallets_own_chain_decides() -> None:
    """Kaspa failed and Bitcoin was read. Each wallet is judged by its own chain's row."""
    run = finished(7, {BITCOIN: SUCCESS, KASPA: FAILED})

    assert wallet_reading_problem(BITCOIN, reading(6), run, NOW) is None
    assert wallet_reading_problem(KASPA, reading(6), run, NOW) is (
        WalletReadingProblem.CHAIN_FAILED
    )


def test_a_chain_that_was_read_never_marks_a_reading_whatever_run_wrote_it() -> None:
    """The id comparison is half of one rule, not a rule by itself: a wallet whose chain
    succeeded is current on a reading from any run, older ones included."""
    for written_by in (1, 6, 7, 8):
        assert wallet_reading_problem(BITCOIN, reading(written_by), BITCOIN_READ, NOW) is None


@pytest.mark.parametrize(
    ("age", "reason"),
    [
        pytest.param(timedelta(0), None, id="read this instant"),
        pytest.param(A_DAY - ONE_MICROSECOND, None, id="one microsecond inside"),
        pytest.param(A_DAY, None, id="exactly twenty-four hours: at most, so current"),
        pytest.param(A_DAY + ONE_MICROSECOND, "stale", id="one microsecond past"),
        pytest.param(A_DAY * 30, "stale", id="a month"),
        pytest.param(-timedelta(minutes=5), None, id="dated after the clock: current"),
        pytest.param(-A_DAY * 400, None, id="dated far after the clock: current"),
    ],
)
@pytest.mark.parametrize(
    "run",
    [
        pytest.param(None, id="no finished run"),
        pytest.param(BITCOIN_READ, id="the chain was read"),
        pytest.param(finished(7, {KASPA: FAILED}), id="no row for the chain"),
    ],
)
def test_a_wallets_reading_is_current_for_exactly_twenty_four_hours(
    run: SyncRunSummary | None, age: timedelta, reason: str | None
) -> None:
    """ "At most" a day, to the microsecond."""
    assert timedelta(hours=24) == MAX_READING_AGE
    found = wallet_reading_problem(BITCOIN, reading(7, age=age), run, NOW)

    assert reason_value(found) == reason


@pytest.mark.parametrize(
    "age", [timedelta(0), A_DAY, A_DAY + ONE_MICROSECOND, A_DAY * 30, -timedelta(minutes=5)]
)
def test_a_failed_chain_marks_a_reading_whatever_its_age(age: timedelta) -> None:
    """Minutes old or a month old: the failure is recorded, so the age is not consulted."""
    found = wallet_reading_problem(BITCOIN, reading(7, age=age), BITCOIN_FAILED, NOW)

    assert found is WalletReadingProblem.CHAIN_FAILED
