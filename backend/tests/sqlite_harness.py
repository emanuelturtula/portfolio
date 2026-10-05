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

**Run once per process, then copied.** The migrations run into one template file the first
time a process needs a database, and every database after that is a byte copy of it. The
copy is the file the migrations wrote, so nothing above changes: it is what production
executes, with the seeded `assets`. What changes is the cost. A migration from an empty file
takes about 140 ms with coverage on and a copy about 1 ms, and on 2026-10-04 846 tests took
901 databases between them here and through `tests/db/conftest.py`, so migrating each one was
minutes of every CI run spent proving the same migration. The migrations themselves are
proven from an empty file, step by step, by the suites in `tests/db/` that are about them,
and those do not come through here.

The migration hops through a worker thread for the same reason the application's lifespan
does: Alembic's async `env.py` calls `asyncio.run`, which raises `RuntimeError` when a loop
is already running in the calling thread.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

from anyio import to_thread

from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import create_database_engine, create_session_factory
from tests.backup_harness import SIDECARS, sqlite_url

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEMPLATE_LOCK: Final = threading.Lock()


@cache
def _build_template() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="portfolio-template-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    template = directory / "template.db"
    upgrade_to_head(sqlite_url(template))
    # A copy of the main file alone would leave behind whatever a `-wal` still held. The
    # migration's engine is disposed when it finishes, which checkpoints and removes it.
    left = [name for name in SIDECARS if template.with_name(template.name + name).exists()]
    if left:
        message = f"the migrated template still has {left} beside it, so a copy would be partial"
        raise RuntimeError(message)
    return template


def migrated_template() -> Path:
    """The file every database here is copied from: migrated to head once per process.

    Per process rather than per session, so each pytest-xdist worker builds its own and none
    of them ever opens another's. Lazily, so a run that needs no database builds none. Under
    a lock, because callers arrive on worker threads and two of them could otherwise both
    build it. Blocking: an async caller hops through a worker thread, as the migration must.
    """
    with _TEMPLATE_LOCK:
        return _build_template()


def copy_migrated_template(database: Path) -> None:
    """Put a database migrated to head at `database`, which must not exist yet."""
    shutil.copyfile(migrated_template(), database)


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

    A file that is already there is migrated where it lies instead of replaced, which is what
    the lifespan does at every start. Suites open the same file twice to play a restart, and
    the second open must find what the first one wrote.
    """
    database = directory / name
    url = sqlite_url(database)
    if database.exists():
        await to_thread.run_sync(upgrade_to_head, url)
    else:
        await to_thread.run_sync(copy_migrated_template, database)
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
