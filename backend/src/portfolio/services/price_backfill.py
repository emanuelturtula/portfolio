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

## Further back than the recent source keeps (spec 038, R8)

Kraken keeps 720 days. An optional `older` source -- Coinbase Exchange's candles in
production -- is asked, after the recent source, for the days **before the earliest close
stored for the pair** and not before its own `earliest_day`, and they are written as `close`
under its own name. So it is asked once for the whole decade, and once that range is filled
the earliest stored close is the older source's first day and a run asks it for nothing.

A pair with **no close stored at all** has nothing to extend backwards from: the recent
source has never answered it, so where its window begins is unknown, and asking the older
source up to today would only be overwritten. That is a failed line, `NoRecentClose`,
rather than a silent skip -- the history is short, and the operator should be able to see
why. It costs no request, and the next run that finds a close stored fills the gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from portfolio.providers.errors import ProviderError
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.price_history import CLOSE, PriceHistoryRepository
from portfolio.services.prices import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.providers.prices.base import (
        DailyClose,
        DailyCloseSource,
        HistoricalCloseSource,
        PricePair,
    )

__all__ = [
    "NO_RECENT_CLOSE",
    "UNSUPPORTED_PAIR",
    "BackfillReport",
    "BackfilledPair",
    "FailedPair",
    "PriceBackfillService",
    "build_price_backfill_service",
]

UNSUPPORTED_PAIR: Final = "UnsupportedPair"
"""A pair the recent source does not serve, or whose asset has no row: nothing was asked."""

NO_RECENT_CLOSE: Final = "NoRecentClose"
"""A pair the older source could extend, with no close stored to extend it back from."""


@dataclass(frozen=True, slots=True)
class BackfilledPair:
    """One pair a source answered: how many days it stored, from when to when, and who said.

    `first_day` and `last_day` are `None` when the source answered with no committed close.
    `source` is the source's `name`, as written to `price_history.source`: a pair can have one
    line from the recent source and one from the older source in the same report.
    """

    asset_symbol: str
    quote_currency: str
    days: int
    first_day: date | None
    last_day: date | None
    source: str


@dataclass(frozen=True, slots=True)
class FailedPair:
    """One pair a source could not answer, and the class name of why. Never the message.

    `error` is an exception's class name, or `UnsupportedPair` or `NoRecentClose` when
    nothing was asked. `source` is the name of the source the line is about.
    """

    asset_symbol: str
    quote_currency: str
    error: str
    source: str


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """What one backfill did, both tuples sorted by pair.

    Within one pair, the recent source's line comes before the older source's.
    """

    backfilled: tuple[BackfilledPair, ...]
    failed: tuple[FailedPair, ...]


class PriceBackfillService:
    """Asks a `DailyCloseSource` for every pair it serves and stores the closes.

    Then, when it has one, asks an `older` source for the days before the earliest stored
    close of each pair it serves (spec 038, R8).

    Owns its transactions: one commit per pair and source, so a failure on the second pair
    leaves the first pair's closes stored.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        history: PriceHistoryRepository,
        assets: AssetRepository,
        source: DailyCloseSource,
        older: HistoricalCloseSource | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._history = history
        self._assets = assets
        self._source = source
        self._older = older
        self._clock = clock

    async def backfill(self, pairs: Sequence[PricePair] | None = None) -> BackfillReport:
        """Store every committed daily close for these pairs, or for all the source serves.

        A pair the source does not serve, or whose asset has no row in `assets`, is reported
        as failed without a request: nothing could have answered it.

        Then the `older` source, for the requested pairs it serves whose asset has a row (one
        without was reported just above): the days from its `earliest_day` to the day before
        the earliest stored close. A pair whose range is already filled asks nothing and has
        no line; one with no close stored at all is `NoRecentClose`, also without a request.
        The older source runs whether or not the recent one answered today, because the
        earliest stored close does not depend on today's answer.
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
                failed.append(
                    FailedPair(
                        asset_symbol, quote_currency, UNSUPPORTED_PAIR, source=self._source.name
                    )
                )
                continue
            try:
                closes = await self._source.daily_closes(pair)
            except ProviderError as exc:
                failed.append(
                    FailedPair(
                        asset_symbol,
                        quote_currency,
                        type(exc).__name__,
                        source=self._source.name,
                    )
                )
                continue
            backfilled.append(
                await self._store(asset.id, pair, closes, self._source.name, recorded_at)
            )

        older = self._older
        if older is not None:
            for pair in requested:
                asset = assets.get(pair[0])
                if pair not in older.pairs or asset is None:
                    continue
                line = await self._extend_backwards(older, asset.id, pair, recorded_at)
                if isinstance(line, FailedPair):
                    failed.append(line)
                elif line is not None:
                    backfilled.append(line)

        return BackfillReport(
            backfilled=tuple(sorted(backfilled, key=_pair_of)),
            failed=tuple(sorted(failed, key=_pair_of)),
        )

    async def _extend_backwards(
        self,
        older: HistoricalCloseSource,
        asset_id: int,
        pair: PricePair,
        recorded_at: datetime,
    ) -> BackfilledPair | FailedPair | None:
        """Ask `older` for the days before the pair's earliest stored close, and store them.

        `None` when there is nothing left to ask for, which is every run after the first
        that succeeded: the earliest stored close is then the older source's first day.
        """
        asset_symbol, quote_currency = pair
        earliest = await self._history.earliest_close_day(asset_id, quote_currency)
        if earliest is None:
            return FailedPair(asset_symbol, quote_currency, NO_RECENT_CLOSE, source=older.name)
        first_day = older.earliest_day
        last_day = earliest - timedelta(days=1)
        if first_day > last_day:
            return None
        try:
            closes = await older.daily_closes_between(pair, first_day, last_day)
        except ProviderError as exc:
            return FailedPair(asset_symbol, quote_currency, type(exc).__name__, source=older.name)
        return await self._store(asset_id, pair, closes, older.name, recorded_at)

    async def _store(
        self,
        asset_id: int,
        pair: PricePair,
        closes: Sequence[DailyClose],
        source: str,
        recorded_at: datetime,
    ) -> BackfilledPair:
        """Write each close as `close` under `source`, commit, and say what was stored."""
        asset_symbol, quote_currency = pair
        for close in closes:
            await self._history.record(
                asset_id=asset_id,
                quote_currency=quote_currency,
                day=close.day,
                amount=close.close,
                basis=CLOSE,
                source=source,
                recorded_at=recorded_at,
            )
        await self._session.commit()
        return BackfilledPair(
            asset_symbol=asset_symbol,
            quote_currency=quote_currency,
            days=len(closes),
            first_day=closes[0].day if closes else None,
            last_day=closes[-1].day if closes else None,
            source=source,
        )


def _pair_of(line: BackfilledPair | FailedPair) -> PricePair:
    """A report line's pair, the sort key. `sorted` is stable, so within a pair the line
    written first -- the recent source's -- stays first."""
    return (line.asset_symbol, line.quote_currency)


def build_price_backfill_service(
    session: AsyncSession,
    *,
    source: DailyCloseSource,
    older: HistoricalCloseSource | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> PriceBackfillService:
    """Assemble the backfill over one session and built sources.

    `source` has no default, for the reason `build_price_refresh_service`'s `sources` has none:
    building one needs the process-wide HTTP client, whose lifetime is the caller's. `older`
    defaults to none, which is spec 037's backfill exactly.
    """
    return PriceBackfillService(
        session=session,
        history=PriceHistoryRepository(session),
        assets=AssetRepository(session),
        source=source,
        older=older,
        clock=clock,
    )
