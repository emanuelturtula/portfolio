"""Spec 031, criteria 4 and 5: the balance sync over an extended-key wallet.

Two kinds of provider drive these tests, each for what only it can show.

* **The real `EsploraProvider`** over the address-answering fake in
  `tests/extended_key_harness.py`, with a spy on its `derive_child`. This is criterion 4 as
  the issue states it: a second sync derives nothing, and a newly used index derives only up
  to twenty past it. The spy observes; the real derivation still runs, so the addresses that
  land in `derived_addresses` are the ones BIP-84 gives.
* **A scripted scanner** that returns exactly the `ExtendedKeyScan` a test hands it. This is
  how the sum's edges are reached (a signed pending, a sum too large to store) and how a scan
  that breaks its contract is produced, which no real provider does. Its addresses are
  position labels rather than addresses: nothing on the sync's path reads them as one.

Everything is read back off the disk through a second session, for the reason
`tests/balance_harness.py` gives: the question is what is in the file.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest
import structlog
from sqlalchemy import text
from structlog.testing import capture_logs

from portfolio.domain.chains import ChainKey
from portfolio.domain.currencies import QuoteCurrency
from portfolio.domain.extended_keys import CHANGE_BRANCH, RECEIVE_BRANCH
from portfolio.providers.base import (
    ExtendedKeyScan,
    ExtendedKeyScanner,
    KnownDerivedAddress,
    ScannedAddress,
)
from portfolio.repositories.sync_runs import SyncErrorKind, SyncRunStatus, SyncTrigger
from portfolio.services.auth import Principal
from portfolio.services.balance_sync import (
    ExtendedKeysUnsupportedError,
    build_balance_sync_service,
)
from portfolio.services.balances import build_balance_service
from portfolio.services.wallets import build_wallet_service
from tests.address_vectors import BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V0
from tests.balance_harness import (
    StubChainProvider,
    insert_user,
    insert_wallet,
    snapshots,
    sqlite_timestamp,
    sync_run_chains,
    sync_runs,
)
from tests.extended_key_forms import TPUB_VERSION, depth_zero, reserialised
from tests.extended_key_harness import (
    CRITERION_THREE_CONFIRMED,
    CRITERION_THREE_PENDING,
    AddressBook,
    DerivationSpy,
    Holding,
    criterion_three_book,
    provider_over,
    spy_on_derivation,
)
from tests.extended_key_harness import insert_key_wallet as harness_insert_key_wallet
from tests.extended_key_vectors import BIP32_TV1_M, SCAN_CHANGE, SCAN_KEY, SCAN_RECEIVE
from tests.logging_harness import preserved_logging
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Callable,
        Iterator,
        Mapping,
        MutableMapping,
        Sequence,
    )
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.base import ChainProvider
    from portfolio.repositories.sync_runs import SyncRunSummary

FIRST_SYNC: Final = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
SECOND_SYNC: Final = datetime(2026, 10, 3, 8, 15, tzinfo=UTC)

#: Every position criterion 3's scan reaches: receive 0 to 44, change 0 to 23.
CRITERION_THREE_POSITIONS: Final = [(RECEIVE_BRANCH, index) for index in range(45)] + [
    (CHANGE_BRANCH, index) for index in range(24)
]
CRITERION_THREE_USED: Final = {(0, 0), (0, 5), (0, 24), (1, 0), (1, 3)}

DERIVED_SQL: Final = (
    "SELECT wallet_id, branch, child_index, address_canonical, used, created_at "
    "FROM derived_addresses ORDER BY wallet_id, branch, child_index"
)

#: Enough refusals for an address that both hosts give up on it, whatever the retry count:
#: the queue is per address, so a failover would otherwise reach a fresh answer.
OUTAGE: Final = 12


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as factory:
        yield factory


@pytest.fixture
def derivation_spy(monkeypatch: pytest.MonkeyPatch) -> DerivationSpy:
    return spy_on_derivation(monkeypatch)


# --------------------------------------------------------------------------------------
# Planting, running, reading back
# --------------------------------------------------------------------------------------


async def insert_key_wallet(
    session: AsyncSession, *, user_id: int, key: str = SCAN_KEY, archived: bool = False
) -> int:
    return await harness_insert_key_wallet(
        session, user_id=user_id, created_at=FIRST_SYNC, key=key, archived=archived
    )


@dataclass
class Planted:
    user_id: int
    key_wallet: int
    address_wallet: int | None = None
    kaspa_wallet: int | None = None


async def plant(
    factory: async_sessionmaker[AsyncSession],
    *,
    with_address_wallet: bool = False,
    with_kaspa: bool = False,
) -> Planted:
    """The extended-key wallet first, so it has the lowest id; the others after it."""
    async with factory() as session:
        user_id = await insert_user(session)
        planted = Planted(user_id, await insert_key_wallet(session, user_id=user_id))
        if with_address_wallet:
            planted.address_wallet = await insert_wallet(
                session, user_id=user_id, chain_key=ChainKey.BITCOIN, address=BIP173_TESTNET_P2WPKH
            )
        if with_kaspa:
            planted.kaspa_wallet = await insert_wallet(
                session, user_id=user_id, chain_key=ChainKey.KASPA, address=KASPA_TESTNET_V0
            )
    return planted


def stepping_monotonic() -> Callable[[], int]:
    ticks = iter(range(0, 10**9, 7))
    return lambda: next(ticks)


async def run_sync(
    factory: async_sessionmaker[AsyncSession],
    providers: Mapping[ChainKey, ChainProvider],
    *,
    at: datetime = FIRST_SYNC,
) -> SyncRunSummary:
    async with factory() as session:
        service = build_balance_sync_service(
            session,
            provider_for=lambda chain_key: providers[ChainKey(chain_key)],
            clock=lambda: at,
            monotonic=stepping_monotonic(),
        )
        return await service.sync(SyncTrigger.MANUAL)


async def sync_over_book(
    factory: async_sessionmaker[AsyncSession],
    book: AddressBook,
    *,
    at: datetime = FIRST_SYNC,
    kaspa: StubChainProvider | None = None,
    network: str = "testnet",
) -> SyncRunSummary:
    """One sync with the real Esplora provider over `book` for Bitcoin."""
    provider, client = provider_over(book, network=network, max_attempts=1)
    providers: dict[ChainKey, ChainProvider] = {ChainKey.BITCOIN: provider}
    if kaspa is not None:
        providers[ChainKey.KASPA] = kaspa
    async with client:
        return await run_sync(factory, providers, at=at)


async def derived_rows(factory: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with factory() as session:
        result = await session.execute(text(DERIVED_SQL))
        return [dict(row) for row in result.mappings().all()]


def positions_of(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    return [(row["branch"], row["child_index"]) for row in rows]


def used_positions(rows: Sequence[Mapping[str, Any]]) -> set[tuple[int, int]]:
    return {(row["branch"], row["child_index"]) for row in rows if row["used"]}


def snapshots_of(rows: Sequence[Mapping[str, Any]], wallet_id: int | None) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if row["wallet_id"] == wallet_id]


def outage() -> list[httpx.Response]:
    return [httpx.Response(503) for _ in range(OUTAGE)]


def without_receive_46(book: AddressBook) -> dict[str, Holding]:
    """Criterion 3's holdings minus receive 46, so a newly used 44 does not cascade to it."""
    return {address: held for address, held in book.holdings.items() if address != SCAN_RECEIVE[46]}


async def balances_view(factory: async_sessionmaker[AsyncSession], user_id: int) -> Any:
    async with factory() as session:
        return await build_balance_service(session).current_balances(
            Principal(user_id=user_id, username="owner", session_id=1),
            quote_currency=QuoteCurrency.EUR,
        )


# --------------------------------------------------------------------------------------
# A scripted scanner, for the sum's edges and a scan that breaks its contract
# --------------------------------------------------------------------------------------


class ScriptedScanner(StubChainProvider):
    """A Bitcoin stub that also scans: it returns the scan it was given, and records calls."""

    def __init__(self, addresses: Sequence[ScannedAddress]) -> None:
        super().__init__(ChainKey.BITCOIN)
        self.result = ExtendedKeyScan(addresses=tuple(addresses), decimals=8)
        self.scans: list[tuple[str, tuple[KnownDerivedAddress, ...]]] = []

    async def scan_extended_key(
        self, key: str, known: Sequence[KnownDerivedAddress]
    ) -> ExtendedKeyScan:
        self.scans.append((key, tuple(known)))
        return self.result


_SCRIPTED_CONFORMS: ExtendedKeyScanner = ScriptedScanner(())
"""`mypy --strict` deciding that the scripted scanner has the protocol's signature."""


def scanned(
    branch: int,
    index: int,
    *,
    used: bool = False,
    confirmed: int = 0,
    pending: int | None = 0,
) -> ScannedAddress:
    return ScannedAddress(
        branch=branch,
        index=index,
        address=f"position-{branch}-{index}",
        used=used,
        confirmed=confirmed,
        pending=pending,
    )


def gap_scan(*overrides: ScannedAddress) -> list[ScannedAddress]:
    """Twenty unused positions per branch, with the given ones replaced or added."""
    addresses = {
        (branch, index): scanned(branch, index)
        for branch in (RECEIVE_BRANCH, CHANGE_BRANCH)
        for index in range(20)
    }
    for address in overrides:
        addresses[(address.branch, address.index)] = address
    return [addresses[position] for position in sorted(addresses)]


# --------------------------------------------------------------------------------------
# Criterion 4: rescans are incremental (R6)
# --------------------------------------------------------------------------------------


async def test_the_first_sync_persists_every_scanned_address_with_its_used_flag(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant(sessions)
    book = criterion_three_book()

    summary = await sync_over_book(sessions, book)

    assert summary.status is SyncRunStatus.SUCCESS
    rows = await derived_rows(sessions)
    assert positions_of(rows) == CRITERION_THREE_POSITIONS
    assert used_positions(rows) == CRITERION_THREE_USED
    assert {row["wallet_id"] for row in rows} == {planted.key_wallet}
    assert {row["created_at"] for row in rows} == {sqlite_timestamp(FIRST_SYNC)}
    # The address stored at each position is the one read for it, and BIP-84's at the
    # positions the vectors pin.
    assert [row["address_canonical"] for row in rows] == book.asked
    pinned = 0
    for row in rows:
        vectors = SCAN_RECEIVE if row["branch"] == RECEIVE_BRANCH else SCAN_CHANGE
        if row["child_index"] in vectors:
            assert row["address_canonical"] == vectors[row["child_index"]]
            pinned += 1
    assert pinned == 6 + 4, "receive 0, 1, 5, 23, 24, 44 and change 0, 3, 22, 23"


async def test_the_first_sync_derives_each_reached_index_once(
    sessions: async_sessionmaker[AsyncSession], derivation_spy: DerivationSpy
) -> None:
    await plant(sessions)

    await sync_over_book(sessions, criterion_three_book())

    assert derivation_spy.children(RECEIVE_BRANCH) == list(range(45))
    assert derivation_spy.children(CHANGE_BRANCH) == list(range(24))


async def test_a_second_sync_with_no_new_use_derives_nothing(
    sessions: async_sessionmaker[AsyncSession], derivation_spy: DerivationSpy
) -> None:
    await plant(sessions)
    await sync_over_book(sessions, criterion_three_book())
    first = await derived_rows(sessions)
    derivation_spy.calls.clear()
    book = criterion_three_book()

    await sync_over_book(sessions, book, at=SECOND_SYNC)

    assert derivation_spy.children(RECEIVE_BRANCH) == []
    assert derivation_spy.children(CHANGE_BRANCH) == []
    assert derivation_spy.accounts() == [RECEIVE_BRANCH, CHANGE_BRANCH], "the two branch keys"
    assert await derived_rows(sessions) == first, "nothing added, nothing changed"
    assert book.asked == [row["address_canonical"] for row in first], "every one read again"


async def test_a_newly_used_index_derives_only_up_to_twenty_past_it(
    sessions: async_sessionmaker[AsyncSession], derivation_spy: DerivationSpy
) -> None:
    """Criterion 4: receive 44 becomes used, and 45 to 64 are derived -- only they."""
    planted = await plant(sessions)
    await sync_over_book(sessions, criterion_three_book())
    derivation_spy.calls.clear()
    book = criterion_three_book()
    book.holdings = {**without_receive_46(book), SCAN_RECEIVE[44]: Holding(chain_tx=1)}

    await sync_over_book(sessions, book, at=SECOND_SYNC)

    assert derivation_spy.children(RECEIVE_BRANCH) == list(range(45, 65))
    assert derivation_spy.children(CHANGE_BRANCH) == []
    rows = await derived_rows(sessions)
    assert len(rows) == 69 + 20
    new = [row for row in rows if row["created_at"] == sqlite_timestamp(SECOND_SYNC)]
    assert positions_of(new) == [(RECEIVE_BRANCH, index) for index in range(45, 65)]
    assert new[0]["address_canonical"] == SCAN_RECEIVE[45]
    assert new[-1]["address_canonical"] == SCAN_RECEIVE[64]
    assert used_positions(rows) == CRITERION_THREE_USED | {(RECEIVE_BRANCH, 44)}
    (row_44,) = [row for row in rows if (row["branch"], row["child_index"]) == (0, 44)]
    assert row_44["created_at"] == sqlite_timestamp(FIRST_SYNC), "updated, not re-inserted"
    assert {row["wallet_id"] for row in rows} == {planted.key_wallet}


async def test_a_persisted_used_flag_is_never_unset(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await plant(sessions)
    await sync_over_book(sessions, criterion_three_book())

    # Every address now reads as never used, which an Esplora instance can do after a reorg
    # or while it rebuilds its index.
    await sync_over_book(sessions, AddressBook(), at=SECOND_SYNC)

    rows = await derived_rows(sessions)
    assert used_positions(rows) == CRITERION_THREE_USED
    assert len(rows) == 69


async def test_an_interrupted_scan_persists_nothing_and_the_next_one_starts_over(
    sessions: async_sessionmaker[AsyncSession], derivation_spy: DerivationSpy
) -> None:
    """R6: a scan that fails half-way writes no address, no flag and no snapshot."""
    planted = await plant(sessions)
    broken = criterion_three_book()
    broken.failures = {SCAN_RECEIVE[23]: outage()}

    summary = await sync_over_book(sessions, broken)

    (chain,) = summary.chains
    assert chain.status is SyncRunStatus.FAILED
    assert chain.error_kind is SyncErrorKind.UNAVAILABLE
    assert SCAN_RECEIVE[5] in broken.asked, "it failed half-way, not before it began"
    assert await derived_rows(sessions) == []
    assert snapshots_of(await snapshots(sessions), planted.key_wallet) == []

    derivation_spy.calls.clear()
    await sync_over_book(sessions, criterion_three_book(), at=SECOND_SYNC)

    assert derivation_spy.children(RECEIVE_BRANCH) == list(range(45))
    assert positions_of(await derived_rows(sessions)) == CRITERION_THREE_POSITIONS


async def test_a_rescan_that_fails_leaves_the_persisted_set_as_it_was(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant(sessions)
    await sync_over_book(sessions, criterion_three_book())
    before = await derived_rows(sessions)
    broken = criterion_three_book()
    broken.holdings = {**without_receive_46(broken), SCAN_RECEIVE[44]: Holding(chain_tx=1)}
    broken.failures = {SCAN_RECEIVE[64]: outage()}

    summary = await sync_over_book(sessions, broken, at=SECOND_SYNC)

    assert summary.chains[0].status is SyncRunStatus.FAILED
    assert SCAN_RECEIVE[45] in broken.asked, "the window had already grown when it failed"
    assert await derived_rows(sessions) == before, "neither the new rows nor the flag on 44"
    assert len(snapshots_of(await snapshots(sessions), planted.key_wallet)) == 1


# --------------------------------------------------------------------------------------
# Criterion 5: the wallet's balance is the sum of its derived addresses (R7)
# --------------------------------------------------------------------------------------


async def test_the_snapshot_is_the_sum_over_every_scanned_address(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant(sessions)

    await sync_over_book(sessions, criterion_three_book())

    (snapshot,) = snapshots_of(await snapshots(sessions), planted.key_wallet)
    assert snapshot["confirmed"] == CRITERION_THREE_CONFIRMED == 158_000
    assert snapshot["pending"] == CRITERION_THREE_PENDING == 3_000
    assert snapshot["decimals"] == 8
    assert snapshot["observed_at"] == sqlite_timestamp(FIRST_SYNC)


async def test_a_rescan_sums_every_persisted_address_again(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Funds arriving at an old address inside the window count on the next sync (R5)."""
    planted = await plant(sessions)
    await sync_over_book(sessions, criterion_three_book())
    book = criterion_three_book()
    book.holdings = {**book.holdings, SCAN_RECEIVE[23]: Holding(funded=2_000, chain_tx=1)}

    await sync_over_book(sessions, book, at=SECOND_SYNC)

    latest = snapshots_of(await snapshots(sessions), planted.key_wallet)[-1]
    assert latest["confirmed"] == CRITERION_THREE_CONFIRMED + 2_000
    assert latest["observed_at"] == sqlite_timestamp(SECOND_SYNC)


async def test_pending_is_none_when_any_scanned_address_reported_none(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant(sessions)
    book = criterion_three_book()
    book.holdings = {
        **book.holdings,
        SCAN_RECEIVE[1]: Holding(funded=500, chain_tx=1, mempool_funded=None),
    }

    await sync_over_book(sessions, book)

    (snapshot,) = snapshots_of(await snapshots(sessions), planted.key_wallet)
    assert snapshot["pending"] is None, "one address that could not say is not a zero"
    assert snapshot["confirmed"] == CRITERION_THREE_CONFIRMED + 500


async def test_a_key_with_nothing_used_is_a_real_zero_not_unread(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant(sessions)

    summary = await sync_over_book(sessions, AddressBook())

    (snapshot,) = snapshots_of(await snapshots(sessions), planted.key_wallet)
    assert (snapshot["confirmed"], snapshot["pending"]) == (0, 0)
    (chain,) = summary.chains
    assert (chain.status, chain.wallets_read) == (SyncRunStatus.SUCCESS, 1)
    assert len(await derived_rows(sessions)) == 40
    view = await balances_view(sessions, planted.user_id)
    assert view.unread == (), "read, and found empty"


async def test_pending_is_a_signed_sum(sessions: async_sessionmaker[AsyncSession]) -> None:
    planted = await plant(sessions)
    scanner = ScriptedScanner(
        gap_scan(
            scanned(0, 0, used=True, confirmed=10_000, pending=3_000),
            scanned(1, 0, used=True, confirmed=4_000, pending=-5_000),
        )
    )

    await run_sync(sessions, {ChainKey.BITCOIN: scanner})

    (snapshot,) = snapshots_of(await snapshots(sessions), planted.key_wallet)
    assert snapshot["confirmed"] == 14_000
    assert snapshot["pending"] == -2_000


async def test_the_dashboard_reads_the_sum_as_the_wallets_balance(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant(sessions)
    await sync_over_book(sessions, criterion_three_book())

    view = await balances_view(sessions, planted.user_id)

    (balance,) = [entry for entry in view.wallets if entry.wallet_id == planted.key_wallet]
    assert balance.confirmed == CRITERION_THREE_CONFIRMED
    assert balance.pending == CRITERION_THREE_PENDING
    assert balance.decimals == 8
    assert balance.quantity == Decimal("0.00158")
    assert view.unread == ()


# --------------------------------------------------------------------------------------
# One wallet, however many addresses: the run's counts, and the chain's other wallets
# --------------------------------------------------------------------------------------


async def test_an_extended_key_is_one_wallet_in_every_count(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant(sessions, with_address_wallet=True, with_kaspa=True)
    kaspa = StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: 7})
    book = criterion_three_book()
    book.holdings = {**book.holdings, BIP173_TESTNET_P2WPKH: Holding(funded=900, chain_tx=1)}

    summary = await sync_over_book(sessions, book, kaspa=kaspa)

    assert summary.status is SyncRunStatus.SUCCESS
    (run,) = await sync_runs(sessions)
    assert (run["wallets_total"], run["wallets_succeeded"], run["wallets_failed"]) == (3, 3, 0)
    chains = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert chains["bitcoin"]["wallets_read"] == 2
    assert chains["kaspa"]["wallets_read"] == 1
    rows = await snapshots(sessions)
    assert snapshots_of(rows, planted.key_wallet)[0]["confirmed"] == CRITERION_THREE_CONFIRMED
    assert snapshots_of(rows, planted.address_wallet)[0]["confirmed"] == 900
    # The address wallets are read once, as a batch, before any key is scanned.
    assert book.asked[0] == BIP173_TESTNET_P2WPKH
    assert book.asked.count(BIP173_TESTNET_P2WPKH) == 1
    assert len(book.asked) == 1 + 69


async def test_an_archived_extended_key_wallet_is_not_scanned(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        user_id = await insert_user(session)
        await insert_key_wallet(session, user_id=user_id, archived=True)
    book = AddressBook()

    summary = await sync_over_book(sessions, book)

    assert book.asked == []
    assert await derived_rows(sessions) == []
    assert summary.chains == ()


async def test_two_key_wallets_are_scanned_in_turn_and_persisted_apart(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        user_id = await insert_user(session)
        vpub = await insert_key_wallet(session, user_id=user_id, key=SCAN_KEY)
        tpub = await insert_key_wallet(session, user_id=user_id, key=BIP32_TV1_M)
    book = AddressBook()

    summary = await sync_over_book(sessions, book)

    assert summary.chains[0].wallets_read == 2
    rows = await derived_rows(sessions)
    assert [row["wallet_id"] for row in rows] == [vpub] * 40 + [tpub] * 40
    assert len(book.asked) == 80
    # One wallet's scan, then the other's: never interleaved.
    assert all(address.startswith("tb1q") for address in book.asked[:40])
    assert all(address[0] in "mn" for address in book.asked[40:])
    assert len(await snapshots(sessions)) == 2


# --------------------------------------------------------------------------------------
# Registered through the service: the scan reads the canonical form (review finding S1)
# --------------------------------------------------------------------------------------


async def register(
    factory: async_sessionmaker[AsyncSession], user_id: int, key: str
) -> tuple[int, str]:
    """`create_wallet` as the API calls it; the wallet id and what it stored as canonical."""
    async with factory() as session:
        view = await build_wallet_service(session, clock=lambda: FIRST_SYNC).create_wallet(
            Principal(user_id=user_id, username="owner", session_id=1),
            chain_key="bitcoin",
            address=key,
        )
    async with factory() as session:
        result = await session.execute(
            text("SELECT address_canonical FROM wallets WHERE id = :id"), {"id": view.id}
        )
        canonical: str = result.scalar_one()
    return view.id, canonical


async def test_a_key_registered_through_the_service_scans_from_its_canonical_form(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """S1 stores the depth-0 form, and the provider is handed that: the scan must still work.

    Same addresses at every pinned position, the same sum -- the position bytes S1 zeroes
    play no part in derivation.
    """
    async with sessions() as session:
        user_id = await insert_user(session)
    wallet_id, canonical = await register(sessions, user_id, SCAN_KEY)
    assert canonical == depth_zero(SCAN_KEY) != SCAN_KEY
    book = criterion_three_book()

    summary = await sync_over_book(sessions, book)

    assert summary.status is SyncRunStatus.SUCCESS
    rows = await derived_rows(sessions)
    assert positions_of(rows) == CRITERION_THREE_POSITIONS
    assert used_positions(rows) == CRITERION_THREE_USED
    for row in rows:
        vectors = SCAN_RECEIVE if row["branch"] == RECEIVE_BRANCH else SCAN_CHANGE
        if row["child_index"] in vectors:
            assert row["address_canonical"] == vectors[row["child_index"]]
    (snapshot,) = snapshots_of(await snapshots(sessions), wallet_id)
    assert snapshot["confirmed"] == CRITERION_THREE_CONFIRMED


async def test_one_key_under_two_versions_is_two_wallets_that_derive_apart(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The version is kept in the canonical form: P2WPKH and P2PKH addresses, both read."""
    async with sessions() as session:
        user_id = await insert_user(session)
    as_p2pkh = reserialised(SCAN_KEY, version=TPUB_VERSION)
    p2wpkh_wallet, _ = await register(sessions, user_id, SCAN_KEY)
    p2pkh_wallet, _ = await register(sessions, user_id, as_p2pkh)
    book = AddressBook()

    summary = await sync_over_book(sessions, book)

    assert summary.status is SyncRunStatus.SUCCESS
    assert summary.chains[0].wallets_read == 2
    rows = await derived_rows(sessions)
    p2wpkh = [row["address_canonical"] for row in rows if row["wallet_id"] == p2wpkh_wallet]
    p2pkh = [row["address_canonical"] for row in rows if row["wallet_id"] == p2pkh_wallet]
    assert len(p2wpkh) == len(p2pkh) == 40
    assert p2wpkh[0] == SCAN_RECEIVE[0]
    assert all(address.startswith("tb1q") for address in p2wpkh)
    assert all(address[0] in "mn" for address in p2pkh)
    assert not set(p2wpkh) & set(p2pkh)


# --------------------------------------------------------------------------------------
# Failures, each one the chain's and nobody else's
# --------------------------------------------------------------------------------------


async def test_a_provider_that_cannot_scan_fails_the_chain_as_internal(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Unreachable through the domain, and loud anyway: a skipped wallet would read as zero."""
    planted = await plant(sessions, with_address_wallet=True, with_kaspa=True)
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: 5})
    kaspa = StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: 7})
    assert not isinstance(bitcoin, ExtendedKeyScanner)

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin, ChainKey.KASPA: kaspa})

    assert summary.status is SyncRunStatus.PARTIAL
    chains = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert chains["bitcoin"]["status"] == "failed"
    assert chains["bitcoin"]["error_kind"] == "internal"
    assert chains["bitcoin"]["detail"] == "ExtendedKeysUnsupportedError"
    assert chains["kaspa"]["status"] == "success"
    rows = await snapshots(sessions)
    assert snapshots_of(rows, planted.key_wallet) == []
    assert snapshots_of(rows, planted.address_wallet) == [], "the chain fails as a whole"
    assert len(snapshots_of(rows, planted.kaspa_wallet)) == 1


def test_the_unsupported_error_is_a_fixed_sentence() -> None:
    error = ExtendedKeysUnsupportedError()

    assert isinstance(error, TypeError)
    assert str(error) == "This chain's provider cannot scan an extended public key."


@pytest.mark.parametrize(
    ("addresses", "unread"),
    [
        pytest.param(gap_scan()[1:], 1, id="a persisted address left unread"),
        pytest.param(gap_scan()[:-3], 3, id="three left unread"),
        pytest.param([*gap_scan(), scanned(0, 0)], 0, id="a position read twice"),
        pytest.param([*gap_scan(), scanned(0, 25), scanned(0, 25)], 0, id="a new one twice"),
    ],
)
async def test_a_scan_that_breaks_its_contract_fails_the_chain_as_response(
    sessions: async_sessionmaker[AsyncSession], addresses: list[ScannedAddress], unread: int
) -> None:
    planted = await plant(sessions)
    await run_sync(sessions, {ChainKey.BITCOIN: ScriptedScanner(gap_scan())})
    before = await derived_rows(sessions)
    assert len(before) == 40

    summary = await run_sync(
        sessions, {ChainKey.BITCOIN: ScriptedScanner(addresses)}, at=SECOND_SYNC
    )

    (chain,) = summary.chains
    assert chain.status is SyncRunStatus.FAILED
    assert chain.error_kind is SyncErrorKind.RESPONSE
    assert chain.detail == (
        f"The extended-key scan left {unread} persisted address(es) unread, or read one "
        "position twice, so its sum cannot be trusted."
    )
    assert await derived_rows(sessions) == before
    assert len(snapshots_of(await snapshots(sessions), planted.key_wallet)) == 1


async def test_a_scan_that_keeps_its_contract_and_reaches_further_is_accepted(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The control for the test above: every persisted position once, plus a new one."""
    await plant(sessions)
    await run_sync(sessions, {ChainKey.BITCOIN: ScriptedScanner(gap_scan())})

    summary = await run_sync(
        sessions,
        {ChainKey.BITCOIN: ScriptedScanner(gap_scan(scanned(0, 20, used=True)))},
        at=SECOND_SYNC,
    )

    assert summary.chains[0].status is SyncRunStatus.SUCCESS
    rows = await derived_rows(sessions)
    assert len(rows) == 41
    assert used_positions(rows) == {(0, 20)}


async def test_the_scan_is_handed_the_persisted_addresses_as_they_are_on_disk(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await plant(sessions)
    first = ScriptedScanner(gap_scan(scanned(0, 3, used=True, confirmed=1)))
    await run_sync(sessions, {ChainKey.BITCOIN: first})
    assert first.scans == [(SCAN_KEY, ())]
    second = ScriptedScanner(first.result.addresses)

    await run_sync(sessions, {ChainKey.BITCOIN: second}, at=SECOND_SYNC)

    ((key, known),) = second.scans
    assert key == SCAN_KEY
    assert [(entry.branch, entry.index, entry.address, entry.used) for entry in known] == [
        (address.branch, address.index, address.address, address.used)
        for address in first.result.addresses
    ]


async def test_a_sum_that_cannot_be_stored_writes_neither_the_snapshot_nor_the_addresses(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """R6: the derived rows share the snapshot's commit, so a refused write takes both."""
    planted = await plant(sessions, with_kaspa=True)
    scanner = ScriptedScanner(gap_scan(scanned(0, 0, used=True, confirmed=2**63)))
    kaspa = StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: 7})

    summary = await run_sync(sessions, {ChainKey.BITCOIN: scanner, ChainKey.KASPA: kaspa})

    chains = {outcome.chain_key: outcome for outcome in summary.chains}
    assert chains["bitcoin"].status is SyncRunStatus.FAILED
    assert chains["bitcoin"].error_kind is SyncErrorKind.INTERNAL
    assert chains["kaspa"].status is SyncRunStatus.SUCCESS
    assert await derived_rows(sessions) == []
    rows = await snapshots(sessions)
    assert snapshots_of(rows, planted.key_wallet) == []
    assert len(snapshots_of(rows, planted.kaspa_wallet)) == 1


async def test_a_key_on_the_wrong_network_is_an_address_rejection(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A `vpub` registered, then read by an instance configured for mainnet: the owner's to fix."""
    planted = await plant(sessions)
    book = AddressBook()

    summary = await sync_over_book(sessions, book, network="mainnet")

    (chain,) = summary.chains
    assert chain.error_kind is SyncErrorKind.ADDRESS_REJECTED
    assert chain.detail is not None
    assert chain.detail.startswith(
        "1 wallet(s) on this chain were not read: an address or an extended key was refused "
        "before it was read (wrong_network). "
    )
    assert SCAN_KEY[4:-4] not in chain.detail
    assert book.asked == []
    assert snapshots_of(await snapshots(sessions), planted.key_wallet) == []


# --------------------------------------------------------------------------------------
# The log: one line per wallet, counts only
# --------------------------------------------------------------------------------------


def scan_events(logs: Sequence[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    return (event for event in logs if event["event"] == "balance_sync_extended_key_scanned")


@contextmanager
def captured_at_debug() -> Iterator[list[MutableMapping[str, Any]]]:
    """`capture_logs` with DEBUG let through, and the configuration put back afterwards.

    `capture_logs` swaps the processors and keeps the wrapper class, which filters at the
    level `configure_logging` set -- so a DEBUG line never reaches it, and "logged at DEBUG"
    would be indistinguishable from "not logged at all".
    """
    with preserved_logging():
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
        with capture_logs() as logs:
            yield logs


async def test_the_scan_is_logged_once_per_wallet_with_counts_and_nothing_else(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant(sessions)

    with capture_logs() as logs:
        await sync_over_book(sessions, criterion_three_book())

    (event,) = scan_events(logs)
    assert event == {
        "event": "balance_sync_extended_key_scanned",
        "log_level": "info",
        "wallet_id": planted.key_wallet,
        "derived_scanned": 69,
        "derived_new": 69,
        "derived_newly_used": 0,
    }


async def test_a_scan_that_adds_no_address_logs_at_debug(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A newly used address inside the window is a change, but not a new row: DEBUG."""
    await plant(sessions)
    await sync_over_book(sessions, criterion_three_book())
    book = criterion_three_book()
    book.holdings = {**book.holdings, SCAN_RECEIVE[23]: Holding(funded=1, chain_tx=1)}

    with captured_at_debug() as logs:
        await sync_over_book(sessions, book, at=SECOND_SYNC)

    (event,) = scan_events(logs)
    assert event["log_level"] == "debug"
    assert (event["derived_scanned"], event["derived_new"]) == (69, 0)
    assert event["derived_newly_used"] == 1


async def test_a_newly_used_index_logs_its_new_rows_at_info(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await plant(sessions)
    await sync_over_book(sessions, criterion_three_book())
    book = criterion_three_book()
    book.holdings = {**without_receive_46(book), SCAN_RECEIVE[44]: Holding(chain_tx=1)}

    with capture_logs() as logs:
        await sync_over_book(sessions, book, at=SECOND_SYNC)

    (event,) = scan_events(logs)
    assert event["log_level"] == "info"
    assert (event["derived_scanned"], event["derived_new"]) == (89, 20)
    assert event["derived_newly_used"] == 1


async def test_no_log_event_carries_the_key_or_a_derived_address(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await plant(sessions)
    book = criterion_three_book()

    with captured_at_debug() as logs:
        await sync_over_book(sessions, book)
        await sync_over_book(sessions, criterion_three_book(), at=SECOND_SYNC)

    assert len(list(scan_events(logs))) == 2, "the scans did log"
    rendered = repr(logs)
    assert SCAN_KEY[4:-4] not in rendered
    for address in set(book.asked):
        assert address not in rendered


async def test_a_failed_scan_logs_no_scan_line(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Written after the commit: an INFO line is a statement about what is on disk."""
    await plant(sessions)
    broken = AddressBook(failures={SCAN_CHANGE[3]: outage()})

    with captured_at_debug() as logs:
        await sync_over_book(sessions, broken)

    assert list(scan_events(logs)) == []
    assert "balance_sync_chain_failed" in [event["event"] for event in logs]
