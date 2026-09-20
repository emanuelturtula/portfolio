"""Criterion 7: the database fixture is a real file, and that is checked mechanically.

`:memory:` is the regression this module exists to catch. It is not a style preference:

* an in-memory database belongs to the connection that opened it, and an async engine is
  pooled, so the second connection would find an empty database;
* SQLite silently ignores `PRAGMA journal_mode=WAL` for it, which would make
  `test_engine.py`'s WAL assertion pass or fail for entirely the wrong reason.

Criteria 3 and 7 therefore protect each other, and the last test here ties them together
by proving the WAL sidecar file is actually created next to the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from anyio import Path as AsyncPath
from sqlalchemy import make_url, text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

IN_MEMORY = ":memory:"


def test_database_fixture_is_file_backed(database_url: str, database_path: Path) -> None:
    """The URL resolves to an absolute path on disk, not to an in-memory database."""
    url = make_url(database_url)

    assert url.database is not None
    assert url.database != IN_MEMORY
    assert IN_MEMORY not in database_url
    resolved = Path(url.database)
    assert resolved.is_absolute()
    assert resolved == database_path
    assert resolved.parent.is_dir()


async def test_the_engine_fixture_creates_the_database_file(
    engine: AsyncEngine,
    database_path: Path,
) -> None:
    """Connecting through the fixture's engine leaves bytes on disk."""
    async_path = AsyncPath(database_path)
    assert not await async_path.exists()

    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))

    assert await async_path.is_file()
    assert (await async_path.stat()).st_size > 0


async def test_the_migrated_fixture_is_the_same_file(
    migrated_database_url: str,
    database_path: Path,
) -> None:
    """The migrated fixture must not quietly point somewhere else."""
    async_path = AsyncPath(database_path)

    assert migrated_database_url.endswith(database_path.as_posix())
    assert await async_path.is_file()
    assert (await async_path.stat()).st_size > 0


async def test_a_write_creates_the_wal_sidecar(
    engine: AsyncEngine,
    database_path: Path,
) -> None:
    """WAL is a property of a file-backed database; an in-memory one has no sidecar."""
    wal_path = AsyncPath(database_path.with_name(database_path.name + "-wal"))

    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))
        journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
        sidecar_exists = await wal_path.exists()

    assert journal_mode == "wal"
    assert sidecar_exists


async def test_two_connections_share_the_fixture_database(engine: AsyncEngine) -> None:
    """The check that an in-memory database would fail outright."""
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))

    async with engine.connect() as connection:
        found = await connection.scalar(
            text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'probe'")
        )

    assert found == "probe"
