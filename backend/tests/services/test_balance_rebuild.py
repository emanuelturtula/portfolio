"""`BalanceRebuildService` over a migrated SQLite file, with fake history readers (spec 038).

The providers are fakes that answer `address_history` from a dictionary, so what is asserted
is the service's part: which addresses a wallet reads, what is stored, what is kept, and how
each outcome is reported. The walk itself is `tests/domain/test_balance_history.py`'s; the
real readers are `tests/providers/chains/`'s. Then the stored rows are read back through
`PortfolioHistoryService`, which is the reason they exist (R7).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import text

from portfolio.domain.addresses import AddressInvalidError, AddressRejection
from portfolio.domain.chains import ChainKey
from portfolio.domain.portfolio_history import DayValue, HistoryRange
from portfolio.providers.base import (
    AddressHistory,
    ChainCapabilities,
    HistoryIncomplete,
    TransactionHistoryReader,
    TxEffect,
)
from portfolio.providers.errors import ProviderUnavailableError, UnknownChainError
from portfolio.repositories.price_history import CLOSE, PriceHistoryRepository
from portfolio.repositories.reconstructed_balances import ReconstructedBalanceRepository
from portfolio.services.balance_rebuild import (
    RebuildOutcome,
    RebuildReport,
    WalletRebuild,
    build_balance_rebuild_service,
)
from portfolio.services.portfolio_history import build_portfolio_history_service
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    BIP350_TESTNET_V1,
    KASPA_TESTNET_V0,
)
from tests.balance_harness import insert_user, insert_wallet, sqlite_timestamp
from tests.extended_key_vectors import BIP84_ACCOUNT_VPUB
from tests.providers.fakes import FakeChainProvider
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.base import ChainProvider

NOW: Final = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
TODAY: Final = date(2026, 10, 8)


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 9, tzinfo=UTC)


def complete(address: str, *effects: tuple[date, int]) -> AddressHistory:
    """A history that proved itself: its effects sum to its balance."""
    return AddressHistory(
        address=address,
        balance=sum(delta for _, delta in effects),
        decimals=8,
        effects=tuple(TxEffect(at(day), delta) for day, delta in effects),
        incomplete=None,
    )


class FakeHistoryReader:
    """A chain provider that also reads histories, from a dictionary. Records every ask."""

    def __init__(
        self,
        histories: Mapping[str, AddressHistory | Exception],
        *,
        chain: ChainKey = ChainKey.BITCOIN,
    ) -> None:
        self._histories = dict(histories)
        self._chain = chain
        self.asked: list[str] = []

    @property
    def capabilities(self) -> ChainCapabilities:
        return ChainCapabilities(chain_key=self._chain, decimals=8, max_addresses_per_call=1)

    async def address_history(self, address: str) -> AddressHistory:
        self.asked.append(address)
        answer = self._histories[address]
        if isinstance(answer, Exception):
            raise answer
        return answer


_CONFORMS: TransactionHistoryReader = FakeHistoryReader({})


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


async def owner(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return await insert_user(session)


async def wallet(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    chain: ChainKey,
    address: str,
    *,
    archived: bool = False,
) -> int:
    async with factory() as session:
        return await insert_wallet(
            session, user_id=user_id, chain_key=chain, address=address, archived=archived
        )


async def key_wallet(
    factory: async_sessionmaker[AsyncSession], user_id: int, derived: Mapping[str, bool]
) -> int:
    """An extended-key wallet and the addresses it has derived, each used or not."""
    async with factory() as session:
        wallet_id = int(
            await session.scalar(
                text(
                    "INSERT INTO wallets (user_id, chain_key, address_canonical, "
                    "address_display, kind, created_at, updated_at) VALUES (:user, 'bitcoin', "
                    ":key, :key, 'extended_key', :at, :at) RETURNING id"
                ),
                {"user": user_id, "key": BIP84_ACCOUNT_VPUB, "at": sqlite_timestamp(NOW)},
            )
        )
        for index, (address, used) in enumerate(derived.items()):
            await session.execute(
                text(
                    "INSERT INTO derived_addresses (wallet_id, branch, child_index, "
                    "address_canonical, used, created_at) VALUES (:wallet, 0, :index, "
                    ":address, :used, :at)"
                ),
                {
                    "wallet": wallet_id,
                    "index": index,
                    "address": address,
                    "used": int(used),
                    "at": sqlite_timestamp(NOW),
                },
            )
        await session.commit()
    return wallet_id


async def rebuild(
    factory: async_sessionmaker[AsyncSession], provider_for: Callable[[str], ChainProvider]
) -> RebuildReport:
    async with factory() as session:
        service = build_balance_rebuild_service(
            session, provider_for=provider_for, clock=lambda: NOW
        )
        return await service.rebuild()


async def stored(
    factory: async_sessionmaker[AsyncSession], wallet_id: int
) -> list[tuple[date, int]]:
    async with factory() as session:
        rows = await ReconstructedBalanceRepository(session).for_wallets([wallet_id])
    return [(row.day, row.confirmed) for row in rows.get(wallet_id, [])]


def only(reader: FakeHistoryReader) -> Callable[[str], ChainProvider]:
    def provider_for(chain_key: str) -> ChainProvider:
        del chain_key
        return reader  # type: ignore[return-value]

    return provider_for


# --------------------------------------------------------------------------------------
# What a rebuild stores
# --------------------------------------------------------------------------------------


async def test_a_complete_history_stores_one_row_per_day_to_today(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    first = TODAY - timedelta(days=3)
    reader = FakeHistoryReader(
        {BIP173_TESTNET_P2WPKH: complete(BIP173_TESTNET_P2WPKH, (first, 100), (TODAY, -40))}
    )

    report = await rebuild(factory, only(reader))

    assert report == RebuildReport(
        wallets=(WalletRebuild(btc, "bitcoin", RebuildOutcome.REBUILT, 4, first, None),)
    )
    assert await stored(factory, btc) == [
        (first, 100),
        (first + timedelta(days=1), 100),
        (first + timedelta(days=2), 100),
        (TODAY, 60),
    ]
    assert reader.asked == [BIP173_TESTNET_P2WPKH]


async def test_a_second_rebuild_replaces_the_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await rebuild(
        factory,
        only(
            FakeHistoryReader(
                {
                    BIP173_TESTNET_P2WPKH: complete(
                        BIP173_TESTNET_P2WPKH, (TODAY - timedelta(days=5), 10)
                    )
                }
            )
        ),
    )

    await rebuild(
        factory,
        only(
            FakeHistoryReader({BIP173_TESTNET_P2WPKH: complete(BIP173_TESTNET_P2WPKH, (TODAY, 3))})
        ),
    )

    assert await stored(factory, btc) == [(TODAY, 3)]


async def test_an_extended_key_reads_its_used_addresses_and_sums_them(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R5: a transfer between two of its own addresses nets to zero on its day."""
    user_id = await owner(factory)
    key = await key_wallet(
        factory,
        user_id,
        {BIP173_TESTNET_P2WPKH: True, BIP173_TESTNET_P2WSH: True, BIP350_TESTNET_V1: False},
    )
    yesterday = TODAY - timedelta(days=1)
    reader = FakeHistoryReader(
        {
            BIP173_TESTNET_P2WPKH: complete(BIP173_TESTNET_P2WPKH, (yesterday, 100), (TODAY, -30)),
            BIP173_TESTNET_P2WSH: complete(BIP173_TESTNET_P2WSH, (TODAY, 30)),
        }
    )

    report = await rebuild(factory, only(reader))

    assert report.wallets[0].outcome is RebuildOutcome.REBUILT
    assert reader.asked == [BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WSH]
    assert await stored(factory, key) == [(yesterday, 100), (TODAY, 100)]


async def test_an_extended_key_with_nothing_used_stores_nothing_and_succeeds(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    key = await key_wallet(factory, user_id, {BIP350_TESTNET_V1: False})
    reader = FakeHistoryReader({})

    report = await rebuild(factory, only(reader))

    assert report.wallets == (WalletRebuild(key, "bitcoin", RebuildOutcome.REBUILT, 0, None, None),)
    assert reader.asked == []
    assert await stored(factory, key) == []


async def test_only_active_wallets_are_rebuilt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH, archived=True)
    reader = FakeHistoryReader({})

    report = await rebuild(factory, only(reader))

    assert report.wallets == ()
    assert reader.asked == []


# --------------------------------------------------------------------------------------
# What a failed rebuild keeps, by R6
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(HistoryIncomplete))
async def test_an_unproven_history_keeps_the_rows_it_had(
    factory: async_sessionmaker[AsyncSession], reason: HistoryIncomplete
) -> None:
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await rebuild(
        factory,
        only(
            FakeHistoryReader({BIP173_TESTNET_P2WPKH: complete(BIP173_TESTNET_P2WPKH, (TODAY, 5))})
        ),
    )
    unproven = AddressHistory(BIP173_TESTNET_P2WPKH, 9, 8, (), incomplete=reason)

    report = await rebuild(factory, only(FakeHistoryReader({BIP173_TESTNET_P2WPKH: unproven})))

    assert report.wallets == (
        WalletRebuild(btc, "bitcoin", RebuildOutcome.INCOMPLETE, 0, None, reason.value),
    )
    assert await stored(factory, btc) == [(TODAY, 5)]


async def test_a_walk_that_does_not_reach_zero_is_incomplete(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A history that says it is complete and still does not reach zero: the walk refuses it."""
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    lying = AddressHistory(
        BIP173_TESTNET_P2WPKH, 100, 8, (TxEffect(at(TODAY), 60),), incomplete=None
    )

    report = await rebuild(factory, only(FakeHistoryReader({BIP173_TESTNET_P2WPKH: lying})))

    assert report.wallets[0].outcome is RebuildOutcome.INCOMPLETE
    assert report.wallets[0].reason == "does_not_reach_zero"
    assert await stored(factory, btc) == []


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (ProviderUnavailableError("the chain did not answer"), "ProviderUnavailableError"),
        (AddressInvalidError(AddressRejection.BAD_CHECKSUM), "AddressInvalidError"),
    ],
)
async def test_a_provider_failure_is_reported_by_class_and_stops_nothing_else(
    factory: async_sessionmaker[AsyncSession], error: Exception, name: str
) -> None:
    user_id = await owner(factory)
    failing = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    fine = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    reader = FakeHistoryReader(
        {
            BIP173_TESTNET_P2WPKH: error,
            BIP173_TESTNET_P2WSH: complete(BIP173_TESTNET_P2WSH, (TODAY, 1)),
        }
    )

    report = await rebuild(factory, only(reader))

    assert report.wallets == (
        WalletRebuild(failing, "bitcoin", RebuildOutcome.FAILED, 0, None, name),
        WalletRebuild(fine, "bitcoin", RebuildOutcome.REBUILT, 1, TODAY, None),
    )


async def test_an_unknown_chain_fails_that_wallet(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    kas = await wallet(factory, user_id, ChainKey.KASPA, KASPA_TESTNET_V0)

    def provider_for(chain_key: str) -> ChainProvider:
        raise UnknownChainError(chain_key, ())

    report = await rebuild(factory, provider_for)

    assert report.wallets == (
        WalletRebuild(kas, "kaspa", RebuildOutcome.FAILED, 0, None, "UnknownChainError"),
    )


async def test_a_provider_that_cannot_read_histories_is_unsupported(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)

    report = await rebuild(factory, lambda _chain: FakeChainProvider())

    assert report.wallets == (
        WalletRebuild(btc, "bitcoin", RebuildOutcome.UNSUPPORTED, 0, None, None),
    )


# --------------------------------------------------------------------------------------
# How the value history reads it, by R7
# --------------------------------------------------------------------------------------


async def test_rebuilt_days_extend_the_value_history_before_the_first_snapshot(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rebuilt 0.5 BTC two days ago and 0.7 yesterday; the snapshot says 0.6 today.

    Yesterday's rebuilt row is used because it is before the first snapshot; today's is not,
    because from the first snapshot on what the chain reported that day wins.
    """
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    two_ago, yesterday = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
    await rebuild(
        factory,
        only(
            FakeHistoryReader(
                {
                    BIP173_TESTNET_P2WPKH: complete(
                        BIP173_TESTNET_P2WPKH,
                        (two_ago, 50_000_000),
                        (yesterday, 20_000_000),
                        (TODAY, 5_000_000),
                    )
                }
            )
        ),
    )
    async with factory() as session:
        run_id = await session.scalar(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
                "wallets_total, wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', 'success', :at, :at, 1, 1, 1, 0) RETURNING id"
            ),
            {"at": sqlite_timestamp(NOW)},
        )
        await session.execute(
            text(
                "INSERT INTO balance_snapshots (wallet_id, sync_run_id, confirmed, pending, "
                "decimals, observed_at) VALUES (:wallet, :run, 60000000, NULL, 8, :at)"
            ),
            {"wallet": btc, "run": run_id, "at": sqlite_timestamp(NOW)},
        )
        asset_id = int(await session.scalar(text("SELECT id FROM assets WHERE symbol = 'BTC'")))
        for day in (two_ago, yesterday, TODAY):
            await PriceHistoryRepository(session).record(
                asset_id=asset_id,
                quote_currency="USD",
                day=day,
                amount=Decimal(100),
                basis=CLOSE,
                source="kraken",
                recorded_at=NOW,
            )
        await session.commit()

    async with factory() as session:
        history = await build_portfolio_history_service(session, clock=lambda: NOW).portfolio(
            user_id, HistoryRange.ALL
        )

    assert history.points == (
        DayValue(two_ago, Decimal(50)),
        DayValue(yesterday, Decimal(70)),
        DayValue(TODAY, Decimal(60)),
    )


async def test_a_wallet_never_snapshotted_is_its_rebuilt_days(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await rebuild(
        factory,
        only(
            FakeHistoryReader(
                {BIP173_TESTNET_P2WPKH: complete(BIP173_TESTNET_P2WPKH, (TODAY, 100_000_000))}
            )
        ),
    )

    async with factory() as session:
        history = await build_portfolio_history_service(session, clock=lambda: NOW).portfolio(
            user_id, HistoryRange.ALL
        )

    # Rebuilt, so known; unpriced today, so a gap rather than a zero.
    assert history.points == (DayValue(TODAY, None),)


async def test_the_newest_rebuild_instant_is_the_timers_last_run(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        assert await ReconstructedBalanceRepository(session).latest_rebuilt_at() is None
        assert await ReconstructedBalanceRepository(session).for_wallets([]) == {}
    user_id = await owner(factory)
    await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await rebuild(
        factory,
        only(
            FakeHistoryReader({BIP173_TESTNET_P2WPKH: complete(BIP173_TESTNET_P2WPKH, (TODAY, 1))})
        ),
    )

    async with factory() as session:
        assert await ReconstructedBalanceRepository(session).latest_rebuilt_at() == NOW
