"""The engine: the SQLite pragmas, and the foreign keys they make real.

Criterion 3 is asserted by reading each pragma back with `PRAGMA <name>` over a
connection handed out by the application's own engine factory. Asserting against
`SQLITE_PRAGMAS` instead would only prove that a tuple of strings contains what it
contains; it would not notice a listener wired to the wrong event, registered on the
wrong engine, or skipped by a dialect guard.

Criterion 4 is the consequence: `foreign_keys=ON` is not an observable property of a
connection unless a violation is actually rejected and a cascade actually cascades.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError

from portfolio.db import models
from portfolio.db.engine import (
    SQLITE_PRAGMAS,
    apply_sqlite_pragmas,
    create_database_engine,
    create_session_factory,
    ensure_database_directory,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.pool import ConnectionPoolEntry

# The value SQLite reports, which is not always the value that was set: `journal_mode`
# answers with a lowercase mode name, and `synchronous` answers with the integer for
# NORMAL rather than the word.
PRAGMA_EXPECTATIONS: tuple[tuple[str, str | int], ...] = (
    ("journal_mode", "wal"),
    ("foreign_keys", 1),
    ("busy_timeout", 5000),
    ("synchronous", 1),
)

A_FIXED_INSTANT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

# Placeholders, not credentials: this change hashes nothing and issues no session. The
# columns are `NOT NULL`, so a row needs *something* in them. Bound to names rather than
# written inline so that the strings are obviously not secrets, to a reader and to ruff.
PLACEHOLDER_USER_DIGEST = "not-a-real-argon2-encoded-hash"
PLACEHOLDER_SESSION_DIGEST = "not-a-real-session-token-digest"


def a_user(username: str = "owner") -> models.User:
    """A minimally valid `users` row."""
    return models.User(
        username=username,
        password_hash=PLACEHOLDER_USER_DIGEST,
        created_at=A_FIXED_INSTANT,
    )


def a_session(user_id: int, token_hash: str = PLACEHOLDER_SESSION_DIGEST) -> models.Session:
    """A minimally valid `sessions` row pointing at `user_id`."""
    return models.Session(
        user_id=user_id,
        token_hash=token_hash,
        created_at=A_FIXED_INSTANT,
        last_seen_at=A_FIXED_INSTANT,
        expires_at=A_FIXED_INSTANT,
    )


@pytest.mark.parametrize(("pragma", "expected"), PRAGMA_EXPECTATIONS)
async def test_sqlite_pragmas_are_in_effect(
    engine: AsyncEngine,
    pragma: str,
    expected: str | int,
) -> None:
    """Each pragma is read back from a connection the engine handed out."""
    async with engine.connect() as connection:
        value = await connection.scalar(text(f"PRAGMA {pragma}"))

    assert value == expected


async def test_every_configured_pragma_is_asserted() -> None:
    """A pragma added to the engine without a matching assertion here is a gap."""
    configured = {statement.removeprefix("PRAGMA ").split("=")[0] for statement in SQLITE_PRAGMAS}

    assert configured == {pragma for pragma, _expected in PRAGMA_EXPECTATIONS}


async def test_pragmas_apply_to_a_second_connection(engine: AsyncEngine) -> None:
    """The pool opens connections lazily, so a pragma set once is not set at all."""
    opened: list[DBAPIConnection] = []

    def record_connect(
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
    ) -> None:
        del connection_record
        opened.append(dbapi_connection)

    event.listen(engine.sync_engine, "connect", record_connect)

    async with engine.connect() as first, engine.connect() as second:
        first_foreign_keys = await first.scalar(text("PRAGMA foreign_keys"))
        second_values = [
            await second.scalar(text(f"PRAGMA {pragma}")) for pragma, _ in PRAGMA_EXPECTATIONS
        ]

    # Two live checkouts cannot be served by one pooled connection, so the listener must
    # have fired twice against two distinct DBAPI connections.
    assert len(opened) == 2
    assert len({id(connection) for connection in opened}) == 2
    assert first_foreign_keys == 1
    assert second_values == [expected for _pragma, expected in PRAGMA_EXPECTATIONS]


async def test_foreign_key_violation_is_rejected(migrated_engine: AsyncEngine) -> None:
    """Without `foreign_keys=ON` SQLite accepts this row without complaint."""
    factory = create_session_factory(migrated_engine)

    async with factory() as session:
        session.add(a_session(user_id=404))
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            await session.commit()


async def test_deleting_a_user_cascades_to_sessions(migrated_engine: AsyncEngine) -> None:
    """`ON DELETE CASCADE` is enforced by SQLite only while the pragma is on."""
    factory = create_session_factory(migrated_engine)

    async with factory() as session:
        user = a_user()
        session.add(user)
        await session.flush()
        session.add(a_session(user_id=user.id))
        await session.commit()
        user_id = user.id

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(models.Session)) == 1
        await session.execute(delete(models.User).where(models.User.id == user_id))
        await session.commit()

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(models.Session)) == 0


async def test_session_factory_does_not_expire_on_commit(migrated_engine: AsyncEngine) -> None:
    """An expired attribute would lazy-load, and a lazy load blocks the event loop."""
    factory = create_session_factory(migrated_engine)

    async with factory() as session:
        user = a_user()
        session.add(user)
        await session.commit()

        # No `await session.refresh(user)`: reading this after the commit must not emit
        # any SQL at all, which is what `expire_on_commit=False` buys.
        assert user.username == "owner"
        assert user.created_at == A_FIXED_INSTANT


def test_the_listener_sets_every_pragma_on_a_bare_connection(tmp_path: Path) -> None:
    """`busy_timeout` is the one pragma whose target value the driver already defaults to.

    aiosqlite opens its connections with a five second timeout of its own, so reading
    `PRAGMA busy_timeout` back as 5000 through the engine would look identical whether
    the listener ran or not. Driving the listener against a connection opened with no
    timeout at all is what makes that case able to fail.
    """
    connection = sqlite3.connect(tmp_path / "bare.db", timeout=0)
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0

        apply_sqlite_pragmas(
            cast("DBAPIConnection", connection),
            cast("ConnectionPoolEntry", None),
        )

        read_back = [
            connection.execute(f"PRAGMA {pragma}").fetchone()[0]
            for pragma, _expected in PRAGMA_EXPECTATIONS
        ]
        assert read_back == [expected for _pragma, expected in PRAGMA_EXPECTATIONS]
    finally:
        connection.close()


def test_ensure_database_directory_creates_a_missing_parent(tmp_path: Path) -> None:
    """SQLite will not create the directory, and its error does not say so."""
    database_path = tmp_path / "nested" / "deeper" / "portfolio.db"

    ensure_database_directory(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    assert database_path.parent.is_dir()
    assert not database_path.exists()


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite://",
        "postgresql+asyncpg://user@example.invalid/portfolio",
    ],
)
def test_ensure_database_directory_ignores_urls_with_no_file(database_url: str) -> None:
    """A URL with no file behind it must be a no-op, not a crash."""
    ensure_database_directory(database_url)


async def test_the_engine_uses_the_async_sqlite_driver(engine: AsyncEngine) -> None:
    """A bare `sqlite://` URL builds a synchronous engine that fails when awaited."""
    assert engine.dialect.name == "sqlite"
    assert engine.dialect.driver == "aiosqlite"


def test_the_pragma_listener_is_registered_on_the_pool(database_url: str) -> None:
    """The `connect` event is DBAPI level, so it has to sit on the *sync* engine."""
    built = create_database_engine(database_url)

    assert event.contains(built.sync_engine, "connect", apply_sqlite_pragmas)
