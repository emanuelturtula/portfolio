"""Fixtures for the database layer.

Every database built here is a real file under `tmp_path`. `:memory:` is deliberately
never used, and `test_fixtures.py` asserts that mechanically rather than trusting review:

* an in-memory database belongs to a single connection, and an async engine is pooled, so
  the schema one connection creates is simply not there for the next one;
* SQLite silently ignores `PRAGMA journal_mode=WAL` for an in-memory database, so the WAL
  assertion in `test_engine.py` would pass or fail for a reason that has nothing to do
  with the code under test.

The migration fixtures hop through a worker thread for the same reason the application's
lifespan does: Alembic's async `env.py` calls `asyncio.run`, which raises `RuntimeError`
when a loop is already running in the calling thread.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
import structlog
from anyio import to_thread
from sqlalchemy import create_engine

from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import create_database_engine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Undo anything a test does to the global logging configuration.

    Two tests here reconfigure it deliberately -- one reads `alembic.ini`, whose
    `fileConfig` section installs its own handlers, and one renders a real log record to
    assert a value is absent from it. Leaving either installed would silently change
    logging for every test that runs afterwards, the redaction suite included.
    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        structlog.reset_defaults()
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """The file the test database lives in. A real path, on a real filesystem."""
    return tmp_path / "test.db"


@pytest.fixture
def database_url(database_path: Path) -> str:
    """The async URL the application's own engine factory is given."""
    return f"sqlite+aiosqlite:///{database_path.as_posix()}"


@pytest.fixture
def sync_url(database_path: Path) -> str:
    """The same file, addressed through the synchronous driver."""
    return f"sqlite:///{database_path.as_posix()}"


@pytest.fixture
def sync_engine(sync_url: str) -> Iterator[Engine]:
    """A plain synchronous engine over the same file, for reflection and assertions.

    Deliberately not built by `create_database_engine`: reading a schema back through a
    second, unconfigured connection is a stronger check than reading it back through the
    engine that wrote it.
    """
    engine = create_engine(sync_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """The application's own engine, pragmas and all, over an empty database file."""
    async_engine = create_database_engine(database_url)
    try:
        yield async_engine
    finally:
        await async_engine.dispose()


@pytest.fixture
async def migrated_database_url(database_url: str) -> str:
    """A file-backed database already migrated to head."""
    await to_thread.run_sync(upgrade_to_head, database_url)
    return database_url


@pytest.fixture
async def migrated_engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    """The application's own engine over a database that is already at head."""
    async_engine = create_database_engine(migrated_database_url)
    try:
        yield async_engine
    finally:
        await async_engine.dispose()
