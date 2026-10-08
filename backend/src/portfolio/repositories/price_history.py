"""Reads and writes of the `price_history` table (spec 037).

Queries and nothing else, as in `repositories/prices.py`: no clock, no policy about which
price a day should have beyond the one rule the table exists to keep (R2), and no commit.

## Nothing here aggregates, orders or compares money

`amount` is a `TEXT` column, and `SUM()`, `ORDER BY` and `<` on it go through SQLite's numeric
affinity, the float this application keeps money away from. Rows are read whole and ordered
by `day`, a date stored as `YYYY-MM-DD` text, which orders as dates do -- and for the same
reason `earliest_close_day` may take a `MIN()` over it.

## Why read-then-write

`UNIQUE (asset_id, quote_currency, day)` is one row per day, and the rule for replacing it
depends on the row already there: a `close` replaces anything, an `observed` price replaces
only another `observed` (R2). That decision is easier to read as Python than as a
dialect-specific `ON CONFLICT ... WHERE`, and there is one writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select
from sqlalchemy.exc import StatementError

from portfolio.db.models import PriceHistory
from portfolio.domain.money import require_amount

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["CLOSE", "OBSERVED", "PriceHistoryRepository"]

CLOSE: Final = "close"
"""A daily candle's closing price: final for its day."""

OBSERVED: Final = "observed"
"""The latest price the hourly refresh saw on a day: gives way to a `close`."""


class PriceHistoryRepository:
    """Every query this application makes against `price_history`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        asset_id: int,
        quote_currency: str,
        day: date,
        amount: Decimal,
        basis: str,
        source: str,
        recorded_at: datetime,
    ) -> bool:
        """Write one day's price by spec 037's R2, and say whether anything was written.

        A `close` is written over whatever the day holds. An `observed` price is written over
        an earlier `observed` one, and **never over a `close`**: the refresh runs every hour,
        and a closing price the backfill brought must not be replaced by the price at some
        hour of the same day. Flushes and does not commit.

        Raises:
            TypeError: `amount` is not a `Decimal`.
            ValueError: `amount` is not finite, or is one the column refuses.
        """
        require_amount(amount, subject="price_history.amount")
        existing = await self._session.scalar(
            select(PriceHistory).where(
                PriceHistory.asset_id == asset_id,
                PriceHistory.quote_currency == quote_currency,
                PriceHistory.day == day,
            )
        )
        if existing is None:
            self._session.add(
                PriceHistory(
                    asset_id=asset_id,
                    quote_currency=quote_currency,
                    day=day,
                    amount=amount,
                    basis=basis,
                    source=source,
                    recorded_at=recorded_at,
                )
            )
        elif basis == CLOSE or existing.basis == OBSERVED:
            existing.amount = amount
            existing.basis = basis
            existing.source = source
            existing.recorded_at = recorded_at
        else:
            return False
        await self._flush()
        return True

    async def series(
        self,
        *,
        asset_ids: Sequence[int],
        quote_currency: str,
        first_day: date,
        last_day: date,
    ) -> dict[int, dict[date, Decimal]]:
        """Each asset's price per day from `first_day` to `last_day`, inclusive.

        Keyed by `asset_id`, then by day. A day with no row is absent, never zero: the domain
        decides what a missing price means.
        """
        if not asset_ids:
            return {}
        rows = await self._session.scalars(
            select(PriceHistory)
            .where(
                PriceHistory.asset_id.in_(asset_ids),
                PriceHistory.quote_currency == quote_currency,
                PriceHistory.day >= first_day,
                PriceHistory.day <= last_day,
            )
            .order_by(PriceHistory.asset_id, PriceHistory.day)
        )
        found: dict[int, dict[date, Decimal]] = {}
        for row in rows:
            found.setdefault(row.asset_id, {})[row.day] = row.amount
        return found

    async def earliest_close_day(self, asset_id: int, quote_currency: str) -> date | None:
        """The first day this pair has a `close` for, or `None` when it has none (spec 038).

        Where the backfill's older source stops: it fills only the days before this one (R8).
        An `observed` row does not count -- it is the refresh's price, not a close, and a
        close for its day is still owed. `MIN()` over `day`, a date stored as `YYYY-MM-DD`
        text, is an ordering on a date, not on money.
        """
        found: date | None = await self._session.scalar(
            select(func.min(PriceHistory.day)).where(
                PriceHistory.asset_id == asset_id,
                PriceHistory.quote_currency == quote_currency,
                PriceHistory.basis == CLOSE,
            )
        )
        return found

    async def latest_close_recorded_at(self) -> datetime | None:
        """When the newest `close` row was written, or `None` when there is none.

        A timestamp, not money: `MAX()` over `recorded_at` is an ordering on an instant.
        """
        found: datetime | None = await self._session.scalar(
            select(func.max(PriceHistory.recorded_at)).where(PriceHistory.basis == CLOSE)
        )
        return found

    async def _flush(self) -> None:
        """Flush, and let a column's own refusal out as itself, as `PriceRepository` does.

        Raises:
            TypeError: a column type refused the value's type.
            ValueError: a column type refused the value.
        """
        try:
            await self._session.flush()
        except StatementError as exc:
            if isinstance(exc.orig, TypeError | ValueError):
                raise exc.orig from exc
            raise
