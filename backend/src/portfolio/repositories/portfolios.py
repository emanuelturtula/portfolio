"""Reads and writes of the `portfolios` table (spec 044).

Queries and nothing else: no clock, no policy about names or about what may be deleted, and
no commit. Every lookup is scoped by `user_id`, for the reason `repositories.wallets` gives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from portfolio.db.models import ExchangeOperation, Portfolio, Wallet

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["PortfolioConstraintError", "PortfolioRepository"]


class PortfolioConstraintError(Exception):
    """A write the database refused, re-raised without the driver's exception, for the reason
    `WalletConstraintError` gives. The service decides what it means by looking."""


class PortfolioRepository:
    """Every query this application makes against `portfolios`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: int) -> list[Portfolio]:
        """Every portfolio of the account, oldest first: the first is where an unnamed write
        goes (R8)."""
        rows = await self._session.scalars(
            select(Portfolio).where(Portfolio.user_id == user_id).order_by(Portfolio.id)
        )
        return list(rows)

    async def get_for_user(self, user_id: int, portfolio_id: int) -> Portfolio | None:
        """One portfolio by id, provided it is this account's."""
        found: Portfolio | None = await self._session.scalar(
            select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
        )
        return found

    async def find_by_name(self, user_id: int, name: str) -> Portfolio | None:
        """The portfolio holding `name` in the unique constraint, if any."""
        found: Portfolio | None = await self._session.scalar(
            select(Portfolio).where(Portfolio.user_id == user_id, Portfolio.name == name)
        )
        return found

    async def wallet_counts(self, user_id: int) -> dict[int, int]:
        """How many wallets each portfolio holds, archived ones included. A count, not money."""
        rows = await self._session.execute(
            select(Wallet.portfolio_id, func.count())
            .where(Wallet.user_id == user_id)
            .group_by(Wallet.portfolio_id)
        )
        return {int(portfolio_id): int(count) for portfolio_id, count in rows.all()}

    async def operation_counts(self, user_id: int) -> dict[int, int]:
        """How many exchange operations each portfolio holds. A count, not money."""
        rows = await self._session.execute(
            select(ExchangeOperation.portfolio_id, func.count())
            .where(ExchangeOperation.user_id == user_id)
            .group_by(ExchangeOperation.portfolio_id)
        )
        return {int(portfolio_id): int(count) for portfolio_id, count in rows.all()}

    async def add(self, *, user_id: int, name: str, created_at: datetime) -> Portfolio:
        """Insert a portfolio. Flushes and does not commit.

        Raises:
            PortfolioConstraintError: the database refused it, most plausibly the name.
        """
        portfolio = Portfolio(
            user_id=user_id, name=name, created_at=created_at, updated_at=created_at
        )
        self._session.add(portfolio)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise PortfolioConstraintError from exc
        return portfolio

    async def rename(self, portfolio: Portfolio, name: str, updated_at: datetime) -> None:
        """Replace the name in place. Flushes and does not commit.

        Raises:
            PortfolioConstraintError: the database refused it, most plausibly the name.
        """
        portfolio.name = name
        portfolio.updated_at = updated_at
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise PortfolioConstraintError from exc

    async def delete(self, portfolio: Portfolio) -> None:
        """Remove one portfolio. Flushes and does not commit.

        Raises:
            PortfolioConstraintError: a wallet or an operation still points at it.
        """
        await self._session.delete(portfolio)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise PortfolioConstraintError from exc
