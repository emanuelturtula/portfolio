"""Spec 041's hourly closes: every new committed hour a source keeps, into `price_hourly`.

Driven with a fake `HourlyCloseSource` over a migrated SQLite file, so the assertions are on
rows. What is pinned: new hours are stored with the source's name and the run's one clock
read; a second run stores only what is new; one pair failing does not stop the other, and is
reported by class name only; a pair whose asset has no row costs no request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from portfolio.db.models import PriceHourly
from portfolio.main import _report_hourly_prices
from portfolio.providers.errors import ProviderUnavailableError
from portfolio.providers.prices.base import BTC, KAS, USD, HourlyClose
from portfolio.services.price_backfill import UNSUPPORTED_PAIR, FailedPair
from portfolio.services.price_hourly import (
    HourlyReport,
    StoredHours,
    build_hourly_price_service,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.prices.base import HourlyCloseSource, PricePair

RUN_AT: Final = datetime(2026, 10, 10, 0, 5, tzinfo=UTC)
NOON: Final = datetime(2026, 10, 9, 12, tzinfo=UTC)
HOUR: Final = timedelta(hours=1)
VENDOR_PROSE: Final = "the vendor said something only a log line should never keep"


@dataclass
class FakeHourly:
    """An `HourlyCloseSource` answering from a table, or raising per pair."""

    name: str = "a-vendor"
    pairs: frozenset[PricePair] = field(default_factory=lambda: frozenset({(BTC, USD), (KAS, USD)}))
    answers: dict[PricePair, tuple[HourlyClose, ...]] = field(default_factory=dict)
    raises: dict[PricePair, BaseException] = field(default_factory=dict)
    asked: list[PricePair] = field(default_factory=list)

    async def hourly_closes(self, pair: PricePair) -> Sequence[HourlyClose]:
        self.asked.append(pair)
        error = self.raises.get(pair)
        if error is not None:
            raise error
        return self.answers.get(pair, ())


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path, name="hourly.db") as made:
        yield made


async def run(factory: async_sessionmaker[AsyncSession], source: HourlyCloseSource) -> HourlyReport:
    async with factory() as session:
        return await build_hourly_price_service(
            session, source=source, clock=lambda: RUN_AT
        ).record()


async def rows_in(factory: async_sessionmaker[AsyncSession]) -> list[PriceHourly]:
    async with factory() as reader:
        return list(await reader.scalars(select(PriceHourly).order_by(PriceHourly.id)))


async def test_new_hours_are_stored_and_a_second_run_stores_only_what_is_new(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeHourly(
        answers={
            (BTC, USD): (
                HourlyClose(NOON, Decimal(60000)),
                HourlyClose(NOON + HOUR, Decimal(60100)),
            ),
            (KAS, USD): (HourlyClose(NOON, Decimal("0.05")),),
        }
    )

    first = await run(factory, source)
    source.answers[(BTC, USD)] += (HourlyClose(NOON + 2 * HOUR, Decimal(60200)),)
    second = await run(factory, source)

    assert first == HourlyReport(
        stored=(StoredHours(BTC, USD, 2), StoredHours(KAS, USD, 1)), failed=()
    )
    assert second == HourlyReport(
        stored=(StoredHours(BTC, USD, 1), StoredHours(KAS, USD, 0)), failed=()
    )
    rows = await rows_in(factory)
    assert [(row.hour, row.amount) for row in rows if row.amount > 1] == [
        (NOON, Decimal(60000)),
        (NOON + HOUR, Decimal(60100)),
        (NOON + 2 * HOUR, Decimal(60200)),
    ]
    assert {row.source for row in rows} == {"a-vendor"}
    assert {row.recorded_at for row in rows} == {RUN_AT}


async def test_one_pair_failing_does_not_stop_the_other(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeHourly(
        answers={(KAS, USD): (HourlyClose(NOON, Decimal("0.05")),)},
        raises={(BTC, USD): ProviderUnavailableError(VENDOR_PROSE)},
    )

    report = await run(factory, source)

    assert report.stored == (StoredHours(KAS, USD, 1),)
    assert report.failed == (FailedPair(BTC, USD, "ProviderUnavailableError", source="a-vendor"),)
    assert VENDOR_PROSE not in repr(report)
    assert len(await rows_in(factory)) == 1


async def test_a_pair_with_no_asset_row_costs_no_request(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeHourly(pairs=frozenset({("XRP", USD)}))

    report = await run(factory, source)

    assert report.failed == (FailedPair("XRP", USD, UNSUPPORTED_PAIR, source="a-vendor"),)
    assert source.asked == []


def test_a_run_with_a_failed_pair_is_logged_as_a_warning_naming_pairs_and_never_amounts() -> None:
    """What `main` logs after each hourly tick: counts and pairs, never a price."""
    report = HourlyReport(
        stored=(StoredHours(KAS, USD, 3),),
        failed=(FailedPair(BTC, USD, "ProviderUnavailableError", source="kraken"),),
    )

    with capture_logs() as logs:
        _report_hourly_prices(report)

    assert logs == [
        {
            "event": "price_hourly_incomplete",
            "log_level": "warning",
            "hours": 3,
            "failed": ("BTC/USD via kraken",),
        }
    ]
