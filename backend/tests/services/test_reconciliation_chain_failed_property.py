"""Spec 028 (#116): the four wallet counts, against an oracle.

A property test drives `ReconciliationService` over in-memory repositories with any set of
wallets, any readings and any latest finished run, and holds the counts, the failed chains,
the oldest reading and the wallet quantities to a second statement of the rule written as set
arithmetic. The repositories are replaced only here: every other service test of the rule, in
`test_reconciliation_chain_failed.py`, reads a SQLite file.

## Why this is a module of its own, and must stay a short one

Hypothesis formats the traceback of **every** failing example while it shrinks, through
pytest, and pytest parses the whole module of the failing frame each time it does. The cost
of a failure is therefore the number of failing examples times the size of this file. Inside
the 1,460-line module this test was written in, a rule broken on purpose took 26 seconds to
shrink with no example database, as on a fresh checkout in CI, against the suite's 30-second
per-test timeout, which ends the whole session with a stack dump instead of the falsifying
example. Under any load it went over. In a module this size the same failure is reported
with its example in a few seconds.

So: no other test belongs here, and `report_multiple_bugs` is off, which shrinks the first
broken assertion alone rather than each of them in turn. One falsifying example is what is
acted on.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final, cast

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from portfolio.db.models import BalanceSnapshot, Wallet
from portfolio.repositories.accounting import SnapshotHeader
from portfolio.repositories.sync_runs import SyncRunStatus
from portfolio.services.accounting import StoredSnapshot
from portfolio.services.reconciliation import (
    FailedChain,
    ReconciliationService,
    WalletSources,
)
from tests.exchange_sync_harness import SettableClock
from tests.services.test_reconciliation_chain_failed import (
    A_DAY,
    BITCOIN,
    FAILED,
    KASPA,
    ONE_MICROSECOND,
    SUCCESS,
    TEN_MINUTES,
    finished,
)
from tests.services.test_reconciliation_service import NOW, SATS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from portfolio.repositories.balances import BalanceRepository
    from portfolio.repositories.exchange_balances import (
        AccountBalances,
        ExchangeBalanceRepository,
    )
    from portfolio.repositories.sync_runs import SyncRunRepository, SyncRunSummary
    from portfolio.repositories.wallets import WalletRepository
    from portfolio.services.accounting import AccountingService
    from portfolio.services.reconciliation import ReconciliationView


@dataclass(frozen=True)
class WalletCase:
    """One generated wallet: its chain and, when it has been read, the run and the reading."""

    chain_key: str
    written_by: int | None
    confirmed: int
    age: timedelta


class _Accounting:
    def __init__(self, snapshot: StoredSnapshot) -> None:
        self._snapshot = snapshot

    async def read_snapshot(self, user_id: int) -> StoredSnapshot:
        del user_id
        return self._snapshot


class _Wallets:
    def __init__(self, wallets: Sequence[Wallet]) -> None:
        self._wallets = list(wallets)

    async def list_for_user(self, user_id: int) -> list[Wallet]:
        del user_id
        return list(self._wallets)


class _Balances:
    def __init__(self, latest: Mapping[int, BalanceSnapshot]) -> None:
        self._latest = dict(latest)

    async def latest_for_wallets(self, wallet_ids: Sequence[int]) -> dict[int, BalanceSnapshot]:
        return {
            wallet_id: self._latest[wallet_id]
            for wallet_id in wallet_ids
            if wallet_id in self._latest
        }


class _SyncRuns:
    def __init__(self, run: SyncRunSummary | None) -> None:
        self._run = run
        self.reads = 0

    async def latest_finished(self) -> SyncRunSummary | None:
        self.reads += 1
        return self._run


class _ExchangeBalances:
    async def list_for_user(self, user_id: int) -> list[AccountBalances]:
        del user_id
        return []


EMPTY_HISTORY: Final = StoredSnapshot(
    header=SnapshotHeader(
        id=1,
        user_id=1,
        method="average_cost",
        engine_version=1,
        input_fingerprint="synthetic",
        event_count=0,
        unallocated_costs=Decimal(0),
        computed_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
    ),
    positions=(),
    warnings=(),
)
"""A snapshot of no events. Every unit a compared wallet holds is then a row of the comparison,
so the wallet quantities can be read off the view."""

SYMBOL: Final = {BITCOIN: "BTC", KASPA: "KAS"}

RUN_IDS: Final = st.integers(min_value=1, max_value=6)
AGES: Final = st.sampled_from(
    [
        -timedelta(minutes=5),
        timedelta(0),
        timedelta(minutes=10),
        A_DAY - ONE_MICROSECOND,
        A_DAY,
        A_DAY + ONE_MICROSECOND,
        timedelta(days=3),
    ]
)
WALLET_CASES: Final = st.lists(
    st.builds(
        WalletCase,
        chain_key=st.sampled_from([BITCOIN, KASPA]),
        written_by=st.none() | RUN_IDS,
        confirmed=st.integers(min_value=0, max_value=5 * SATS),
        age=AGES,
    ),
    max_size=8,
)
CHAIN_OUTCOMES: Final = st.dictionaries(
    st.sampled_from([BITCOIN, KASPA]), st.sampled_from([SUCCESS, FAILED]), max_size=2
)
LATEST_RUNS: Final = st.none() | st.builds(
    finished,
    run_id=RUN_IDS,
    chains=CHAIN_OUTCOMES,
    status=st.sampled_from([SyncRunStatus.SUCCESS, SyncRunStatus.PARTIAL, SyncRunStatus.FAILED]),
)


def expected_sources(
    cases: Sequence[WalletCase], run: SyncRunSummary | None
) -> tuple[dict[str, list[int]], dict[str, Decimal]]:
    """The rule a second time, as sets: which wallets fall under each heading, by index.

    Returns the indices under `compared`, `stale`, `unread` and `chain_failed`, and what the
    compared wallets hold per asset. Written from the spec's two conditions and its order of
    reasons, without calling the function under test.
    """
    failed = (
        set()
        if run is None
        else {outcome.chain_key for outcome in run.chains if outcome.status.value == "failed"}
    )
    groups: dict[str, list[int]] = {"compared": [], "stale": [], "unread": [], "chain_failed": []}
    held_by_asset: dict[str, Decimal] = {}
    for index, case in enumerate(cases):
        newer_than_the_run = (
            run is not None and case.written_by is not None and case.written_by > run.run_id
        )
        if case.chain_key in failed and not newer_than_the_run:
            groups["chain_failed"].append(index)
        elif case.written_by is None:
            groups["unread"].append(index)
        elif case.age > A_DAY:
            groups["stale"].append(index)
        else:
            groups["compared"].append(index)
            symbol = SYMBOL[case.chain_key]
            held_by_asset[symbol] = held_by_asset.get(symbol, Decimal(0)) + Decimal(
                case.confirmed
            ).scaleb(-8)
    return groups, held_by_asset


def view_over(cases: Sequence[WalletCase], run: SyncRunSummary | None) -> ReconciliationView:
    """The service's answer over repositories that hold exactly `cases` and `run`."""
    wallets = [
        Wallet(id=index + 1, user_id=1, chain_key=case.chain_key)
        for index, case in enumerate(cases)
    ]
    latest = {
        index + 1: BalanceSnapshot(
            wallet_id=index + 1,
            sync_run_id=case.written_by,
            confirmed=case.confirmed,
            pending=None,
            decimals=8,
            observed_at=NOW - case.age,
        )
        for index, case in enumerate(cases)
        if case.written_by is not None
    }
    sync_runs = _SyncRuns(run)
    service = ReconciliationService(
        accounting=cast("AccountingService", _Accounting(EMPTY_HISTORY)),
        wallets=cast("WalletRepository", _Wallets(wallets)),
        balances=cast("BalanceRepository", _Balances(latest)),
        sync_runs=cast("SyncRunRepository", sync_runs),
        exchange_balances=cast("ExchangeBalanceRepository", _ExchangeBalances()),
        clock=SettableClock(NOW),
    )
    view = asyncio.run(service.reconciliation(1))
    assert sync_runs.reads == 1, "the latest finished run is read once per request"
    return view


@settings(
    max_examples=400,
    deadline=None,
    report_multiple_bugs=False,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(cases=WALLET_CASES, run=LATEST_RUNS)
def test_the_counts_the_chains_and_the_quantities_agree_with_the_rule_restated(
    cases: list[WalletCase], run: SyncRunSummary | None
) -> None:
    """For any wallets, any readings and any latest finished run (criteria 1 to 6 at once).

    * each wallet is counted exactly once, so the four counts add up to the wallets;
    * each count is the one the restated rule gives;
    * `failed_chains` lists exactly the chains with a wallet left out, sorted, never with a
      zero, and its counts add up to `chain_failed`;
    * `oldest_observed_at` is the oldest reading among the compared wallets, and `None` when
      none is compared;
    * a wallet that is not compared contributes nothing to any quantity.
    """
    groups, held_by_asset = expected_sources(cases, run)

    view = view_over(cases, run)
    found = view.wallets

    assert found.compared == len(groups["compared"])
    assert found.stale == len(groups["stale"])
    assert found.unread == len(groups["unread"])
    assert found.chain_failed == len(groups["chain_failed"])
    assert found.compared + found.stale + found.unread + found.chain_failed == len(cases)

    left_out = Counter(cases[index].chain_key for index in groups["chain_failed"])
    assert found.failed_chains == tuple(
        FailedChain(chain_key, left_out[chain_key]) for chain_key in sorted(left_out)
    )
    assert all(chain.wallets > 0 for chain in found.failed_chains)
    assert sum(chain.wallets for chain in found.failed_chains) == found.chain_failed
    assert [chain.chain_key for chain in found.failed_chains] == sorted(
        {chain.chain_key for chain in found.failed_chains}
    )

    observed = [NOW - cases[index].age for index in groups["compared"]]
    assert found.oldest_observed_at == (min(observed) if observed else None)

    quantities = {row.asset: row.wallet_quantity for row in view.assets}
    assert quantities == {asset: total for asset, total in held_by_asset.items() if total > 0}


def test_the_oracle_and_the_service_are_driven_through_a_case_of_every_kind() -> None:
    """The control on the property: one hand-built example with every heading occupied, so
    that agreement above is not agreement between two answers that are both always empty."""
    cases = [
        WalletCase(BITCOIN, 2, 40_000_000, TEN_MINUTES),
        WalletCase(BITCOIN, 2, 900_000_000, timedelta(days=3)),
        WalletCase(BITCOIN, None, 0, TEN_MINUTES),
        WalletCase(KASPA, 4, 600 * SATS, TEN_MINUTES),
        WalletCase(KASPA, None, 0, TEN_MINUTES),
        WalletCase(KASPA, 5, 250 * SATS, timedelta(0)),
    ]
    run = finished(4, {BITCOIN: SUCCESS, KASPA: FAILED})

    groups, held_by_asset = expected_sources(cases, run)
    view = view_over(cases, run)

    assert groups == {"compared": [0, 5], "stale": [1], "unread": [2], "chain_failed": [3, 4]}
    assert held_by_asset == {"BTC": Decimal("0.4"), "KAS": Decimal(250)}
    assert view.wallets == WalletSources(
        compared=2,
        stale=1,
        unread=1,
        chain_failed=2,
        failed_chains=(FailedChain(KASPA, 2),),
        oldest_observed_at=NOW - TEN_MINUTES,
    )
    assert {row.asset: row.wallet_quantity for row in view.assets} == held_by_asset
