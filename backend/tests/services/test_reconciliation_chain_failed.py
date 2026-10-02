"""Spec 028 (#116): a wallet whose chain failed in the latest finished balance run is left out.

Before this rule a wallet had the age limit alone, so when a chain's provider failed for hours
the wallet's last reading stayed in the comparison until it was a day old. Coins sent from it
to a venue in the meantime were read at the venue and still counted in the wallet: "held
exceeds history" for units that do not exist, with nothing naming the wallet.

Two layers, each with its own section below, and a third in a module of its own.

## The rule, as a pure function

`wallet_not_compared_reason(chain_key, reading, latest_finished_run, now)` is called with
objects built in memory: no session, no file. Every reason, their precedence, the boundary of
`sync_run_id` against the run's id on both sides, and the age limit to the microsecond.

## The service, over a real SQLite file

The runs, their chain rows and the balance snapshots are planted as rows, the way the balance
sync leaves them, so that what is asserted includes `SyncRunRepository.latest_finished`
choosing the run. That is where a `running` or an `interrupted` newest run is shown not to
hide the finished one before it, and where a reading a later run has already written is shown
to be kept.

The runs are numbered by the database. A test that needs "run 2 failed the chain, run 3 is in
flight" plants them in that order and names the ids it was given.

## The four counts, against an oracle

In `test_reconciliation_chain_failed_property.py`: a property test over in-memory
repositories, held to a second statement of the rule. It is a module of its own so that a
failure there is reported in seconds; that module says why.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event, text

from portfolio.db.models import BalanceSnapshot
from portfolio.domain.accounting import ReconciliationStatus
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import ExchangeKey
from portfolio.repositories.sync_runs import (
    ChainOutcome,
    SyncErrorKind,
    SyncRunStatus,
    SyncRunSummary,
    SyncTrigger,
)
from portfolio.services.reconciliation import (
    MAX_READING_AGE,
    FailedChain,
    ReconciliationView,
    WalletNotComparedReason,
    WalletSources,
    build_reconciliation_service,
    wallet_not_compared_reason,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    BIP350_TESTNET_V1,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V0_ASPECTRON,
    KASPA_TESTNET_V1_KEY,
)
from tests.balance_harness import sqlite_timestamp
from tests.exchange_sync_harness import SettableClock, held
from tests.services.test_reconciliation_service import (
    BITGET_READ,
    NOW,
    OBSERVED_NEWER,
    OBSERVED_NEWEST,
    OBSERVED_OLDEST,
    SATS,
    Planted,
    buy,
    by_asset,
    decimals,
    figures,
    plant_owner_with_history,
    plant_wallet,
    store_balances,
    view_of,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


A_DAY: Final = timedelta(hours=24)
ONE_MICROSECOND: Final = timedelta(microseconds=1)
TEN_MINUTES: Final = timedelta(minutes=10)

BITCOIN: Final = "bitcoin"
KASPA: Final = "kaspa"

FAILED: Final = SyncRunStatus.FAILED
SUCCESS: Final = SyncRunStatus.SUCCESS

#: The three statuses of a run that ran to its end, written out rather than read off the
#: repository: spec 028 names them.
FINISHED_STATUSES: Final = ("success", "partial", "failed")
#: The two a run has while it has no chain rows.
UNFINISHED_STATUSES: Final = ("running", "interrupted")


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


# --------------------------------------------------------------------------------------
# The rule, as a pure function
# --------------------------------------------------------------------------------------


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


def reason_value(found: WalletNotComparedReason | None) -> str | None:
    return None if found is None else found.value


def test_the_three_wallet_reasons_in_the_order_they_are_tested() -> None:
    """`chain_failed` first: the failure is the fact the owner can act on (spec 028)."""
    assert [(reason.name, reason.value) for reason in WalletNotComparedReason] == [
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
def test_the_reason_a_wallet_is_not_compared_is_the_first_that_applies(
    latest: BalanceSnapshot | None, run: SyncRunSummary | None, reason: str | None
) -> None:
    """The pure rule for a Bitcoin wallet: `chain_failed`, then `unread`, then `stale`."""
    assert reason_value(wallet_not_compared_reason(BITCOIN, latest, run, NOW)) == reason


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
def test_a_reading_is_left_out_up_to_the_latest_finished_runs_id_and_kept_above_it(
    written_by: int, reason: str | None
) -> None:
    """Condition 2 of the spec, at its boundary: `sync_run_id <= run.id` is left out.

    The latest finished run is 7 and it failed the chain. A reading written by run 7 or an
    earlier one is not newer than that verdict; one written by run 8 is, and is the newest
    reading there is.
    """
    found = wallet_not_compared_reason(BITCOIN, reading(written_by), BITCOIN_FAILED, NOW)

    assert reason_value(found) == reason


@pytest.mark.parametrize("status", [SyncRunStatus.PARTIAL, SyncRunStatus.FAILED])
def test_a_chain_failed_in_a_partial_run_and_in_a_failed_one_alike(status: SyncRunStatus) -> None:
    """A run is `partial` when another chain was read and `failed` when none was. The wallet's
    chain failed in both, and that is what the rule goes by."""
    run = finished(7, {BITCOIN: FAILED}, status=status)

    assert wallet_not_compared_reason(BITCOIN, reading(6), run, NOW) is (
        WalletNotComparedReason.CHAIN_FAILED
    )
    assert wallet_not_compared_reason(BITCOIN, None, run, NOW) is (
        WalletNotComparedReason.CHAIN_FAILED
    )


def test_only_the_wallets_own_chain_decides() -> None:
    """Kaspa failed and Bitcoin was read. Each wallet is judged by its own chain's row."""
    run = finished(7, {BITCOIN: SUCCESS, KASPA: FAILED})

    assert wallet_not_compared_reason(BITCOIN, reading(6), run, NOW) is None
    assert wallet_not_compared_reason(KASPA, reading(6), run, NOW) is (
        WalletNotComparedReason.CHAIN_FAILED
    )


def test_a_chain_that_was_read_never_leaves_a_wallet_out_whatever_its_readings_run() -> None:
    """The id comparison is half of one rule, not a rule by itself: a wallet whose chain
    succeeded is compared on a reading from any run, older ones included."""
    for written_by in (1, 6, 7, 8):
        assert wallet_not_compared_reason(BITCOIN, reading(written_by), BITCOIN_READ, NOW) is None


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
    """The age limit is unchanged (non-goal of spec 028): "at most" a day, to the microsecond."""
    assert timedelta(hours=24) == MAX_READING_AGE
    found = wallet_not_compared_reason(BITCOIN, reading(7, age=age), run, NOW)

    assert reason_value(found) == reason


@pytest.mark.parametrize(
    "age", [timedelta(0), A_DAY, A_DAY + ONE_MICROSECOND, A_DAY * 30, -timedelta(minutes=5)]
)
def test_a_failed_chain_leaves_a_reading_out_whatever_its_age(age: timedelta) -> None:
    """Minutes old or a month old: the failure is recorded, so the age is not consulted."""
    found = wallet_not_compared_reason(BITCOIN, reading(7, age=age), BITCOIN_FAILED, NOW)

    assert found is WalletNotComparedReason.CHAIN_FAILED


# --------------------------------------------------------------------------------------
# Planting balance runs, their chain rows and the snapshots they wrote
# --------------------------------------------------------------------------------------


async def plant_balance_run(
    factory: async_sessionmaker[AsyncSession],
    status: str,
    chains: Mapping[str, str] | None = None,
    *,
    started_at: datetime = NOW - timedelta(minutes=30),
    error_kind: str = "unavailable",
) -> int:
    """One `sync_runs` row with its `sync_run_chains` rows, as the balance sync leaves them.

    A finished run has an end time and a duration. A `running` or an `interrupted` one has
    neither and no chain rows: `finish_run` writes those with the final status, and the test
    below that plants chains on an unfinished run does so to show they are not read.
    """
    ended = status in FINISHED_STATUSES
    async with factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
                "wallets_total, wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', :status, :started, :finished, :duration, 0, 0, 0) "
                "RETURNING id"
            ),
            {
                "status": status,
                "started": sqlite_timestamp(started_at),
                "finished": sqlite_timestamp(started_at) if ended else None,
                "duration": 1 if ended else None,
            },
        )
        run_id: int = result.scalar_one()
        for chain_key, outcome in (chains or {}).items():
            await session.execute(
                text(
                    "INSERT INTO sync_run_chains "
                    "(sync_run_id, chain_key, status, wallets_read, error_kind, detail) "
                    "VALUES (:run, :chain, :status, :read, :kind, NULL)"
                ),
                {
                    "run": run_id,
                    "chain": chain_key,
                    "status": outcome,
                    "read": 0 if outcome == "failed" else 1,
                    "kind": error_kind if outcome == "failed" else None,
                },
            )
        await session.commit()
    return run_id


async def plant_snapshot(
    factory: async_sessionmaker[AsyncSession],
    *,
    wallet_id: int,
    run_id: int,
    confirmed: int,
    observed_at: datetime = OBSERVED_NEWER,
) -> None:
    """One `balance_snapshots` row written by run `run_id`."""
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO balance_snapshots "
                "(wallet_id, sync_run_id, confirmed, pending, decimals, observed_at) "
                "VALUES (:wallet, :run, :confirmed, NULL, 8, :at)"
            ),
            {
                "wallet": wallet_id,
                "run": run_id,
                "confirmed": confirmed,
                "at": sqlite_timestamp(observed_at),
            },
        )
        await session.commit()


def sources(
    *,
    compared: int = 0,
    stale: int = 0,
    unread: int = 0,
    failed_chains: Mapping[str, int] | None = None,
    oldest: datetime | None = None,
) -> WalletSources:
    """The wallet sources a test expects. `chain_failed` is the chains' wallets added up."""
    chains = tuple(
        FailedChain(chain_key, wallets) for chain_key, wallets in (failed_chains or {}).items()
    )
    return WalletSources(
        compared=compared,
        stale=stale,
        unread=unread,
        chain_failed=sum(chain.wallets for chain in chains),
        failed_chains=chains,
        oldest_observed_at=oldest,
    )


async def a_bitcoin_wallet_read_once(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[Planted, int, int]:
    """The owner (0.5 BTC in the history), one Bitcoin wallet, and the run that read 0.4 into it.

    Returns the planted ids, the wallet's id and the run's id.
    """
    planted = await plant_owner_with_history(factory)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    first = await plant_balance_run(factory, "success", {BITCOIN: "success"})
    await plant_snapshot(factory, wallet_id=wallet, run_id=first, confirmed=40_000_000)
    return planted, wallet, first


# --------------------------------------------------------------------------------------
# Criterion 1: left out, and counted
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("run_status", ["partial", "failed"])
async def test_a_wallet_whose_chain_failed_in_the_latest_finished_run_adds_nothing(
    factory: async_sessionmaker[AsyncSession], run_status: str
) -> None:
    """Read ten minutes ago with 0.4 BTC, and then the chain failed: 0 BTC from wallets.

    The reading is far inside the age limit. It is left out because the run after it could
    not read the chain, so nothing says the 0.4 BTC is still there.
    """
    planted, _wallet, _first = await a_bitcoin_wallet_read_once(factory)
    await plant_balance_run(factory, run_status, {BITCOIN: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(failed_chains={BITCOIN: 1})
    assert view.wallets.chain_failed == 1
    assert figures(by_asset(view)["BTC"]) == decimals("0.5", "0", "0", "0", "-0.5")
    assert by_asset(view)["BTC"].status is ReconciliationStatus.HISTORY_OVER, (
        "a source that contributes nothing can hide a finding, and never produces a false one"
    )


async def test_a_wallet_with_no_reading_whose_chain_failed_is_chain_failed_not_unread(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The failure is the fact the owner can act on, so it is the reason that is given."""
    planted = await plant_owner_with_history(factory)
    await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(failed_chains={BITCOIN: 1})
    assert view.wallets.unread == 0


async def test_a_failed_chain_leaves_out_a_reading_the_age_limit_would_also_leave_out(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three days old and the chain failed: counted once, as `chain_failed`, not as `stale`."""
    planted = await plant_owner_with_history(factory)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    first = await plant_balance_run(factory, "success", {BITCOIN: "success"})
    await plant_snapshot(
        factory,
        wallet_id=wallet,
        run_id=first,
        confirmed=40_000_000,
        observed_at=NOW - timedelta(days=3),
    )
    await plant_balance_run(factory, "failed", {BITCOIN: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(failed_chains={BITCOIN: 1})


async def test_a_wallet_stays_chain_failed_for_as_long_as_that_run_is_the_latest(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The last run that finished failed the chain a month ago, and the timer is off since.

    The wallet does not turn `stale` when its reading passes the age limit. The failure is
    still the latest verdict there is, and it is the one the owner can act on.
    """
    a_month_ago = NOW - timedelta(days=30)
    planted = await plant_owner_with_history(factory)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    first = await plant_balance_run(
        factory, "success", {BITCOIN: "success"}, started_at=a_month_ago - timedelta(minutes=15)
    )
    await plant_snapshot(
        factory,
        wallet_id=wallet,
        run_id=first,
        confirmed=40_000_000,
        observed_at=a_month_ago - timedelta(minutes=15),
    )
    await plant_balance_run(factory, "failed", {BITCOIN: "failed"}, started_at=a_month_ago)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(failed_chains={BITCOIN: 1})
    assert view.wallets.stale == 0


@pytest.mark.parametrize(
    "error_kind",
    ["unavailable", "rate_limited", "response", "unknown_chain", "address_rejected", "internal"],
)
async def test_a_chain_failed_whatever_the_kind_of_its_failure(
    factory: async_sessionmaker[AsyncSession], error_kind: str
) -> None:
    """The vendor's fault, the owner's (`address_rejected`) or ours (`internal`): the chain was
    not read, so nothing says what its wallets hold. The kind is the run log's to report."""
    planted, _wallet, _first = await a_bitcoin_wallet_read_once(factory)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed"}, error_kind=error_kind)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(failed_chains={BITCOIN: 1})
    assert by_asset(view)["BTC"].wallet_quantity == 0


# --------------------------------------------------------------------------------------
# Criterion 2: a chain that did not fail changes nothing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("run_status", FINISHED_STATUSES)
async def test_a_wallet_whose_chain_was_read_in_the_latest_finished_run_is_compared(
    factory: async_sessionmaker[AsyncSession], run_status: str
) -> None:
    planted, _wallet, _first = await a_bitcoin_wallet_read_once(factory)
    await plant_balance_run(factory, run_status, {BITCOIN: "success", KASPA: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(compared=1, oldest=OBSERVED_NEWER)
    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.4")


async def test_a_chain_with_no_row_in_the_latest_finished_run_did_not_fail(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """No wallet was active on Bitcoin when the run ran, so it has no row for it. The run
    failed Kaspa, and that says nothing of a Bitcoin wallet."""
    planted, _wallet, _first = await a_bitcoin_wallet_read_once(factory)
    await plant_balance_run(factory, "failed", {KASPA: "failed"})
    await plant_wallet(factory, planted, "btc-new", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(compared=1, unread=1, oldest=OBSERVED_NEWER)
    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.4")


async def test_a_latest_finished_run_with_no_chain_rows_leaves_nothing_out(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A run over no active wallet records no chain. The run before it failed Bitcoin, and
    is no longer the latest."""
    planted, _wallet, _first = await a_bitcoin_wallet_read_once(factory)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed"})
    await plant_balance_run(factory, "success")

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(compared=1, oldest=OBSERVED_NEWER)


@pytest.mark.parametrize("only_status", UNFINISHED_STATUSES)
async def test_with_no_finished_run_every_wallet_is_judged_by_the_other_rules(
    factory: async_sessionmaker[AsyncSession], only_status: str
) -> None:
    """The first run ever is in flight, or was interrupted, and has committed one reading.

    No run has finished, so no chain is known to have failed: one wallet is compared on the
    reading that run wrote, one is unread, one is stale.
    """
    planted = await plant_owner_with_history(factory)
    read = await plant_wallet(factory, planted, "a", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    old = await plant_wallet(factory, planted, "b", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    await plant_wallet(factory, planted, "c", ChainKey.KASPA, KASPA_TESTNET_V0)
    only = await plant_balance_run(factory, only_status)
    await plant_snapshot(factory, wallet_id=read, run_id=only, confirmed=40_000_000)
    await plant_snapshot(
        factory,
        wallet_id=old,
        run_id=only,
        confirmed=900_000_000,
        observed_at=NOW - timedelta(days=3),
    )

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(compared=1, stale=1, unread=1, oldest=OBSERVED_NEWER)
    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.4")


async def test_with_no_balance_run_at_all_an_unread_wallet_is_unread(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_owner_with_history(factory)
    await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(unread=1)


# --------------------------------------------------------------------------------------
# Criteria 3 and 4: a later run's reading is kept, and an unfinished run hides nothing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("newest_status", UNFINISHED_STATUSES)
@pytest.mark.parametrize(
    ("chain_outcome", "newer_reading", "expected"),
    [
        pytest.param("failed", False, "chain_failed", id="chain failed, nothing newer: left out"),
        pytest.param("failed", True, "0.25", id="chain failed, a later run read it: compared"),
        pytest.param("success", False, "0.4", id="chain read, nothing newer: compared"),
        pytest.param("success", True, "0.25", id="chain read, a later run read it: compared"),
    ],
)
async def test_an_unfinished_newest_run_does_not_hide_the_latest_finished_one(
    factory: async_sessionmaker[AsyncSession],
    newest_status: str,
    chain_outcome: str,
    newer_reading: bool,
    expected: str,
) -> None:
    """Three runs. The first read 0.4 BTC, the second finished with the chain `chain_outcome`,
    and the third is `running` or `interrupted`, with or without a reading committed.

    The third run has no chain rows and cannot say which chains failed, so the second is the
    latest finished one whatever the third's status (criterion 4). When the third run has
    already written the wallet's reading, that reading is newer than the second run's
    verdict and it is compared (criterion 3). Snapshots are committed per chain before a run
    closes, which is how an unfinished run comes to have one.
    """
    planted, wallet, first = await a_bitcoin_wallet_read_once(factory)
    second = await plant_balance_run(factory, "partial", {BITCOIN: chain_outcome})
    third = await plant_balance_run(factory, newest_status)
    assert first < second < third
    if newer_reading:
        await plant_snapshot(
            factory,
            wallet_id=wallet,
            run_id=third,
            confirmed=25_000_000,
            observed_at=OBSERVED_NEWEST,
        )

    view = await view_of(factory, planted.user_id)

    if expected == "chain_failed":
        assert view.wallets == sources(failed_chains={BITCOIN: 1})
        assert by_asset(view)["BTC"].wallet_quantity == 0
    else:
        observed = OBSERVED_NEWEST if newer_reading else OBSERVED_NEWER
        assert view.wallets == sources(compared=1, oldest=observed)
        assert by_asset(view)["BTC"].wallet_quantity == Decimal(expected)


@pytest.mark.parametrize("newest_status", UNFINISHED_STATUSES)
async def test_chain_rows_under_an_unfinished_run_are_not_read(
    factory: async_sessionmaker[AsyncSession], newest_status: str
) -> None:
    """No writer leaves a chain row under a `running` or an `interrupted` run. One planted
    there by hand is still not consulted: the run is chosen by its status."""
    planted, _wallet, _first = await a_bitcoin_wallet_read_once(factory)
    await plant_balance_run(factory, newest_status, {BITCOIN: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(compared=1, oldest=OBSERVED_NEWER)


async def test_the_latest_finished_run_is_the_newest_by_id_and_decides_alone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The chain failed, then was read, then failed again: the wallet follows the newest run.

    Each later run is given an **earlier** `started_at`, so a run chosen by its clock rather
    than by its id answers the opposite at every step.
    """
    planted, wallet, _first = await a_bitcoin_wallet_read_once(factory)

    await plant_balance_run(
        factory, "failed", {BITCOIN: "failed"}, started_at=NOW - timedelta(minutes=20)
    )
    after_failure = await view_of(factory, planted.user_id)

    recovered = await plant_balance_run(
        factory, "success", {BITCOIN: "success"}, started_at=NOW - timedelta(hours=2)
    )
    await plant_snapshot(
        factory, wallet_id=wallet, run_id=recovered, confirmed=25_000_000, observed_at=NOW
    )
    after_recovery = await view_of(factory, planted.user_id)

    await plant_balance_run(
        factory, "partial", {BITCOIN: "failed"}, started_at=NOW - timedelta(hours=5)
    )
    after_second_failure = await view_of(factory, planted.user_id)

    assert after_failure.wallets == sources(failed_chains={BITCOIN: 1})
    assert after_recovery.wallets == sources(compared=1, oldest=NOW)
    assert by_asset(after_recovery)["BTC"].wallet_quantity == Decimal("0.25")
    assert after_second_failure.wallets == sources(failed_chains={BITCOIN: 1})
    assert by_asset(after_second_failure)["BTC"].wallet_quantity == 0


async def test_a_reading_written_by_the_latest_finished_run_itself_is_left_out(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The boundary, through the stored rows: `sync_run_id` equal to the run's id.

    The balance sync writes no snapshot for a chain it failed, so these two rows do not occur
    together. The rule is stated as "that run or an earlier one", and this is the row that
    tells `<=` from `<`.
    """
    planted = await plant_owner_with_history(factory)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    run = await plant_balance_run(factory, "partial", {BITCOIN: "failed"})
    await plant_snapshot(factory, wallet_id=wallet, run_id=run, confirmed=40_000_000)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(failed_chains={BITCOIN: 1})


# --------------------------------------------------------------------------------------
# Criterion 5: the counts, and the chains that are named
# --------------------------------------------------------------------------------------


async def test_the_four_counts_add_up_to_the_active_wallets(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Six active wallets and one archived, one of every kind, with Kaspa failed.

    | Wallet | Chain | Reading | Counted as |
    |---|---|---|---|
    | btc-a | bitcoin | 0.4, ten minutes old | compared |
    | btc-b | bitcoin | 9, three days old | stale |
    | btc-c | bitcoin | none | unread |
    | kas-a | kaspa | 600, from the run before the failure | chain_failed |
    | kas-b | kaspa | none | chain_failed |
    | kas-c | kaspa | 250, written by the run in flight | compared |
    | kas-old | kaspa, archived | 4000 | not a source |
    """
    planted = await plant_owner_with_history(factory)
    btc_a = await plant_wallet(factory, planted, "btc-a", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    btc_b = await plant_wallet(factory, planted, "btc-b", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    await plant_wallet(factory, planted, "btc-c", ChainKey.BITCOIN, BIP350_TESTNET_V1)
    kas_a = await plant_wallet(factory, planted, "kas-a", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_wallet(factory, planted, "kas-b", ChainKey.KASPA, KASPA_TESTNET_V1_KEY)
    kas_c = await plant_wallet(
        factory, planted, "kas-c", ChainKey.KASPA, KASPA_TESTNET_V0_ASPECTRON
    )
    retired = await plant_wallet(
        factory, planted, "kas-old", ChainKey.KASPA, BIP173_TESTNET_P2WPKH, archived=True
    )
    first = await plant_balance_run(factory, "success", {BITCOIN: "success", KASPA: "success"})
    await plant_snapshot(factory, wallet_id=btc_a, run_id=first, confirmed=40_000_000)
    await plant_snapshot(
        factory,
        wallet_id=btc_b,
        run_id=first,
        confirmed=900_000_000,
        observed_at=NOW - timedelta(days=3),
    )
    await plant_snapshot(
        factory, wallet_id=kas_a, run_id=first, confirmed=600 * SATS, observed_at=OBSERVED_OLDEST
    )
    await plant_snapshot(factory, wallet_id=retired, run_id=first, confirmed=4000 * SATS)
    await plant_balance_run(factory, "partial", {BITCOIN: "success", KASPA: "failed"})
    in_flight = await plant_balance_run(factory, "running")
    await plant_snapshot(
        factory,
        wallet_id=kas_c,
        run_id=in_flight,
        confirmed=250 * SATS,
        observed_at=OBSERVED_NEWEST,
    )

    view = await view_of(factory, planted.user_id)

    assert view.wallets == WalletSources(
        compared=2,
        stale=1,
        unread=1,
        chain_failed=2,
        failed_chains=(FailedChain(KASPA, 2),),
        oldest_observed_at=OBSERVED_NEWER,
    )
    counted = (
        view.wallets.compared + view.wallets.stale + view.wallets.unread + view.wallets.chain_failed
    )
    assert counted == 6, "one count per active wallet, and the archived one in none"
    rows = by_asset(view)
    assert rows["BTC"].wallet_quantity == Decimal("0.4")
    assert rows["KAS"].wallet_quantity == Decimal(250), "kas-c alone: 600 and 4000 are left out"


async def test_the_failed_chains_are_sorted_by_chain_key_each_with_its_own_count(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both chains failed. The Kaspa wallet was registered first, and Bitcoin is listed first."""
    planted = await plant_owner_with_history(factory)
    await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_wallet(factory, planted, "btc-a", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await plant_wallet(factory, planted, "btc-b", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    await plant_balance_run(factory, "failed", {KASPA: "failed", BITCOIN: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.wallets.failed_chains == (FailedChain(BITCOIN, 2), FailedChain(KASPA, 1))
    assert [chain.chain_key for chain in view.wallets.failed_chains] == [BITCOIN, KASPA]
    assert view.wallets.chain_failed == 3
    assert isinstance(view.wallets.failed_chains, tuple)
    assert view.wallets == sources(failed_chains={BITCOIN: 2, KASPA: 1})


async def test_a_failed_chain_with_no_wallet_left_out_is_not_listed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both chains failed in the latest finished run, and neither left a wallet of the owner out.

    The Bitcoin wallet has a reading a later run wrote. The only Kaspa wallet is archived, so
    it is not a source at all. `failed_chains` names a chain for the wallets it left out, so
    it is empty, and `wallets` is never zero.
    """
    planted, wallet, _first = await a_bitcoin_wallet_read_once(factory)
    await plant_wallet(factory, planted, "kas-old", ChainKey.KASPA, KASPA_TESTNET_V0, archived=True)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed", KASPA: "failed"})
    in_flight = await plant_balance_run(factory, "running")
    await plant_snapshot(
        factory, wallet_id=wallet, run_id=in_flight, confirmed=25_000_000, observed_at=NOW
    )

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(compared=1, oldest=NOW)
    assert view.wallets.failed_chains == ()
    assert view.wallets.chain_failed == 0


async def test_one_chain_lists_only_the_wallets_it_left_out(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three Bitcoin wallets and the chain failed. One has since been read by the run in
    flight: two are left out, and the entry says two, not three."""
    planted, read_again, first = await a_bitcoin_wallet_read_once(factory)
    second = await plant_wallet(factory, planted, "btc-b", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    await plant_wallet(factory, planted, "btc-c", ChainKey.BITCOIN, BIP350_TESTNET_V1)
    await plant_snapshot(factory, wallet_id=second, run_id=first, confirmed=30_000_000)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed"})
    in_flight = await plant_balance_run(factory, "interrupted")
    await plant_snapshot(
        factory,
        wallet_id=read_again,
        run_id=in_flight,
        confirmed=25_000_000,
        observed_at=OBSERVED_NEWEST,
    )

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(compared=1, failed_chains={BITCOIN: 2}, oldest=OBSERVED_NEWEST)
    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.25")


async def test_another_owners_wallets_on_the_failed_chain_are_not_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A balance run is the application's, not an owner's: its verdict on a chain applies to
    every owner's wallets, and each owner is told only of their own."""
    planted, _wallet, _first = await a_bitcoin_wallet_read_once(factory)
    other = await plant_owner_with_history(
        factory, [buy(9001, 0, "KAS", "7", "7")], username="second"
    )
    await plant_wallet(factory, other, "kas-a", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_wallet(factory, other, "kas-b", ChainKey.KASPA, KASPA_TESTNET_V1_KEY)
    await plant_wallet(factory, other, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed", KASPA: "failed"})

    mine = await view_of(factory, planted.user_id)
    theirs = await view_of(factory, other.user_id)

    assert mine.wallets == sources(failed_chains={BITCOIN: 1})
    assert theirs.wallets == sources(failed_chains={BITCOIN: 1, KASPA: 2})


# --------------------------------------------------------------------------------------
# Criterion 6: the oldest reading is among the compared wallets
# --------------------------------------------------------------------------------------


async def test_the_oldest_reading_ignores_a_wallet_left_out_for_its_chain(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Kaspa wallet's reading is the oldest there is, and Kaspa failed. It bounds nothing
    that is compared, so the oldest reading is the Bitcoin wallet's."""
    planted = await plant_owner_with_history(factory)
    bitcoin = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    kaspa = await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    first = await plant_balance_run(factory, "success", {BITCOIN: "success", KASPA: "success"})
    await plant_snapshot(
        factory, wallet_id=kaspa, run_id=first, confirmed=600 * SATS, observed_at=OBSERVED_OLDEST
    )
    await plant_snapshot(
        factory, wallet_id=bitcoin, run_id=first, confirmed=40_000_000, observed_at=OBSERVED_NEWER
    )
    await plant_balance_run(factory, "partial", {BITCOIN: "success", KASPA: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.wallets == sources(compared=1, failed_chains={KASPA: 1}, oldest=OBSERVED_NEWER)
    assert view.wallets.oldest_observed_at != OBSERVED_OLDEST


async def test_with_every_wallet_left_out_for_its_chain_there_is_no_oldest_reading(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_owner_with_history(factory)
    bitcoin = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    kaspa = await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    first = await plant_balance_run(factory, "success", {BITCOIN: "success", KASPA: "success"})
    await plant_snapshot(factory, wallet_id=bitcoin, run_id=first, confirmed=40_000_000)
    await plant_snapshot(factory, wallet_id=kaspa, run_id=first, confirmed=600 * SATS)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed", KASPA: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.wallets.oldest_observed_at is None
    assert view.wallets == sources(failed_chains={BITCOIN: 1, KASPA: 1})
    rows = by_asset(view)
    assert rows["BTC"].wallet_quantity == 0
    assert rows["KAS"].wallet_quantity == 0


# --------------------------------------------------------------------------------------
# Criterion 7: the scenario of the issue
# --------------------------------------------------------------------------------------


async def kas_sent_from_a_wallet_to_bitget(
    factory: async_sessionmaker[AsyncSession], chain_outcome: str
) -> Planted:
    """1000 KAS bought, read in a wallet, then sent to Bitget, whose next sync read it there.

    The balance run after the transfer ended with Kaspa `chain_outcome`. When it failed, the
    wallet's last reading is the one from before the transfer, twenty minutes old.
    """
    planted = await plant_owner_with_history(factory, [buy(1001, 0, "KAS", "1000", "100")])
    wallet = await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    before = await plant_balance_run(factory, "success", {KASPA: "success"})
    await plant_snapshot(
        factory, wallet_id=wallet, run_id=before, confirmed=1000 * SATS, observed_at=OBSERVED_OLDEST
    )
    await store_balances(
        factory, planted.accounts[ExchangeKey.BITGET], (held("KAS", "1000"),), BITGET_READ
    )
    after = await plant_balance_run(factory, "failed", {KASPA: chain_outcome})
    if chain_outcome == "success":
        await plant_snapshot(
            factory, wallet_id=wallet, run_id=after, confirmed=0, observed_at=OBSERVED_NEWEST
        )
    return planted


async def test_coins_sent_from_a_wallet_whose_chain_then_failed_are_not_counted_twice(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The issue, start to finish.

    The owner holds exactly the 1000 KAS the history accounts for. It sat in a wallet when the
    wallet was last read, twenty minutes ago; it was then deposited to Bitget, and the balance
    run after that could not read Kaspa. Bitget's reading, six minutes old, has the coins.

    Summed, the wallet's last reading and the venue's make 2000 held against 1000 in the
    history: `history_short` for 1000 KAS that do not exist, with advice to record an opening
    balance and nothing naming the wallet. With the wallet left out, 1000 is held, 1000 is
    accounted for, it is a `match`, and the source names Kaspa.
    """
    planted = await kas_sent_from_a_wallet_to_bitget(factory, "failed")

    view = await view_of(factory, planted.user_id)

    (row,) = view.assets
    assert figures(row) == decimals("1000", "0", "1000", "1000", "0")
    assert row.status is ReconciliationStatus.MATCH
    assert view.wallets == sources(failed_chains={KASPA: 1})


async def test_the_double_count_before_the_rule_is_what_the_age_limit_alone_reports(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The control on the scenario above: the same rows, read through the age limit alone.

    With the failed chain row removed, nothing leaves the wallet out and its twenty-minute-old
    reading is summed with the venue's: 2000 held against 1000. So the test above passes for
    the rule, and not because the rows planted cannot produce the double count.
    """
    planted = await kas_sent_from_a_wallet_to_bitget(factory, "failed")
    async with factory() as session:
        await session.execute(text("DELETE FROM sync_run_chains WHERE status = 'failed'"))
        await session.commit()

    view = await view_of(factory, planted.user_id)

    (row,) = view.assets
    assert figures(row) == decimals("1000", "1000", "1000", "2000", "1000")
    assert row.status is ReconciliationStatus.HISTORY_SHORT
    assert view.wallets == sources(compared=1, oldest=OBSERVED_OLDEST)


async def test_once_the_chain_is_read_again_the_wallet_is_compared_with_what_it_holds_now(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same transfer with Kaspa read by the run after it: the wallet holds nothing, the
    venue holds the 1000, and it is the same `match`, with no wallet left out."""
    planted = await kas_sent_from_a_wallet_to_bitget(factory, "success")

    view = await view_of(factory, planted.user_id)

    (row,) = view.assets
    assert figures(row) == decimals("1000", "0", "1000", "1000", "0")
    assert row.status is ReconciliationStatus.MATCH
    assert view.wallets == sources(compared=1, oldest=OBSERVED_NEWEST)


# --------------------------------------------------------------------------------------
# No snapshot, and what a request reads
# --------------------------------------------------------------------------------------


async def test_with_no_snapshot_the_failed_chains_are_still_answered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The sources do not depend on the accounting snapshot: "not computed" still says which
    chain could not be read."""
    planted = await plant_owner_with_history(factory, snapshot=False)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    first = await plant_balance_run(factory, "success", {BITCOIN: "success"})
    await plant_snapshot(factory, wallet_id=wallet, run_id=first, confirmed=40_000_000)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed"})

    view = await view_of(factory, planted.user_id)

    assert view.computed_at is None
    assert view.assets == ()
    assert view.wallets == sources(failed_chains={BITCOIN: 1})


async def recorded_statements(
    factory: async_sessionmaker[AsyncSession], user_id: int
) -> tuple[ReconciliationView, list[str]]:
    """One request's view, and every statement it sent to SQLite."""
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement)

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            service = build_reconciliation_service(session, clock=SettableClock(NOW))
            view = await service.reconciliation(user_id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
    return view, statements


async def test_the_latest_finished_run_is_read_once_whatever_the_number_of_wallets(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """One read of the run and one of its chains for six wallets, as for one: every wallet is
    held to the same run, and the request does not grow with the wallets."""
    planted = await plant_owner_with_history(factory)
    for name, chain, address in (
        ("a", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH),
        ("b", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH),
        ("c", ChainKey.BITCOIN, BIP350_TESTNET_V1),
        ("d", ChainKey.KASPA, KASPA_TESTNET_V0),
        ("e", ChainKey.KASPA, KASPA_TESTNET_V1_KEY),
        ("f", ChainKey.KASPA, KASPA_TESTNET_V0_ASPECTRON),
    ):
        await plant_wallet(factory, planted, name, chain, address)
    await plant_balance_run(factory, "success", {BITCOIN: "success", KASPA: "success"})
    await plant_balance_run(factory, "failed", {BITCOIN: "failed", KASPA: "failed"})

    view, statements = await recorded_statements(factory, planted.user_id)

    assert view.wallets == sources(failed_chains={BITCOIN: 3, KASPA: 3}), "the control"
    about_runs = [s for s in statements if "FROM sync_runs" in s]
    about_chains = [s for s in statements if "FROM sync_run_chains" in s]
    assert len(about_runs) == 1, about_runs
    assert len(about_chains) == 1, about_chains


async def test_nothing_about_a_run_is_decided_on_a_datetime_or_aggregated_in_sql(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 8: the run is chosen by its `status` and its `id`, never by a `TEXT` instant.

    `started_at`, `finished_at` and `observed_at` are text in SQLite. The newest run is the
    one with the highest `INTEGER` id, and a reading's run is compared with it in Python.
    """
    planted, _wallet, _first = await a_bitcoin_wallet_read_once(factory)
    await plant_balance_run(factory, "failed", {BITCOIN: "failed"})

    view, statements = await recorded_statements(factory, planted.user_id)

    assert view.wallets.chain_failed == 1, "the control: the rule was applied"
    assert [s for s in statements if not s.lstrip().upper().startswith("SELECT")] == []
    flat = [" ".join(statement.upper().split()) for statement in statements]
    about_runs = [s for s in flat if " FROM SYNC_RUNS" in s or " FROM SYNC_RUN_CHAINS" in s]
    assert len(about_runs) == 2, statements
    for statement in about_runs:
        for aggregate in ("SUM(", "TOTAL(", "AVG(", "MIN(", "MAX(", "COUNT("):
            assert aggregate not in statement, statement
        tail = statement.split(" FROM ", 1)[1]
        for instant in ("STARTED_AT", "FINISHED_AT", "OBSERVED_AT"):
            assert instant not in tail, f"{instant} is compared or ordered in SQL: {statement}"
    (the_run,) = [s for s in flat if " FROM SYNC_RUNS" in s]
    assert "SYNC_RUNS.STATUS IN" in the_run
    assert the_run.split(" ORDER BY ", 1)[1].startswith("SYNC_RUNS.ID DESC"), the_run
    (the_chains,) = [s for s in flat if " FROM SYNC_RUN_CHAINS" in s]
    assert " ORDER BY SYNC_RUN_CHAINS.CHAIN_KEY" in the_chains
    # Across the whole request: no instant is filtered or sorted on, a reading's run is not
    # compared with the latest finished one in SQL, and no column is summed.
    for statement in flat:
        for clause in (" WHERE ", " ORDER BY "):
            for part in statement.split(clause)[1:]:
                for instant in ("STARTED_AT", "FINISHED_AT", "OBSERVED_AT", "COMPUTED_AT"):
                    assert instant not in part, f"{instant} after{clause}in: {statement}"
        assert "SYNC_RUN_ID <" not in statement, "the run comparison is made in Python"
        assert "SYNC_RUN_ID >" not in statement, "the run comparison is made in Python"
        for aggregate in ("SUM(", "TOTAL(", "AVG("):
            assert aggregate not in statement, statement
