"""Reads and writes of the single owner account.

The repository owns the queries and nothing else: no policy, no hashing, no clock. It is
handed an `AsyncSession` and it does not commit -- the service that opened the unit of
work decides when it ends, because `create-user --replace` deletes a user and inserts
another and those two have to be one transaction or none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from portfolio.db.models import User

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class UserRepository:
    """Every query this application makes against `users`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_username(self, username: str) -> User | None:
        """The account with this exact username, or `None`."""
        found: User | None = await self._session.scalar(
            select(User).where(User.username == username)
        )
        return found

    async def get_by_id(self, user_id: int) -> User | None:
        """The account with this primary key, or `None`."""
        found: User | None = await self._session.get(User, user_id)
        return found

    async def count(self) -> int:
        """How many accounts exist.

        `COUNT` and not one of the aggregates rule 2 forbids: those coerce a `TEXT` money
        column to a C double, and counting rows touches no column at all.
        """
        total = await self._session.scalar(select(func.count()).select_from(User))
        # `SELECT COUNT(*)` always returns a row, so this is never None in practice; the
        # fallback is here because the type says it can be and mypy is right to insist.
        return total or 0

    async def add(self, *, username: str, password_hash: str, created_at: datetime) -> User:
        """Insert an account. The caller has already applied the password policy."""
        user = User(username=username, password_hash=password_hash, created_at=created_at)
        self._session.add(user)
        await self._session.flush()
        return user

    async def set_password_hash(self, user: User, password_hash: str) -> None:
        """Replace the stored hash in place, leaving the account's identity alone."""
        user.password_hash = password_hash
        await self._session.flush()

    async def delete_all(self) -> None:
        """Remove every account.

        The `ON DELETE CASCADE` on `sessions.user_id` takes every session with it, which
        is why `create-user --replace` does not have to revoke anything by hand -- and is
        also why the pragma that enables foreign keys is not optional.
        """
        await self._session.execute(delete(User))
        await self._session.flush()
