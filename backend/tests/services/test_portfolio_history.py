"""`PortfolioHistoryService` over a migrated SQLite file (spec 037).

The snapshots and the prices are planted as the balance sync and the price history leave
them, and the clock is named, so "today" is a literal. What is asserted is the service's
part: which snapshot closes a day, which wallets count, and how the stored prices reach the
domain. The arithmetic itself is `tests/domain/test_portfolio_history.py`'s.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import text

from portfolio.domain.chains import ChainKey
from portfolio.domain.portfolio_history import DayValue, HistoryRange, WalletDay
from portfolio.repositories.price_history import CLOSE, OBSERVED, PriceHistoryRepository
from portfolio.services.portfolio_history import build_portfolio_history_service
from portfolio.services.wallets import WalletNotFoundError
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WSH, KASPA_TESTNET_V0
from tests.balance_harness import insert_user, insert_wallet, sqlite_timestamp
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW: Final = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
TODAY: Final = date(2026, 10, 8)
YESTERDAY: Final = date(2026, 10, 7)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


async def owner(factory: async_sessionmaker[AsyncSession], username: str = "owner") -> int:
    async with factory() as session:
        return await insert_user(session, username)


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


async def reading(
    factory: async_sessionmaker[AsyncSession], wallet_id: int, confirmed: int, at: datetime
) -> None:
    """One snapshot under a finished run of its own, as the balance sync leaves it."""
    async with factory() as session:
        run_id = await session.scalar(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
                "wallets_total, wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', 'success', :at, :at, 1, 1, 1, 0) RETURNING id"
            ),
            {"at": sqlite_timestamp(at)},
        )
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
                "at": sqlite_timestamp(at),
            },
        )
        await session.commit()


async def price(
    factory: async_sessionmaker[AsyncSession],
    symbol: str,
    day: date,
    amount: str,
    *,
    currency: str = "USD",
    basis: str = CLOSE,
) -> None:
    async with factory() as session:
        asset_id = await session.scalar(
            text("SELECT id FROM assets WHERE symbol = :symbol"), {"symbol": symbol}
        )
        await PriceHistoryRepository(session).record(
            asset_id=int(asset_id),
            quote_currency=currency,
            day=day,
            amount=Decimal(amount),
            basis=basis,
            source="kraken",
            recorded_at=NOW,
        )
        await session.commit()


def at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


async def test_the_last_snapshot_of_a_day_closes_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three readings yesterday: the 23:00 one is the day's balance, 0.3 BTC at 60000."""
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await reading(factory, btc, 10_000_000, at(YESTERDAY, 1))
    await reading(factory, btc, 30_000_000, at(YESTERDAY, 23))
    await reading(factory, btc, 20_000_000, at(YESTERDAY, 12))
    await price(factory, "BTC", YESTERDAY, "60000")
    await price(factory, "BTC", TODAY, "61000", basis=OBSERVED)

    async with factory() as session:
        history = await build_portfolio_history_service(session, clock=lambda: NOW).portfolio(
            user_id, HistoryRange.ALL
        )

    assert history.history_range is HistoryRange.ALL
    assert history.points == (
        DayValue(YESTERDAY, Decimal(18000)),
        DayValue(TODAY, Decimal(18300)),
    )


async def test_a_fixed_range_has_a_point_for_every_day_with_gaps_as_none(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await reading(factory, btc, 10_000_000, at(YESTERDAY, 9))
    await price(factory, "BTC", YESTERDAY, "60000")

    async with factory() as session:
        history = await build_portfolio_history_service(session, clock=lambda: NOW).portfolio(
            user_id, HistoryRange.DAYS_30
        )

    assert len(history.points) == 30
    assert history.points[-2] == DayValue(YESTERDAY, Decimal(6000))
    # Today has the reading but no price yet; every day before yesterday has no reading.
    assert history.points[-1] == DayValue(TODAY, None)
    assert all(point.value is None for point in history.points[:-2])


async def test_only_the_owners_active_wallets_count(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    stranger = await owner(factory, "stranger")
    mine = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    archived = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH, archived=True)
    theirs = await wallet(factory, stranger, ChainKey.KASPA, KASPA_TESTNET_V0)
    for wallet_id in (mine, archived, theirs):
        await reading(factory, wallet_id, 100_000_000, at(TODAY, 8))
    await price(factory, "BTC", TODAY, "60000", basis=OBSERVED)

    async with factory() as session:
        history = await build_portfolio_history_service(session, clock=lambda: NOW).portfolio(
            user_id, HistoryRange.ALL
        )

    assert history.points == (DayValue(TODAY, Decimal(60000)),)


async def test_only_usd_prices_value_the_history(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A EUR price for the day does not stand in for the missing USD one."""
    user_id = await owner(factory)
    btc = await wallet(factory, user_id, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await reading(factory, btc, 100_000_000, at(TODAY, 8))
    await price(factory, "BTC", TODAY, "55000", currency="EUR", basis=OBSERVED)

    async with factory() as session:
        history = await build_portfolio_history_service(session, clock=lambda: NOW).portfolio(
            user_id, HistoryRange.ALL
        )

    assert history.points == (DayValue(TODAY, None),)


async def test_nothing_registered_is_today_alone_and_unknown(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)

    async with factory() as session:
        history = await build_portfolio_history_service(session, clock=lambda: NOW).portfolio(
            user_id, HistoryRange.ALL
        )

    assert history.points == (DayValue(TODAY, None),)


async def test_one_wallet_with_its_quantity_archived_included(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    kas = await wallet(factory, user_id, ChainKey.KASPA, KASPA_TESTNET_V0, archived=True)
    await reading(factory, kas, 100_000_000_000, at(YESTERDAY, 10))
    await price(factory, "KAS", YESTERDAY, "0.05")

    async with factory() as session:
        history = await build_portfolio_history_service(session, clock=lambda: NOW).wallet(
            user_id, kas, HistoryRange.ALL
        )

    assert (history.wallet_id, history.asset, history.history_range) == (
        kas,
        "KAS",
        HistoryRange.ALL,
    )
    assert history.points == (
        WalletDay(YESTERDAY, Decimal(1000), Decimal(50)),
        WalletDay(TODAY, Decimal(1000), None),
    )


async def test_a_wallet_that_is_not_the_owners_is_not_found(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await owner(factory)
    stranger = await owner(factory, "stranger")
    theirs = await wallet(factory, stranger, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)

    async with factory() as session:
        service = build_portfolio_history_service(session, clock=lambda: NOW)
        with pytest.raises(WalletNotFoundError):
            await service.wallet(user_id, theirs, HistoryRange.DAYS_30)
        with pytest.raises(WalletNotFoundError):
            await service.wallet(user_id, 999_999, HistoryRange.DAYS_30)
