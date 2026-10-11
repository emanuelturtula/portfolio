"""Reads and writes of the `price_hourly` table (spec 041).

Queries and nothing else, as in `repositories/price_history.py`: no clock, no policy beyond
the one rule the table keeps -- a committed hour is final, so a stored hour is never
rewritten (R1) -- and no commit.

## Nothing here aggregates, orders or compares money

Rows are ordered and compared by `hour`, an instant, never by `amount`, a `TEXT` money column
that SQLite would compare as a float.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from portfolio.db.models import PriceHourly
from portfolio.domain.money import require_amount

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["HOUR", "PriceHourlyRepository"]

HOUR: Final = timedelta(hours=1)
"""How long a candle lasts: its close is the price at its `hour` plus this."""


class PriceHourlyRepository:
    """Every query this application makes against `price_hourly`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_new(
        self,
        *,
        asset_id: int,
        quote_currency: str,
        closes: Sequence[tuple[datetime, Decimal]],
        source: str,
        recorded_at: datetime,
    ) -> int:
        """Store each `(hour, close)` whose hour is not stored yet, and say how many were.

        An hour already stored is left as it is: a committed candle does not change, so the
        hourly timer asks for 30 days every hour and writes only the hour that is new.
        Flushes and does not commit.

        Raises:
            TypeError: a close is not a `Decimal`.
            ValueError: a close is not finite.
        """
        if not closes:
            return 0
        for _hour, amount in closes:
            require_amount(amount, subject="price_hourly.amount")
        stored = set(
            await self._session.scalars(
                select(PriceHourly.hour).where(
                    PriceHourly.asset_id == asset_id,
                    PriceHourly.quote_currency == quote_currency,
                    PriceHourly.hour >= min(hour for hour, _amount in closes),
                )
            )
        )
        new = [(hour, amount) for hour, amount in closes if hour not in stored]
        self._session.add_all(
            PriceHourly(
                asset_id=asset_id,
                quote_currency=quote_currency,
                hour=hour,
                amount=amount,
                source=source,
                recorded_at=recorded_at,
            )
            for hour, amount in new
        )
        await self._session.flush()
        return len(new)

    async def prices_at(
        self,
        *,
        asset_ids: Sequence[int],
        quote_currency: str,
        at: datetime,
        max_age: timedelta,
    ) -> dict[int, Decimal]:
        """Each asset's price at the instant `at`, keyed by `asset_id` (spec 041, R2).

        The close of the latest hour that ended at or before `at`, and only one that ended
        less than `max_age` before it. An asset with no such hour is absent, never zero.
        """
        if not asset_ids:
            return {}
        rows = await self._session.scalars(
            select(PriceHourly)
            .where(
                PriceHourly.asset_id.in_(asset_ids),
                PriceHourly.quote_currency == quote_currency,
                PriceHourly.hour <= at - HOUR,
                PriceHourly.hour > at - HOUR - max_age,
            )
            .order_by(PriceHourly.asset_id, PriceHourly.hour)
        )
        found: dict[int, Decimal] = {}
        for row in rows:
            found[row.asset_id] = row.amount
        return found
