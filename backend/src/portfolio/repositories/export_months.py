"""Reads and writes of the `export_months` table (spec 040).

Queries and nothing else: which months an owner has marked done, and marking one. No clock
and no rule about which months may be marked; the service decides that. Every query is
scoped by `user_id`, for the reason `repositories/wallets.py` gives.

Marking is read-then-write rather than `INSERT ... ON CONFLICT`, for the reason
`repositories/prices.py` gives: one writer, a handful of rows, and no dialect-specific SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from portfolio.db.models import ExportMonth

if TYPE_CHECKING:
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class ExportMonthRepository:
    """Every query this application makes against `export_months`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def done_months(self, user_id: int) -> set[date]:
        """The months this owner has marked done."""
        result = await self._session.scalars(
            select(ExportMonth.month).where(ExportMonth.user_id == user_id)
        )
        return set(result)

    async def mark_done(self, user_id: int, month: date, *, at: datetime) -> None:
        """Record `month` as done, unless it already is. Flushes and does not commit.

        A month marked twice keeps its first `done_at`: the second mark changes nothing.
        """
        existing = await self._session.scalar(
            select(ExportMonth.id).where(ExportMonth.user_id == user_id, ExportMonth.month == month)
        )
        if existing is not None:
            return
        self._session.add(ExportMonth(user_id=user_id, month=month, done_at=at))
        await self._session.flush()
