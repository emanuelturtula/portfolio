"""Spec 028 (#116), timeline by timeline: the real balance sync, then the real holdings check.

`tests/services/test_reconciliation_chain_failed.py` plants `sync_runs`, `sync_run_chains` and
`balance_snapshots` by hand, which is how it reaches every state. This module plants none of
them. Each run here is `BalanceSyncService.sync` over a real SQLite file, with chain providers
that answer from a dictionary or fail on command, and each answer is the real
`ReconciliationService` reading what that sync left. What is asserted is therefore the two
halves together: the rows the sync really writes, in the order it really writes them, are the
rows the check reads its verdict from.

That matters for the states that only exist because of how a run is written:

* **A run commits each chain's readings before it closes.** So a run in flight, one whose
  close-out failed, and one swept to `interrupted` can each have written a reading newer than
  the latest finished run's verdict. The check keeps that reading (criterion 3).
* **A run that dies before reading anything writes nothing**, and the verdict before it stands
  (criterion 4).
* **A run and a request are not atomic with each other.** The service reads the latest
  finished run and the latest snapshots in two statements, and a run can land between them.
  Ruling R3 fixed the order of those reads, and the interleaving is forced here.

The scenario every timeline starts from: 1 BTC in the history, one Bitcoin wallet holding it
and one Kaspa wallet, both read by run 1. The Kaspa wallet is there so that a run in which
Bitcoin fails is `partial`, as it is for an owner with both chains. Every address is a
published testnet vector and every amount is made up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import text

from portfolio.domain.accounting import ReconciliationStatus
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.errors import ProviderUnavailableError
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.exchange_balances import ExchangeBalanceRepository
from portfolio.repositories.sync_runs import (
    SyncErrorKind,
    SyncRunRepository,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.accounting import build_accounting_service
from portfolio.services.balance_sync import BalanceSyncService
from portfolio.services.reconciliation import (
    FailedChain,
    ReconciliationService,
    ReconciliationView,
    build_reconciliation_service,
)
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WSH, KASPA_TESTNET_V0
from tests.balance_harness import StubChainProvider, sqlite_timestamp
from tests.exchange_sync_harness import SettableClock, held
from tests.services.test_reconciliation_service import (
    Planted,
    buy,
    by_asset,
    plant_owner_with_history,
    plant_wallet,
    store_balances,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.db.models import BalanceSnapshot
    from portfolio.repositories.sync_runs import ChainOutcome, SyncRunSummary

SATS: Final = 100_000_000
START: Final = datetime(2026, 10, 1, 10, 5, tzinfo=UTC)
AN_INTERVAL: Final = timedelta(minutes=15)

BTC_A: Final = BIP173_TESTNET_P2WPKH
BTC_B: Final = BIP173_TESTNET_P2WSH
KAS: Final = KASPA_TESTNET_V0

BITCOIN: Final = "bitcoin"
KASPA: Final = "kaspa"

MATCH: Final = ReconciliationStatus.MATCH


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


class CloseOutError(RuntimeError):
    """What a run's close-out is made to raise: the database was locked."""


class PowerLoss(BaseException):
    """The process died. A `BaseException`, so nothing in the sync records it as a failure."""


class Chains:
    """What each chain's index answers when a sync asks, and whether Bitcoin's is down."""

    def __init__(self) -> None:
        self.bitcoin: dict[str, int] = {BTC_A: 1 * SATS, BTC_B: 0}
        self.kaspa: dict[str, int] = {KAS: 5 * SATS}
        self.bitcoin_down = False
        self.asked: list[str] = []

    def __call__(self, chain_key: str) -> StubChainProvider:
        self.asked.append(chain_key)
        if ChainKey(chain_key) is ChainKey.BITCOIN:
            down = ProviderUnavailableError("the index is down") if self.bitcoin_down else None
            return StubChainProvider(ChainKey.BITCOIN, self.bitcoin, raises=down)
        return StubChainProvider(ChainKey.KASPA, self.kaspa)


class Timeline:
    """One owner, one database, one clock, and the chains the syncs read."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], planted: Planted) -> None:
        self.factory = factory
        self.planted = planted
        self.clock = SettableClock(START)
        self.chains = Chains()

    async def sync(
        self,
        *,
        runs: type[SyncRunRepository] = SyncRunRepository,
        balances: type[BalanceRepository] = BalanceRepository,
        provider_for: Callable[[str], StubChainProvider] | None = None,
    ) -> SyncRunSummary:
        """One balance sync, a quarter of an hour after the last thing that happened."""
        self.clock.advance(AN_INTERVAL)
        async with self.factory() as session:
            service = BalanceSyncService(
                session=session,
                wallets=WalletRepository(session),
                runs=runs(session),
                balances=balances(session),
                provider_for=provider_for or self.chains,
                clock=self.clock,
            )
            return await service.sync(SyncTrigger.SCHEDULED)

    async def venue_holds(self, btc: str) -> None:
        """Bitget's spot account is read now, holding `btc` BTC."""
        balances = () if Decimal(btc) == 0 else (held("BTC", btc),)
        await store_balances(
            self.factory, self.planted.accounts[ExchangeKey.BITGET], balances, self.clock.moment
        )

    async def view(self) -> ReconciliationView:
        async with self.factory() as session:
            service = build_reconciliation_service(session, clock=self.clock)
            return await service.reconciliation(self.planted.user_id)

    async def sweep(self) -> int:
        """What the lifespan does at the next start: orphan runs become `interrupted`."""
        async with self.factory() as session:
            swept = await SyncRunRepository(session).sweep_interrupted()
            await session.commit()
        return swept

    async def run_statuses(self) -> list[tuple[int, str]]:
        async with self.factory() as session:
            rows = await session.execute(text("SELECT id, status FROM sync_runs ORDER BY id"))
            return [(row[0], row[1]) for row in rows]

    async def readings(self) -> list[tuple[int, int, int]]:
        """Every snapshot as `(wallet, run, confirmed)`, in the order they were written."""
        async with self.factory() as session:
            rows = await session.execute(
                text("SELECT wallet_id, sync_run_id, confirmed FROM balance_snapshots ORDER BY id")
            )
            return [(row[0], row[1], row[2]) for row in rows]


async def started(factory: async_sessionmaker[AsyncSession]) -> Timeline:
    """Run 1 read both wallets, and Bitget holds no BTC: 1 BTC in the history, 1 in a wallet."""
    planted = await plant_owner_with_history(factory, [buy(1, 0, "BTC", "1", "100")])
    await plant_wallet(factory, planted, "btc-a", ChainKey.BITCOIN, BTC_A)
    await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KAS)
    timeline = Timeline(factory, planted)
    first = await timeline.sync()
    assert (first.run_id, first.status) == (1, SyncRunStatus.SUCCESS)
    await timeline.venue_holds("0")
    return timeline


async def coins_sent_and_the_chain_fails(timeline: Timeline) -> SyncRunSummary:
    """The 1 BTC leaves the wallet for Bitget; run 2 cannot read Bitcoin; Bitget is read."""
    timeline.chains.bitcoin[BTC_A] = 0
    timeline.chains.bitcoin_down = True
    second = await timeline.sync()
    assert (second.run_id, second.status) == (2, SyncRunStatus.PARTIAL)
    await timeline.venue_holds("1")
    return second


def outcome(summary: SyncRunSummary, chain_key: str) -> ChainOutcome:
    (found,) = [chain for chain in summary.chains if chain.chain_key == chain_key]
    return found


def wallet_counts(view: ReconciliationView) -> tuple[int, int, int, int]:
    wallets = view.wallets
    return wallets.compared, wallets.stale, wallets.unread, wallets.chain_failed


def btc(view: ReconciliationView) -> tuple[Decimal, Decimal, Decimal, ReconciliationStatus]:
    """BTC's history, wallet and exchange quantities, and its status."""
    row = by_asset(view)["BTC"]
    return row.history_quantity, row.wallet_quantity, row.exchange_quantity, row.status


def quantities(history: str, wallets: str, exchanges: str) -> tuple[Decimal, Decimal, Decimal]:
    return Decimal(history), Decimal(wallets), Decimal(exchanges)


BITCOIN_LEFT_OUT: Final = (FailedChain(BITCOIN, 1),)


# --------------------------------------------------------------------------------------
# Criterion 7: the issue, through the real sync
# --------------------------------------------------------------------------------------


async def test_the_issue_with_runs_the_balance_sync_really_wrote(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Read, sent to the venue, the chain down, the chain back: a match at every step.

    The 1 BTC is counted in the wallet while the wallet was the last place it was read, at
    the venue once the venue has read it, and never in both.
    """
    timeline = await started(factory)
    healthy = await timeline.view()

    second = await coins_sent_and_the_chain_fails(timeline)
    failed = await timeline.view()

    timeline.chains.bitcoin_down = False
    third = await timeline.sync()
    recovered = await timeline.view()

    assert wallet_counts(healthy) == (2, 0, 0, 0)
    assert btc(healthy) == (*quantities("1", "1", "0"), MATCH)

    assert outcome(second, BITCOIN).status is SyncRunStatus.FAILED
    assert outcome(second, BITCOIN).error_kind is SyncErrorKind.UNAVAILABLE
    assert outcome(second, KASPA).status is SyncRunStatus.SUCCESS
    assert wallet_counts(failed) == (1, 0, 0, 1)
    assert failed.wallets.failed_chains == BITCOIN_LEFT_OUT
    assert btc(failed) == (*quantities("1", "0", "1"), MATCH)

    assert third.status is SyncRunStatus.SUCCESS
    assert wallet_counts(recovered) == (2, 0, 0, 0)
    assert recovered.wallets.failed_chains == ()
    assert btc(recovered) == (*quantities("1", "0", "1"), MATCH)
    assert timeline.chains.asked.count(BITCOIN) == 3, "each sync went through the provider seam"


# --------------------------------------------------------------------------------------
# Criteria 3 and 4: runs that did not finish
# --------------------------------------------------------------------------------------


async def test_a_run_in_flight_that_has_read_the_chain_already_counts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The holdings check is asked from inside run 3, after it wrote Bitcoin's reading and
    before it closed.

    Run 3 is `running` and has no chain rows, so the latest finished run is still run 2 and
    its verdict on Bitcoin is `failed`. The wallet's reading is run 3's, newer than that
    verdict, and it is compared: nothing changes when the run then closes.
    """
    timeline = await started(factory)
    await coins_sent_and_the_chain_fails(timeline)
    timeline.chains.bitcoin_down = False
    seen: list[ReconciliationView] = []
    statuses: list[list[tuple[int, str]]] = []

    class Observing(SyncRunRepository):
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
            seen.append(await timeline.view())
            statuses.append(await timeline.run_statuses())
            await super().finish_run(
                run_id,
                status=status,
                finished_at=finished_at,
                duration_ms=duration_ms,
                wallets_succeeded=wallets_succeeded,
                wallets_failed=wallets_failed,
                chains=chains,
            )

    third = await timeline.sync(runs=Observing)
    after = await timeline.view()

    (in_flight,) = seen
    assert statuses == [[(1, "success"), (2, "partial"), (3, "running")]]
    assert wallet_counts(in_flight) == (2, 0, 0, 0)
    assert in_flight.wallets.failed_chains == ()
    assert btc(in_flight) == (*quantities("1", "0", "1"), MATCH)
    assert third.status is SyncRunStatus.SUCCESS
    assert after.wallets == in_flight.wallets
    assert after.assets == in_flight.assets


async def test_a_run_whose_close_out_failed_keeps_the_readings_it_wrote(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run 3 read Bitcoin, committed the reading, and then could not close: an orphan
    `running` row, swept to `interrupted` at the next start.

    Before and after the sweep the latest finished run is run 2, with Bitcoin failed, and the
    wallet is compared on the reading run 3 wrote. A run that then fails the chain is a new
    verdict, newer than that reading, and leaves the wallet out again.
    """
    timeline = await started(factory)
    await coins_sent_and_the_chain_fails(timeline)
    timeline.chains.bitcoin_down = False

    class CloseOutFails(SyncRunRepository):
        async def finish_run(self, run_id: int, **_: object) -> None:
            raise CloseOutError(f"database is locked (run {run_id})")

    with pytest.raises(CloseOutError):
        await timeline.sync(runs=CloseOutFails)
    orphan = await timeline.view()
    orphan_statuses = await timeline.run_statuses()

    swept = await timeline.sweep()
    interrupted = await timeline.view()
    interrupted_statuses = await timeline.run_statuses()

    timeline.chains.bitcoin_down = True
    fourth = await timeline.sync()
    failed_again = await timeline.view()

    bitcoin_wallet = timeline.planted.wallets["btc-a"]
    assert (bitcoin_wallet, 3, 0) in await timeline.readings(), "run 3 committed its reading"
    assert orphan_statuses == [(1, "success"), (2, "partial"), (3, "running")]
    assert wallet_counts(orphan) == (2, 0, 0, 0)
    assert btc(orphan) == (*quantities("1", "0", "1"), MATCH)

    assert swept == 1
    assert interrupted_statuses == [(1, "success"), (2, "partial"), (3, "interrupted")]
    assert interrupted.wallets == orphan.wallets
    assert interrupted.assets == orphan.assets

    assert (fourth.run_id, fourth.status) == (4, SyncRunStatus.PARTIAL)
    assert wallet_counts(failed_again) == (1, 0, 0, 1)
    assert failed_again.wallets.failed_chains == BITCOIN_LEFT_OUT
    assert btc(failed_again) == (*quantities("1", "0", "1"), MATCH)


async def test_a_wallet_added_beside_one_an_interrupted_run_read_is_left_out_alone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ruling R1's state. Run 2 finished with Bitcoin failed. Run 3 read Bitcoin and wrote
    wallet A's reading, then was interrupted. Wallet B is added on Bitcoin afterwards.

    A is compared: its reading is newer than run 2's verdict. B has no reading and the latest
    **finished** run failed its chain, so B is left out, although the last sync there was did
    read Bitcoin. That is why the notice says "the last balance sync that finished".
    """
    timeline = await started(factory)
    timeline.chains.bitcoin_down = True
    second = await timeline.sync()
    timeline.chains.bitcoin_down = False

    class CloseOutFails(SyncRunRepository):
        async def finish_run(self, run_id: int, **_: object) -> None:
            raise CloseOutError(f"database is locked (run {run_id})")

    with pytest.raises(CloseOutError):
        await timeline.sync(runs=CloseOutFails)
    assert await timeline.sweep() == 1
    await plant_wallet(factory, timeline.planted, "btc-b", ChainKey.BITCOIN, BTC_B)

    view = await timeline.view()

    wallet_a = timeline.planted.wallets["btc-a"]
    assert second.status is SyncRunStatus.PARTIAL
    assert await timeline.run_statuses() == [(1, "success"), (2, "partial"), (3, "interrupted")]
    assert [reading for reading in await timeline.readings() if reading[0] == wallet_a] == [
        (wallet_a, 1, SATS),
        (wallet_a, 3, SATS),
    ]
    assert wallet_counts(view) == (2, 0, 0, 1), "A and the Kaspa wallet compared; B left out"
    assert view.wallets.failed_chains == BITCOIN_LEFT_OUT
    assert btc(view) == (*quantities("1", "1", "0"), MATCH)


async def test_a_run_that_died_before_reading_anything_leaves_the_verdict_standing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run 2 failed Bitcoin and finished. Run 3 died before it read a chain and was swept.

    It wrote no reading and has no chain row. Run 2 is still the latest finished run, the
    wallet's reading is run 1's, and the wallet stays left out.
    """
    timeline = await started(factory)
    await coins_sent_and_the_chain_fails(timeline)
    before = await timeline.view()

    def dying(chain_key: str) -> StubChainProvider:
        raise PowerLoss(chain_key)

    with pytest.raises(PowerLoss):
        await timeline.sync(provider_for=dying)
    running = await timeline.view()
    assert await timeline.sweep() == 1
    interrupted = await timeline.view()

    bitcoin_wallet = timeline.planted.wallets["btc-a"]
    assert await timeline.run_statuses() == [(1, "success"), (2, "partial"), (3, "interrupted")]
    assert [r for r in await timeline.readings() if r[0] == bitcoin_wallet] == [
        (bitcoin_wallet, 1, SATS)
    ]
    for view in (before, running, interrupted):
        assert wallet_counts(view) == (1, 0, 0, 1)
        assert view.wallets.failed_chains == BITCOIN_LEFT_OUT
        assert btc(view) == (*quantities("1", "0", "1"), MATCH)


# --------------------------------------------------------------------------------------
# A failure that is ours, and a wallet the failed run never saw
# --------------------------------------------------------------------------------------


async def test_a_reading_that_could_not_be_stored_fails_the_chain_and_leaves_the_wallet_out(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The index answered and the snapshot write failed: the sync records Bitcoin as `failed`
    with kind `internal`, and no reading of that run exists.

    Not an outage, and the same consequence: nothing newer than run 1 says what the wallet
    holds, so it is left out. A Bitcoin wallet added afterwards is left out with it.
    """
    timeline = await started(factory)
    bitcoin_wallet = timeline.planted.wallets["btc-a"]

    class RefusesBitcoin(BalanceRepository):
        async def record(
            self,
            *,
            wallet_id: int,
            sync_run_id: int,
            confirmed: int,
            pending: int | None,
            decimals: int,
            observed_at: datetime,
        ) -> BalanceSnapshot:
            if wallet_id == bitcoin_wallet:
                raise RuntimeError("disk full")
            return await super().record(
                wallet_id=wallet_id,
                sync_run_id=sync_run_id,
                confirmed=confirmed,
                pending=pending,
                decimals=decimals,
                observed_at=observed_at,
            )

    second = await timeline.sync(balances=RefusesBitcoin)
    write_failed = await timeline.view()
    await plant_wallet(factory, timeline.planted, "btc-b", ChainKey.BITCOIN, BTC_B)
    wallet_added = await timeline.view()

    assert second.status is SyncRunStatus.PARTIAL
    assert outcome(second, BITCOIN).status is SyncRunStatus.FAILED
    assert outcome(second, BITCOIN).error_kind is SyncErrorKind.INTERNAL
    assert [r for r in await timeline.readings() if r[0] == bitcoin_wallet] == [
        (bitcoin_wallet, 1, SATS)
    ]
    assert wallet_counts(write_failed) == (1, 0, 0, 1)
    assert write_failed.wallets.failed_chains == BITCOIN_LEFT_OUT
    assert btc(write_failed) == (*quantities("1", "0", "0"), ReconciliationStatus.HISTORY_OVER), (
        "left out, the wallet can hide a finding and cannot invent one"
    )
    assert wallet_counts(wallet_added) == (1, 0, 0, 2)
    assert wallet_added.wallets.failed_chains == (FailedChain(BITCOIN, 2),)


# --------------------------------------------------------------------------------------
# The window ruling R5 states: a chain the latest finished run did not attempt
# --------------------------------------------------------------------------------------


async def set_archived(timeline: Timeline, name: str, *, archived: bool) -> None:
    async with timeline.factory() as session:
        await session.execute(
            text("UPDATE wallets SET archived_at = :at WHERE id = :id"),
            {
                "at": sqlite_timestamp(timeline.clock.moment) if archived else None,
                "id": timeline.planted.wallets[name],
            },
        )
        await session.commit()


async def test_a_chain_the_latest_finished_run_did_not_attempt_has_no_verdict_in_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second window of ruling R5, pinned as it is built and as the documents state it.

    Run 2 failed Bitcoin and the wallet is left out. The owner archives the wallet, the only
    one on Bitcoin, and run 3 runs: it attempts Kaspa alone and has no row for Bitcoin. The
    wallet is then restored. The latest finished run is run 3, which says nothing of Bitcoin,
    so the wallet's reading from run 1 is compared again, with the 1 BTC that has since been
    read at the venue: `history_short`, until the next run finishes and gives Bitcoin a
    verdict.

    The age limit alone did the same, so the rule did not open this. Closing it means
    comparing only a reading written by the latest finished run or a later one, which is a
    different rule and not this issue's.
    """
    timeline = await started(factory)
    await coins_sent_and_the_chain_fails(timeline)
    left_out = await timeline.view()

    await set_archived(timeline, "btc-a", archived=True)
    third = await timeline.sync()
    archived = await timeline.view()

    await set_archived(timeline, "btc-a", archived=False)
    restored = await timeline.view()

    fourth = await timeline.sync()
    next_run = await timeline.view()

    assert wallet_counts(left_out) == (1, 0, 0, 1)

    assert third.status is SyncRunStatus.SUCCESS
    assert [chain.chain_key for chain in third.chains] == [KASPA], "Bitcoin was not attempted"
    assert wallet_counts(archived) == (1, 0, 0, 0), "an archived wallet is not a source"
    assert btc(archived) == (*quantities("1", "0", "1"), MATCH)

    assert wallet_counts(restored) == (2, 0, 0, 0)
    assert restored.wallets.failed_chains == ()
    assert btc(restored) == (*quantities("1", "1", "1"), ReconciliationStatus.HISTORY_SHORT)

    assert outcome(fourth, BITCOIN).status is SyncRunStatus.FAILED
    assert wallet_counts(next_run) == (1, 0, 0, 1)
    assert next_run.wallets.failed_chains == BITCOIN_LEFT_OUT
    assert btc(next_run) == (*quantities("1", "0", "1"), MATCH)


# --------------------------------------------------------------------------------------
# Ruling R3: a run that lands between the service's two reads
# --------------------------------------------------------------------------------------


class Between:
    """Lets one thing happen after the first of the service's two reads, and records both.

    The service reads the latest finished run and the wallets' latest snapshots in two
    statements. Whichever it makes first, `land` runs once that read has answered and before
    the other is made: a balance run that starts, writes and finishes between the two.
    """

    def __init__(self, land: Callable[[], Awaitable[SyncRunSummary]]) -> None:
        self._land = land
        self.order: list[str] = []
        self.landed: list[SyncRunSummary] = []

    async def after(self, read: str) -> None:
        self.order.append(read)
        if len(self.order) == 1:
            self.landed.append(await self._land())


def racing_service(
    session: AsyncSession, timeline: Timeline, between: Between
) -> ReconciliationService:
    """The real service over the real repositories, each of the two reads reporting in."""

    class Runs(SyncRunRepository):
        async def latest_finished(self) -> SyncRunSummary | None:
            found = await super().latest_finished()
            await between.after("latest_finished")
            return found

    class Balances(BalanceRepository):
        async def latest_for_wallets(self, wallet_ids: Sequence[int]) -> dict[int, BalanceSnapshot]:
            found = await super().latest_for_wallets(wallet_ids)
            await between.after("latest_for_wallets")
            return found

    return ReconciliationService(
        accounting=build_accounting_service(session, clock=timeline.clock),
        wallets=WalletRepository(session),
        balances=Balances(session),
        sync_runs=Runs(session),
        exchange_balances=ExchangeBalanceRepository(session),
        clock=timeline.clock,
    )


async def test_a_run_that_reads_the_chain_between_the_two_reads_is_never_a_false_finding(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The interleaving the review forced, whichever read the service makes first.

    Run 2 failed Bitcoin, the coins are at the venue, and the wallet is left out: a match.
    Then one request is served while run 3 lands in the middle of it: run 3 reads Bitcoin,
    writes the wallet's reading (0 BTC) and finishes `success`, after the service's first
    read and before its second.

    Read as snapshots first and the run second, the request pairs the reading from **before**
    run 3 (1 BTC, run 1's) with the verdict **of** run 3 (`success`): the wallet is compared
    holding 1 BTC beside the venue's 1 BTC, 2 held against 1, `history_short`. That is the
    false finding this issue removes, served for one response.

    Read as the run first, the request pairs run 2's verdict with run 3's reading, which has
    a higher `sync_run_id` and is kept: 0 in the wallet, 1 at the venue, a match. That is the
    state after run 3, so the response is one that held at some instant.
    """
    timeline = await started(factory)
    await coins_sent_and_the_chain_fails(timeline)
    before = await timeline.view()
    timeline.chains.bitcoin_down = False
    between = Between(timeline.sync)

    async with factory() as session:
        raced = await racing_service(session, timeline, between).reconciliation(
            timeline.planted.user_id
        )
    after = await timeline.view()

    (landed,) = between.landed
    bitcoin_wallet = timeline.planted.wallets["btc-a"]
    assert (landed.run_id, landed.status) == (3, SyncRunStatus.SUCCESS), "the run did land"
    assert (bitcoin_wallet, 3, 0) in await timeline.readings()
    assert len(between.order) == 2, "the service made both reads, once each"

    assert btc(before) == (*quantities("1", "0", "1"), MATCH)
    assert wallet_counts(before) == (1, 0, 0, 1)

    assert btc(raced)[3] is MATCH, "one response with units that do not exist"
    assert btc(raced) == (*quantities("1", "0", "1"), MATCH)
    assert raced.wallets.failed_chains == ()
    assert wallet_counts(raced) == (2, 0, 0, 0)
    assert raced.wallets == after.wallets, "the raced response is the state after the run"
    assert raced.assets == after.assets


async def test_the_latest_finished_run_is_read_before_the_latest_snapshots(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ruling R3, stated as the order itself: the run, then the snapshots.

    The test above is the consequence. This is the cause, so that a change of order fails
    with its name rather than with a quantity.
    """
    timeline = await started(factory)
    await coins_sent_and_the_chain_fails(timeline)

    async def nothing_lands() -> SyncRunSummary:
        async with factory() as session:
            (latest,) = await SyncRunRepository(session).list_runs(limit=1)
        return latest

    between = Between(nothing_lands)
    async with factory() as session:
        view = await racing_service(session, timeline, between).reconciliation(
            timeline.planted.user_id
        )

    assert between.order == ["latest_finished", "latest_for_wallets"]
    assert wallet_counts(view) == (1, 0, 0, 1), "the control: the rule was applied"


async def test_a_run_that_fails_the_chain_between_the_two_reads_is_the_state_before_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other run that can land: one that fails the chain, and so writes no reading.

    Run 1 read the wallet. One request is served while run 2 fails Bitcoin in the middle of
    it. The service read "run 1, success" first and then the wallet's reading, which is run
    1's: the wallet is compared, as it was when the request began. The next request sees run
    2 and leaves it out. Neither is a state that never held.
    """
    timeline = await started(factory)
    timeline.chains.bitcoin_down = True
    between = Between(timeline.sync)

    async with factory() as session:
        raced = await racing_service(session, timeline, between).reconciliation(
            timeline.planted.user_id
        )
    after = await timeline.view()

    (landed,) = between.landed
    assert (landed.run_id, landed.status) == (2, SyncRunStatus.PARTIAL)
    assert wallet_counts(raced) == (2, 0, 0, 0)
    assert btc(raced) == (*quantities("1", "1", "0"), MATCH)
    assert wallet_counts(after) == (1, 0, 0, 1)
    assert after.wallets.failed_chains == BITCOIN_LEFT_OUT
