"""Price rows planted the way a test needs them: straight into the `prices` table.

`PriceRepository` writes through the price refresh, which asks a vendor; a test that only
needs a price to exist plants the row the refresh would have written instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from tests.balance_harness import sqlite_timestamp

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["plant_price"]


async def plant_price(
    session: AsyncSession,
    *,
    symbol: str,
    amount: Decimal,
    as_of: datetime,
    currency: str = "USD",
    source: str = "coinbase",
) -> None:
    """A `prices` row in the fixed-point text `NumericText(12)` writes. Committed."""
    await session.execute(
        text(
            "INSERT INTO prices (asset_id, quote_currency, amount, source, as_of, fetched_at) "
            "VALUES ((SELECT id FROM assets WHERE symbol = :symbol), "
            ":currency, :amount, :source, :as_of, :as_of)"
        ),
        {
            "symbol": symbol,
            "currency": currency,
            "amount": f"{amount:.12f}",
            "source": source,
            "as_of": sqlite_timestamp(as_of),
        },
    )
    await session.commit()
