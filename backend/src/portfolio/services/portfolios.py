"""Portfolios: create, list, rename and delete them, and decide which one a write lands in
(spec 044).

## Where a write goes (R8)

A write that names a portfolio goes there, once `resolve_portfolio` has checked it is the
caller's. One that names none goes to the owner's oldest, and an owner who has none yet gets
one named `Main` there and then. So an account created after the migration needs nothing done
for it, and the CLI and every older client keep working unchanged.

## What may be deleted (R10)

A portfolio that holds no wallet, archived ones included, and no operation, and that is not
the owner's last one. The foreign keys refuse the first two again at the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.repositories.portfolios import PortfolioConstraintError, PortfolioRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import Portfolio

__all__ = [
    "DEFAULT_PORTFOLIO_NAME",
    "MAX_NAME_LENGTH",
    "PortfolioInUseError",
    "PortfolioInUseReason",
    "PortfolioNameTakenError",
    "PortfolioNotFoundError",
    "PortfolioRegistryService",
    "PortfolioView",
    "build_portfolio_registry_service",
    "resolve_portfolio",
]

DEFAULT_PORTFOLIO_NAME: Final = "Main"
"""What the first portfolio is called, by the migration and by `resolve_portfolio`."""

MAX_NAME_LENGTH: Final = 60
"""The longest name, once trimmed (R9)."""


def utc_now() -> datetime:
    """The production clock, aware and in UTC."""
    return datetime.now(UTC)


class PortfolioNotFoundError(Exception):
    """No portfolio with that id belongs to the caller."""

    def __str__(self) -> str:
        return "No portfolio with that id."


class PortfolioNameTakenError(Exception):
    """Another of the caller's portfolios already has that name (R9)."""

    def __str__(self) -> str:
        return "A portfolio with this name already exists."


class PortfolioInUseReason(StrEnum):
    """Why a portfolio cannot be deleted (R10)."""

    NOT_EMPTY = "not_empty"
    LAST = "last"


class PortfolioInUseError(Exception):
    """The portfolio cannot be deleted, and `reason` says why."""

    def __init__(self, reason: PortfolioInUseReason) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        if self.reason is PortfolioInUseReason.LAST:
            return "The only portfolio cannot be deleted."
        return (
            "This portfolio still holds wallets or operations. Move them to another "
            "portfolio first."
        )


@dataclass(frozen=True, slots=True)
class PortfolioView:
    """One portfolio, with how many wallets (archived included) and operations it holds."""

    id: int
    name: str
    wallets: int
    operations: int
    created_at: datetime


def normalise_name(name: str) -> str:
    """The name as stored: trimmed.

    Raises:
        ValueError: empty once trimmed, or longer than `MAX_NAME_LENGTH`.
    """
    clean = name.strip()
    if not clean:
        message = "A portfolio needs a name."
        raise ValueError(message)
    if len(clean) > MAX_NAME_LENGTH:
        message = f"A portfolio name is at most {MAX_NAME_LENGTH} characters."
        raise ValueError(message)
    return clean


async def resolve_portfolio(
    portfolios: PortfolioRepository,
    user_id: int,
    portfolio_id: int | None,
    *,
    now: datetime,
) -> int:
    """The portfolio a write lands in (R8): the one named, or the owner's first.

    Flushes a new `Main` when the owner has none; the caller's commit keeps it.

    Raises:
        PortfolioNotFoundError: `portfolio_id` is not one of the caller's.
    """
    if portfolio_id is not None:
        if await portfolios.get_for_user(user_id, portfolio_id) is None:
            raise PortfolioNotFoundError
        return portfolio_id
    existing = await portfolios.list_for_user(user_id)
    if existing:
        return existing[0].id
    created = await portfolios.add(user_id=user_id, name=DEFAULT_PORTFOLIO_NAME, created_at=now)
    return created.id


class PortfolioRegistryService:
    """Owns the transaction: the repository flushes and this class commits."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        portfolios: PortfolioRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._portfolios = portfolios
        self._clock = clock

    async def list_portfolios(self, user_id: int) -> list[PortfolioView]:
        """Every portfolio of the account, oldest first, with what each holds."""
        rows = await self._portfolios.list_for_user(user_id)
        wallets = await self._portfolios.wallet_counts(user_id)
        operations = await self._portfolios.operation_counts(user_id)
        return [
            _view(row, wallets=wallets.get(row.id, 0), operations=operations.get(row.id, 0))
            for row in rows
        ]

    async def exists(self, user_id: int, portfolio_id: int) -> bool:
        """Whether `portfolio_id` is one of the caller's: what a read scoped to it checks."""
        return await self._portfolios.get_for_user(user_id, portfolio_id) is not None

    async def create(self, user_id: int, name: str) -> PortfolioView:
        """Add a portfolio.

        Raises:
            ValueError: the name is empty or too long.
            PortfolioNameTakenError: another portfolio has that name.
        """
        clean = normalise_name(name)
        if await self._portfolios.find_by_name(user_id, clean) is not None:
            raise PortfolioNameTakenError
        try:
            row = await self._portfolios.add(user_id=user_id, name=clean, created_at=self._clock())
        except PortfolioConstraintError as exc:
            # Lost a race with another create of the same name.
            await self._session.rollback()
            raise PortfolioNameTakenError from exc
        await self._session.commit()
        return _view(row, wallets=0, operations=0)

    async def rename(self, user_id: int, portfolio_id: int, name: str) -> PortfolioView:
        """Rename a portfolio. Its own name again is not a conflict.

        Raises:
            ValueError: the name is empty or too long.
            PortfolioNotFoundError: no such portfolio is the caller's.
            PortfolioNameTakenError: another portfolio has that name.
        """
        clean = normalise_name(name)
        row = await self._portfolios.get_for_user(user_id, portfolio_id)
        if row is None:
            raise PortfolioNotFoundError
        holder = await self._portfolios.find_by_name(user_id, clean)
        if holder is not None and holder.id != row.id:
            raise PortfolioNameTakenError
        try:
            await self._portfolios.rename(row, clean, self._clock())
        except PortfolioConstraintError as exc:
            await self._session.rollback()
            raise PortfolioNameTakenError from exc
        await self._session.commit()
        (view,) = [entry for entry in await self.list_portfolios(user_id) if entry.id == row.id]
        return view

    async def delete(self, user_id: int, portfolio_id: int) -> None:
        """Delete an empty portfolio that is not the owner's last (R10).

        Raises:
            PortfolioNotFoundError: no such portfolio is the caller's.
            PortfolioInUseError: it holds rows, or it is the only one.
        """
        views = await self.list_portfolios(user_id)
        found = [view for view in views if view.id == portfolio_id]
        if not found:
            raise PortfolioNotFoundError
        (view,) = found
        # The last one first: emptying it would not make it deletable.
        if len(views) == 1:
            raise PortfolioInUseError(PortfolioInUseReason.LAST)
        if view.wallets or view.operations:
            raise PortfolioInUseError(PortfolioInUseReason.NOT_EMPTY)
        row = await self._portfolios.get_for_user(user_id, portfolio_id)
        if row is None:
            raise PortfolioNotFoundError
        try:
            await self._portfolios.delete(row)
        except PortfolioConstraintError as exc:
            # A wallet or an operation was filed in it after the counts were read.
            await self._session.rollback()
            raise PortfolioInUseError(PortfolioInUseReason.NOT_EMPTY) from exc
        await self._session.commit()


def _view(row: Portfolio, *, wallets: int, operations: int) -> PortfolioView:
    return PortfolioView(
        id=row.id,
        name=row.name,
        wallets=wallets,
        operations=operations,
        created_at=row.created_at,
    )


def build_portfolio_registry_service(
    session: AsyncSession, *, clock: Callable[[], datetime] = utc_now
) -> PortfolioRegistryService:
    """Assemble the service over one database session."""
    return PortfolioRegistryService(
        session=session, portfolios=PortfolioRepository(session), clock=clock
    )
