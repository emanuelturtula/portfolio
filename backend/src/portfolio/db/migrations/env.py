"""The Alembic environment: async engine, batch mode, one source for the database URL.

Two settings here are not optional on SQLite:

* `render_as_batch=True` -- SQLite has no `ALTER COLUMN` and no `DROP CONSTRAINT`, so
  Alembic changes one by rebuilding the table. Without batch mode, any migration that
  alters or drops a column simply cannot be written.
* `compare_type=True` -- autogenerate ignores type changes otherwise, which on a database
  with no declared type affinity means a column silently keeps the wrong type forever.

Batch mode is also why the migration connection runs with foreign key enforcement off and
is checked for orphaned references before it commits. `portfolio.db.migration_guards`
explains that mechanism in full; the short version is that a batch rebuild drops the
original table, and a `DROP TABLE` under `foreign_keys=ON` fires cascades.

The URL is never read from the ini. It arrives on `Config.attributes` from
`build_alembic_config`, and falls back to `Settings` when the developer CLI is driving.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context

from portfolio.config import get_settings
from portfolio.db.alembic_config import DATABASE_URL_ATTRIBUTE
from portfolio.db.engine import create_database_engine
from portfolio.db.migration_guards import (
    assert_no_dangling_foreign_keys,
    disable_foreign_key_enforcement,
)
from portfolio.db.models import metadata as target_metadata

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config

if config.config_file_name is not None:
    # Only the developer CLI hands Alembic an ini file, and it is the only caller that
    # wants stdlib logging configured. A Config built by `build_alembic_config` has no
    # file behind it, so the application's structlog setup is left alone.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def resolve_database_url() -> str:
    """Return the URL this run should migrate: the caller's, else the configured one."""
    url = config.attributes.get(DATABASE_URL_ATTRIBUTE)
    if isinstance(url, str) and url:
        return url
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it, for `alembic upgrade --sql`.

    There is no connection here, so neither guard applies and the generated script does
    not carry `PRAGMA foreign_keys=OFF`. That omission is deliberate: Alembic wraps the
    script in a transaction, and the pragma is a documented no-op inside one, so emitting
    it would produce a line that looks protective and is not. An operator applying a
    generated script that contains a batch rebuild must set the pragma themselves, outside
    the transaction, and run `PRAGMA foreign_key_check` before committing.
    """
    context.configure(
        url=resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on an already-open synchronous connection.

    Order matters twice over.

    Enforcement goes off first, before anything opens a transaction, because the pragma
    does nothing once one is open.

    Then the whole run is wrapped in one explicit transaction, opened *before* the context
    is configured. Alembic's SQLite implementation declares DDL non-transactional, which
    means its own `begin_transaction()` is a no-op at the run level and a real,
    self-committing transaction around each individual migration -- so an integrity check
    placed after `run_migrations()` would be inspecting a database that had already
    committed, and raising would stamp the revision anyway. Beginning the transaction here
    instead puts Alembic into its external-transaction mode: it stops managing commits,
    and the check below genuinely gates the commit rather than merely reporting on it.
    """
    disable_foreign_key_enforcement(connection)

    with connection.begin():
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        assert_no_dangling_foreign_keys(connection)


async def run_async_migrations() -> None:
    """Open the application's own engine -- pragmas included -- and migrate through it."""
    engine = create_database_engine(resolve_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """Drive the async migration run from a thread that has no event loop of its own."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
