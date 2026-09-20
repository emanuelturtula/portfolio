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
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
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


def take_transaction_control(
    dbapi_connection: DBAPIConnection,
    connection_record: ConnectionPoolEntry,
) -> None:
    """Stop pysqlite deciding when a transaction starts. Migration connections only.

    In its default mode the driver emits `BEGIN` before `INSERT`, `UPDATE`, `DELETE` and
    `REPLACE`, and before nothing else -- so `CREATE TABLE` and `DROP TABLE` run outside
    any transaction and survive a rollback. Setting `isolation_level` to `None` hands
    transaction control to us; `begin_migration_transaction` is the other half.
    """
    del connection_record  # Part of the event signature, not needed here.
    # `isolation_level` is a pysqlite attribute rather than part of the DBAPI protocol,
    # so it is not on the `DBAPIConnection` type.
    driver_connection: Any = dbapi_connection
    driver_connection.isolation_level = None


def begin_migration_transaction(connection: Connection) -> None:
    """Emit `BEGIN` ourselves, so the transaction brackets DDL as well as DML.

    A connection running under AUTOCOMMIT is left alone. SQLAlchemy still creates a
    transaction object and still fires this event there, but emitting `BEGIN` would defeat
    the reason the caller asked for AUTOCOMMIT: `disable_foreign_key_enforcement` switches
    to it so that its `PRAGMA foreign_keys=OFF` lands outside a transaction, where the
    pragma is not a no-op.

    Otherwise `isolation_level` is re-asserted here rather than only on connect, because
    changing the isolation level hands transaction control back to the driver and that
    guard has to change it. This is the last point before a transaction actually starts.
    """
    if connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        return
    driver_connection: Any = connection.connection.dbapi_connection
    driver_connection.isolation_level = None
    connection.exec_driver_sql("BEGIN")


def create_migration_engine(database_url: str) -> AsyncEngine:
    """Build an engine for running migrations, with transactional DDL on SQLite.

    A separate engine rather than a flag on the runtime one: the application's
    transaction behaviour must not change, and a rollback that silently kept a
    `CREATE TABLE` is exactly the class of bug this exists to prevent. Everything the
    runtime engine does -- the four pragmas included -- still applies here; this only
    adds explicit transaction control on top.

    Without it a refused run is unrecoverable rather than merely failed: the integrity
    check rolls back the rows and the `alembic_version` stamp, the schema changes stay,
    and every later `upgrade head` dies with "table already exists".
    """
    engine = create_database_engine(database_url)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", take_transaction_control)
        event.listen(engine.sync_engine, "begin", begin_migration_transaction)
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
