"""Reads and writes of the single owner account.

The repository owns the queries and nothing else: no policy, no hashing, no clock. It is
handed an `AsyncSession` and it does not commit -- the service that opened the unit of
work decides when it ends, because `create-user --replace` changes the account's
credential and revokes its sessions, and those two have to be one transaction or none.

**Nothing here deletes an account.** Every row the owner has -- wallets, balance history,
exchange accounts and their fills -- hangs off `users.id`, some by `ON DELETE CASCADE` and
some by `RESTRICT`. A delete would either destroy that history or fail on it, so the one
path that used to delete a user now updates it in place instead, and the query that did the
deleting is gone rather than left for the next caller to find.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

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

    async def list_all(self) -> list[User]:
        """Every account, oldest first.

        What `create-user` asks instead of `count()`, because it has to tell none, one and
        more than one apart *and* have the one row in hand to update. A count answers only
        the first half, so it would have to be followed by a fetch; this is both in one
        query. The product allows a single account, so this is one row or none in every
        database the application itself has written.

        Ordered by the primary key only so that the result is deterministic. The order
        does not say which account matters: when there are several, nothing picks one.
        """
        found = await self._session.scalars(select(User).order_by(User.id))
        return list(found)

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

    async def set_credentials(self, user: User, *, username: str, password_hash: str) -> None:
        """Replace the username and the hash in place, keeping `id` and `created_at`.

        Keeping the `id` is the point: it is what every wallet, snapshot and exchange
        account refers to, so an update leaves all of them attached -- including rows in
        tables that do not exist yet. It revokes nothing; the service does that explicitly,
        because an update fires no cascade.
        """
        user.username = username
        user.password_hash = password_hash
        await self._session.flush()
