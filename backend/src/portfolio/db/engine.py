"""The async engine, the session factory, and the SQLite pragmas that make both safe.

Two SQLite defaults are actively wrong for this application, and both fail silently:

* foreign keys are **off** unless enabled per connection, which makes every `ForeignKey`
  in the schema decorative rather than enforced;
* the rollback journal serialises readers against a writer, so the UI polling `/api` would
  block -- or time out -- for as long as a sync is writing.

Both are fixed on the DBAPI `connect` event rather than once at startup, because the pool
opens connections lazily and a pragma set on one connection says nothing about the next.
The event fires for every connection the pool creates, including the ones Alembic opens.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.pool import ConnectionPoolEntry

# Applied in order, once per connection.
#
#   journal_mode=WAL   a reader no longer blocks on a writer. Database-level rather than
#                      connection-level, so re-issuing it is redundant but harmless -- and
#                      it is what brings a brand new file up in WAL with no bootstrap step.
#   foreign_keys=ON    off by default; without it the schema's foreign keys do nothing.
#   busy_timeout=5000  turns "database is locked" from a crash into a five second wait.
#   synchronous=NORMAL the safe pairing with WAL: durable across a process crash, one
#                      fsync per checkpoint instead of one per commit.
SQLITE_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)


def apply_sqlite_pragmas(
    dbapi_connection: DBAPIConnection,
    connection_record: ConnectionPoolEntry,
) -> None:
    """Set every pragma on a freshly opened DBAPI connection."""
    del connection_record  # The pool entry is part of the event signature, not needed.
    cursor = dbapi_connection.cursor()
    try:
        for statement in SQLITE_PRAGMAS:
            cursor.execute(statement)
            # `PRAGMA journal_mode` answers with the mode it settled on. Leaving that row
            # unread leaves the cursor mid-result, so drain every statement uniformly.
            cursor.fetchall()
    finally:
        cursor.close()


def create_database_engine(database_url: str) -> AsyncEngine:
    """Build the async engine for a URL, with the SQLite pragmas wired to its pool."""
    engine = create_async_engine(database_url)
    if engine.dialect.name == "sqlite":
        # Registered on the sync engine: the `connect` event is a DBAPI-level event, and
        # the async engine is a wrapper around the sync one rather than a separate pool.
        event.listen(engine.sync_engine, "connect", apply_sqlite_pragmas)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory the application hands to its repositories.

    `expire_on_commit=False` because an expired attribute reloads itself lazily, and a
    lazy load inside an async request is a blocking call in the middle of the event loop.
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ensure_database_directory(database_url: str) -> None:
    """Create the directory holding a file-backed SQLite database, if it is missing.

    SQLite will not create a missing parent directory: it fails with "unable to open
    database file", which says nothing about what is actually wrong. The production image
    already ships an owned `/app/data`, so this matters most on a fresh checkout.
    """
    url = make_url(database_url)
    database = url.database
    if not url.drivername.startswith("sqlite") or not database or database == ":memory:":
        return
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
