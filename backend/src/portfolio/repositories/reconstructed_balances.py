"""Reads and writes of the `reconstructed_balances` table (spec 038).

Queries and nothing else: the rebuild service decides when a wallet's rows are replaced, and
commits. Rows are ordered by `day`, a date stored as `YYYY-MM-DD` text, and `confirmed` is an
integer column; nothing here sums or compares a balance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from portfolio.db.models import ReconstructedBalance

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.domain.balance_history import DailyBalance

__all__ = ["ReconstructedBalanceRepository"]


class ReconstructedBalanceRepository:
    """Every query this application makes against `reconstructed_balances`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_wallet(
        self,
        wallet_id: int,
        days: Sequence[DailyBalance],
        *,
        decimals: int,
        rebuilt_at: datetime,
    ) -> None:
        """Replace every row of the wallet with `days` (R6). Flushes, does not commit.

        A delete and inserts in the caller's one transaction, so a reader sees the old rows or
        the new ones and never a mix.
        """
        await self._session.execute(
            delete(ReconstructedBalance).where(ReconstructedBalance.wallet_id == wallet_id)
        )
        self._session.add_all(
            ReconstructedBalance(
                wallet_id=wallet_id,
                day=balance.day,
                confirmed=balance.confirmed,
                decimals=decimals,
                rebuilt_at=rebuilt_at,
            )
            for balance in days
        )
        await self._session.flush()

    async def for_wallets(self, wallet_ids: Sequence[int]) -> dict[int, list[ReconstructedBalance]]:
        """Each wallet's rebuilt days, oldest first. A wallet never rebuilt is absent."""
        if not wallet_ids:
            return {}
        rows = await self._session.scalars(
            select(ReconstructedBalance)
            .where(ReconstructedBalance.wallet_id.in_(wallet_ids))
            .order_by(ReconstructedBalance.wallet_id, ReconstructedBalance.day)
        )
        found: dict[int, list[ReconstructedBalance]] = {}
        for row in rows:
            found.setdefault(row.wallet_id, []).append(row)
        return found

    async def latest_rebuilt_at(self) -> datetime | None:
        """When the newest row was written, or `None`: the rebuild timer's "last run"."""
        found: datetime | None = await self._session.scalar(
            select(func.max(ReconstructedBalance.rebuilt_at))
        )
        return found
