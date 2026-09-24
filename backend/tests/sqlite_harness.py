"""One migrated, file-backed SQLite database, for the suites outside `tests/db/`.

`tests/db/conftest.py` owns the database fixtures for the persistence suite and its
fixtures are visible only inside that package. Two other suites now need a real database
for reasons of their own -- `tests/services/` because the price service decides things
against rows, and `tests/providers/prices/` because criterion 6's failover is only half
proven until the source that answered is the source in the `source` column -- and the
alternative to this module is the same twenty lines in three places.

**A real file under `tmp_path`, never `:memory:`.** The reason is worth repeating wherever
a second copy lives: an in-memory database belongs to the connection that opened it and an
async engine is pooled, so the schema one connection creates is simply not there for the
next one. `tests/db/test_fixtures.py` asserts that mechanically.

**Built by running the migrations, never `metadata.create_all`.** The migration is the only
description of the schema production executes, so a `CHECK` or a `UNIQUE` asserted against a
`create_all` schema is asserted against a file the Raspberry Pi has never opened. Running
them also seeds `assets`, which is where every `asset_id` in these suites comes from.

The migration hops through a worker thread for the same reason the application's lifespan
does: Alembic's async `env.py` calls `asyncio.run`, which raises `RuntimeError` when a loop
is already running in the calling thread.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from anyio import to_thread

from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import create_database_engine, create_session_factory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@asynccontextmanager
async def migrated_sessionmaker(
    directory: Path,
    *,
    name: str = "test.db",
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The application's own session **factory**, over a file migrated to head.

    The factory rather than one session, because #10 brought a question the earlier suites
    never had to ask: *what had been committed at the moment the provider was called?* A
    second session opened from this factory takes a second connection out of the pool, so
    it sees what the first one committed and nothing it merely staged -- which is the only
    way to tell a row that is on disk from a row that is pending in an identity map.

    The engine is the application's own -- pragmas, foreign keys and `hide_parameters`
    included -- because a test against a differently configured engine is a test of a
    connection production never opens.
    """
    url = f"sqlite+aiosqlite:///{(directory / name).as_posix()}"
    await to_thread.run_sync(upgrade_to_head, url)
    engine = create_database_engine(url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@asynccontextmanager
async def migrated_session(
    directory: Path,
    *,
    name: str = "test.db",
) -> AsyncIterator[AsyncSession]:
    """One session over a freshly migrated, file-backed database.

    Written in terms of `migrated_sessionmaker` rather than beside it, so the three
    decisions this module exists to hold -- a real file, the migrations, the application's
    own engine -- stay in one place however a suite wants them served.
    """
    async with migrated_sessionmaker(directory, name=name) as factory, factory() as opened:
        yield opened
