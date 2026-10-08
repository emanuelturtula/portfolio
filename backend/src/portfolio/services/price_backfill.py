"""The price backfill: every daily close a vendor still keeps, into `price_history` (spec 037).

The hourly refresh records one `observed` price per day from the day it starts running. The
backfill fills in what came before and corrects what came since: each run asks the source for
every committed daily close it serves -- Kraken keeps 720 days -- and writes them as `close`,
which replaces an `observed` price for the same day and is never replaced by one (R2).

**Idempotent by construction.** A close for a day is the same number on every run, and the
row for it is replaced rather than added, so running the backfill twice, or daily for a year,
leaves one row per day. That is what lets the timer run it every day without bookkeeping:
today's run writes yesterday's close, and every earlier day is rewritten with what it already
held.

**One pair failing does not stop the other.** Each pair is asked, written and committed on
its own, and a vendor failure is a line in the report rather than an exception. Like the
refresh, this module is never imported by a router: the `prices-are-never-fetched-in-a-request`
import contract holds for it because only `main.py` and `cli.py` build it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from portfolio.providers.errors import ProviderError
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.price_history import CLOSE, PriceHistoryRepository
from portfolio.services.prices import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.providers.prices.base import DailyCloseSource, PricePair

__all__ = [
    "BackfillReport",
    "BackfilledPair",
    "FailedPair",
    "PriceBackfillService",
    "build_price_backfill_service",
]


@dataclass(frozen=True, slots=True)
class BackfilledPair:
    """One pair the source answered: how many days it stored, and from when to when.

    `first_day` and `last_day` are `None` when the source answered with no committed close.
    """

    asset_symbol: str
    quote_currency: str
    days: int
    first_day: date | None
    last_day: date | None


@dataclass(frozen=True, slots=True)
class FailedPair:
    """One pair the source could not answer, and the class name of why. Never the message."""

    asset_symbol: str
    quote_currency: str
    error: str


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """What one backfill did, both tuples sorted by pair."""

    backfilled: tuple[BackfilledPair, ...]
    failed: tuple[FailedPair, ...]


class PriceBackfillService:
    """Asks a `DailyCloseSource` for every pair it serves and stores the closes.

    Owns its transactions: one commit per pair, so a failure on the second pair leaves the
    first pair's closes stored.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        history: PriceHistoryRepository,
        assets: AssetRepository,
        source: DailyCloseSource,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._history = history
        self._assets = assets
        self._source = source
        self._clock = clock

    async def backfill(self, pairs: Sequence[PricePair] | None = None) -> BackfillReport:
        """Store every committed daily close for these pairs, or for all the source serves.

        A pair the source does not serve, or whose asset has no row in `assets`, is reported
        as failed without a request: nothing could have answered it.
        """
        requested = sorted(pairs if pairs is not None else self._source.pairs)
        recorded_at = self._clock()
        assets = await self._assets.by_symbol()
        backfilled: list[BackfilledPair] = []
        failed: list[FailedPair] = []
        for pair in requested:
            asset_symbol, quote_currency = pair
            asset = assets.get(asset_symbol)
            if pair not in self._source.pairs or asset is None:
                failed.append(FailedPair(asset_symbol, quote_currency, "UnsupportedPair"))
                continue
            try:
                closes = await self._source.daily_closes(pair)
            except ProviderError as exc:
                failed.append(FailedPair(asset_symbol, quote_currency, type(exc).__name__))
                continue
            for close in closes:
                await self._history.record(
                    asset_id=asset.id,
                    quote_currency=quote_currency,
                    day=close.day,
                    amount=close.close,
                    basis=CLOSE,
                    source=self._source.name,
                    recorded_at=recorded_at,
                )
            await self._session.commit()
            backfilled.append(
                BackfilledPair(
                    asset_symbol=asset_symbol,
                    quote_currency=quote_currency,
                    days=len(closes),
                    first_day=closes[0].day if closes else None,
                    last_day=closes[-1].day if closes else None,
                )
            )
        return BackfillReport(backfilled=tuple(backfilled), failed=tuple(failed))


def build_price_backfill_service(
    session: AsyncSession,
    *,
    source: DailyCloseSource,
    clock: Callable[[], datetime] = utc_now,
) -> PriceBackfillService:
    """Assemble the backfill over one session and a built source.

    `source` has no default, for the reason `build_price_refresh_service`'s `sources` has none:
    building one needs the process-wide HTTP client, whose lifetime is the caller's.
    """
    return PriceBackfillService(
        session=session,
        history=PriceHistoryRepository(session),
        assets=AssetRepository(session),
        source=source,
        clock=clock,
    )
