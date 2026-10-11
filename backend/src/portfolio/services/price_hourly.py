"""Storing hourly closes in `price_hourly`, for the change over 24 hours and 7 days (spec 041).

Run by the hourly price timer, after the refresh. Each run asks the source for every
committed hourly close it serves -- Kraken keeps 30 days of them -- and stores only the hours
not stored yet: a committed candle is final (R1). So the first run fills 30 days, every later
run adds the hour that just ended, and a run missed for a while catches up on the next.

**One pair failing does not stop the other**, as in the daily backfill: each pair is asked,
written and committed on its own, and a vendor failure is a line in the report. Like the
refresh, this module is never imported by a router: only `main.py` builds it, so the
`prices-are-never-fetched-in-a-request` import contract holds for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from portfolio.providers.errors import ProviderError
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.price_hourly import PriceHourlyRepository
from portfolio.services.price_backfill import UNSUPPORTED_PAIR, FailedPair
from portfolio.services.prices import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.providers.prices.base import HourlyCloseSource

__all__ = ["HourlyPriceService", "HourlyReport", "StoredHours", "build_hourly_price_service"]


@dataclass(frozen=True, slots=True)
class StoredHours:
    """One pair the source answered, and how many hours were new."""

    asset_symbol: str
    quote_currency: str
    hours: int


@dataclass(frozen=True, slots=True)
class HourlyReport:
    """What one run stored, and what failed, both sorted by pair."""

    stored: tuple[StoredHours, ...]
    failed: tuple[FailedPair, ...]


class HourlyPriceService:
    """Asks an `HourlyCloseSource` for every pair it serves and stores the new hours.

    Owns its transactions: one commit per pair.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        hourly: PriceHourlyRepository,
        assets: AssetRepository,
        source: HourlyCloseSource,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._hourly = hourly
        self._assets = assets
        self._source = source
        self._clock = clock

    async def record(self) -> HourlyReport:
        """Store every new committed hourly close of every pair the source serves.

        A pair whose asset has no row in `assets` is reported as failed without a request.
        """
        recorded_at = self._clock()
        assets = await self._assets.by_symbol()
        stored: list[StoredHours] = []
        failed: list[FailedPair] = []
        for asset_symbol, quote_currency in sorted(self._source.pairs):
            asset = assets.get(asset_symbol)
            if asset is None:
                failed.append(
                    FailedPair(
                        asset_symbol, quote_currency, UNSUPPORTED_PAIR, source=self._source.name
                    )
                )
                continue
            try:
                closes = await self._source.hourly_closes((asset_symbol, quote_currency))
            except ProviderError as exc:
                failed.append(
                    FailedPair(
                        asset_symbol, quote_currency, type(exc).__name__, source=self._source.name
                    )
                )
                continue
            hours = await self._hourly.record_new(
                asset_id=asset.id,
                quote_currency=quote_currency,
                closes=[(close.hour, close.close) for close in closes],
                source=self._source.name,
                recorded_at=recorded_at,
            )
            await self._session.commit()
            stored.append(StoredHours(asset_symbol, quote_currency, hours))
        return HourlyReport(stored=tuple(stored), failed=tuple(failed))


def build_hourly_price_service(
    session: AsyncSession,
    *,
    source: HourlyCloseSource,
    clock: Callable[[], datetime] = utc_now,
) -> HourlyPriceService:
    """Assemble the service over one session and a built source.

    `source` has no default, for the reason `build_price_backfill_service`'s has none.
    """
    return HourlyPriceService(
        session=session,
        hourly=PriceHourlyRepository(session),
        assets=AssetRepository(session),
        source=source,
        clock=clock,
    )
