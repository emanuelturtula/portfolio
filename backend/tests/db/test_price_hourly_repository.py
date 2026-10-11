"""Spec 041's `price_hourly` repository: new hours only, and the price at an instant.

Every database here is a real file under `tmp_path`, built by running the migrations, for the
reason `test_prices_repository.py` gives. Nothing here sums, orders or compares money in SQL:
rows are chosen by `hour`, an instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import select, text

from portfolio.db.engine import create_session_factory
from portfolio.db.models import Asset
from portfolio.repositories.price_hourly import HOUR, PriceHourlyRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

USD: Final = "USD"
NOON: Final = datetime(2026, 10, 9, 12, tzinfo=UTC)
RECORDED_AT: Final = datetime(2026, 10, 10, 0, 5, tzinfo=UTC)
TWO_HOURS: Final = timedelta(hours=2)


@pytest.fixture
def factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(migrated_engine)


@pytest.fixture
async def session(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as opened:
        yield opened


async def asset_id(session: AsyncSession, symbol: str) -> int:
    found = (await session.scalars(select(Asset).where(Asset.symbol == symbol))).one()
    return found.id


async def record(session: AsyncSession, symbol: str, *closes: tuple[datetime, str]) -> int:
    stored = await PriceHourlyRepository(session).record_new(
        asset_id=await asset_id(session, symbol),
        quote_currency=USD,
        closes=[(hour, Decimal(amount)) for hour, amount in closes],
        source="kraken",
        recorded_at=RECORDED_AT,
    )
    await session.commit()
    return stored


async def test_only_new_hours_are_stored_and_a_stored_hour_is_never_rewritten(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R1: a committed candle is final. The second call adds one hour and changes none."""
    first = await record(session, "BTC", (NOON, "60000"), (NOON + HOUR, "60100"))
    second = await record(
        session, "BTC", (NOON, "1"), (NOON + HOUR, "2"), (NOON + 2 * HOUR, "60200")
    )

    async with factory() as reader:
        rows = (
            await reader.execute(text("SELECT hour, amount FROM price_hourly ORDER BY hour"))
        ).all()

    assert (first, second) == (2, 1)
    assert [amount for _hour, amount in rows] == [
        "60000.000000000000",
        "60100.000000000000",
        "60200.000000000000",
    ]


async def test_nothing_to_store_is_nothing_stored(session: AsyncSession) -> None:
    assert await record(session, "BTC") == 0


async def test_a_float_close_is_refused_before_anything_is_written(session: AsyncSession) -> None:
    with pytest.raises(TypeError):
        await PriceHourlyRepository(session).record_new(
            asset_id=await asset_id(session, "BTC"),
            quote_currency=USD,
            closes=[(NOON, 1.5)],  # type: ignore[list-item]
            source="kraken",
            recorded_at=RECORDED_AT,
        )


async def test_the_price_at_an_instant_is_the_close_of_the_latest_hour_ended_by_then(
    session: AsyncSession,
) -> None:
    """R2: the 12:00 candle ends at 13:00, so it prices 13:00 and 14:59, not 12:59."""
    await record(session, "BTC", (NOON - HOUR, "59900"), (NOON, "60000"), (NOON + HOUR, "60100"))
    await record(session, "KAS", (NOON, "0.05"))
    btc, kas = await asset_id(session, "BTC"), await asset_id(session, "KAS")
    repository = PriceHourlyRepository(session)

    async def at(instant: datetime) -> dict[int, Decimal]:
        return await repository.prices_at(
            asset_ids=[btc, kas], quote_currency=USD, at=instant, max_age=TWO_HOURS
        )

    assert await at(NOON + timedelta(minutes=59)) == {btc: Decimal(59900)}
    assert await at(NOON + HOUR) == {btc: Decimal(60000), kas: Decimal("0.05")}
    assert await at(NOON + 2 * HOUR + timedelta(minutes=30)) == {
        btc: Decimal(60100),
        kas: Decimal("0.05"),
    }


async def test_an_hour_that_ended_max_age_or_more_before_the_instant_prices_nothing(
    session: AsyncSession,
) -> None:
    await record(session, "BTC", (NOON, "60000"))
    btc = await asset_id(session, "BTC")
    repository = PriceHourlyRepository(session)

    found = await repository.prices_at(
        asset_ids=[btc], quote_currency=USD, at=NOON + HOUR + TWO_HOURS, max_age=TWO_HOURS
    )

    assert found == {}
    assert (
        await repository.prices_at(asset_ids=[], quote_currency=USD, at=NOON, max_age=TWO_HOURS)
        == {}
    )
