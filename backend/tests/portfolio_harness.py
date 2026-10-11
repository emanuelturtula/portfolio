"""A portfolio for the suites that write wallets and operations below the service (spec 044).

Every wallet and every operation is filed in a portfolio, so a test that inserts one with SQL
or through a repository needs its owner to have one. The service creates the first one on its
own (R8); these are for the tests that go around it. Each returns the owner's `Main`, creating
it the first time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

ENSURE_MAIN: Final = text(
    "INSERT OR IGNORE INTO portfolios (user_id, name, created_at, updated_at) "
    "VALUES (:user, 'Main', '2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')"
)
MAIN_ID: Final = text("SELECT id FROM portfolios WHERE user_id = :user AND name = 'Main'")


def main_portfolio_sync(connection: Connection, user_id: int) -> int:
    """The owner's `Main`, on a synchronous connection."""
    connection.execute(ENSURE_MAIN, {"user": user_id})
    return int(connection.execute(MAIN_ID, {"user": user_id}).scalar_one())


async def main_portfolio(executor: AsyncSession | AsyncConnection, user_id: int) -> int:
    """The owner's `Main`, through an async session or connection."""
    await executor.execute(ENSURE_MAIN, {"user": user_id})
    return int((await executor.execute(MAIN_ID, {"user": user_id})).scalar_one())
