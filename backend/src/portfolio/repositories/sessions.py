"""Reads and writes of the `sessions` table.

Every lookup here is by `token_hash`, which carries a unique index, because that is the
query every authenticated request performs. The plaintext token never reaches this module:
the service hashes it first, and the column holds only the digest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from portfolio.db.models import Session, User

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class SessionRepository:
    """Every query this application makes against `sessions`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_with_username(self, token_hash: str) -> tuple[Session, str] | None:
        """The session row for a token digest, with its owner's name, or `None`.

        Joined rather than fetched in two steps, and not because of the round trip: a
        second query would have to handle a session whose user has been deleted, a case
        the `ON DELETE CASCADE` makes impossible. A branch that cannot run is a branch
        nothing can test, so the join removes it instead.

        Expired rows come back like any other. Whether a session is still valid is a rule
        in `domain.auth`, applied by the service, rather than a `WHERE` clause repeated at
        every call site.
        """
        result = await self._session.execute(
            select(Session, User.username)
            .join(User, User.id == Session.user_id)
            .where(Session.token_hash == token_hash)
        )
        row = result.first()
        if row is None:
            return None
        return row[0], row[1]

    async def add(
        self,
        *,
        user_id: int,
        token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> Session:
        """Insert a freshly issued session. `last_seen_at` starts equal to `created_at`."""
        row = Session(
            user_id=user_id,
            token_hash=token_hash,
            created_at=created_at,
            last_seen_at=created_at,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def touch(self, row: Session, now: datetime) -> None:
        """Slide the idle window. `expires_at` is untouched: activity cannot move the ceiling."""
        row.last_seen_at = now
        await self._session.flush()

    async def delete_by_id(self, session_id: int) -> None:
        """Revoke one session -- what logout does."""
        await self._session.execute(delete(Session).where(Session.id == session_id))
        await self._session.flush()

    async def delete_for_user(self, user_id: int) -> None:
        """Revoke every session an account holds -- what a password change does."""
        await self._session.execute(delete(Session).where(Session.user_id == user_id))
        await self._session.flush()
