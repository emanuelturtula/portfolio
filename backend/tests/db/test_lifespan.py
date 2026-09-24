"""Application startup owns the schema and the engine.

This is the spec's addition to the issue: without it the production container comes up
against an empty database file. It is tested here so that removing it, if that is ever
the right call, is a visible revert rather than a silent regression.

`get_settings` is `lru_cache`d and `Settings` also reads a `.env` file, so the cache is
cleared on both sides of the environment override. Skipping the second clear would leave
a temporary database URL cached for every test that runs afterwards in the session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from anyio import Path as AsyncPath
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from portfolio.config import get_settings
from portfolio.db.models import Asset
from portfolio.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.pool import ConnectionPoolEntry

EXPECTED_SEED_SYMBOLS = ["BTC", "KAS", "USDT"]


@pytest.fixture
def lifespan_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the process-wide settings at a temporary file, and put them back.

    The balance schedule is off here for the reason `tests/auth/conftest.py` sets out at
    length: a lifespan with the loop running syncs at startup against two real public
    indexes, because a database created a moment ago has no finished run to suppress it.
    The tests below that are about the scheduler turn it back on by name.
    """
    database_path = tmp_path / "lifespan" / "portfolio.db"
    monkeypatch.setenv(
        "PORTFOLIO_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "false")
    get_settings.cache_clear()
    try:
        yield database_path
    finally:
        # Cleared again so the cached temporary URL cannot leak into a later test. The
        # environment variable itself is undone by `monkeypatch` after this.
        get_settings.cache_clear()


async def test_lifespan_migrates_and_exposes_a_session_factory(
    lifespan_database: Path,
) -> None:
    """Entering the lifespan brings up the schema and publishes the engine."""
    # The parent directory does not exist yet: `ensure_database_directory` has to create
    # it, or SQLite fails with an error that says nothing about a missing directory.
    database_file = AsyncPath(lifespan_database)
    assert not await database_file.parent.exists()
    app = create_app()

    closed: list[DBAPIConnection] = []

    def record_close(
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
    ) -> None:
        del connection_record
        closed.append(dbapi_connection)

    async with app.router.lifespan_context(app):
        engine = app.state.db_engine
        sessionmaker = app.state.db_sessionmaker
        assert isinstance(engine, AsyncEngine)
        assert isinstance(sessionmaker, async_sessionmaker)
        event.listen(engine.sync_engine, "close", record_close)

        assert await database_file.is_file()

        async with sessionmaker() as session:
            symbols = list(await session.scalars(select(Asset.symbol).order_by(Asset.symbol)))
            # The pragmas reach the application's own sessions, not only the tests'.
            foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))
            journal_mode = await session.scalar(text("PRAGMA journal_mode"))

    assert symbols == EXPECTED_SEED_SYMBOLS
    assert foreign_keys == 1
    assert journal_mode == "wal"
    # Leaving the lifespan disposes the engine; a pool left open leaks a file handle and
    # keeps the WAL from being checkpointed on shutdown.
    assert closed


async def test_lifespan_is_idempotent_across_two_startups(lifespan_database: Path) -> None:
    """A restart re-runs `upgrade head` against a database that is already at head."""
    for _ in range(2):
        app = create_app()
        async with app.router.lifespan_context(app):
            sessionmaker = app.state.db_sessionmaker
            async with sessionmaker() as session:
                symbols = list(await session.scalars(select(Asset.symbol).order_by(Asset.symbol)))
        assert symbols == EXPECTED_SEED_SYMBOLS

    assert await AsyncPath(lifespan_database).is_file()


async def test_two_applications_do_not_share_a_pool(lifespan_database: Path) -> None:
    """The engine lives on `app.state` rather than in a module global for this reason."""
    assert not await AsyncPath(lifespan_database).exists()
    first = create_app()
    second = create_app()

    async with first.router.lifespan_context(first), second.router.lifespan_context(second):
        assert first.state.db_engine is not second.state.db_engine
        assert first.state.db_sessionmaker is not second.state.db_sessionmaker
