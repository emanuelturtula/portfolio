"""The guards that make a migration run safe on SQLite.

`env.py` cannot be imported -- its module body runs a migration -- so the behaviour that
stands between a batch rebuild and silent row deletion lives in an importable module and
is tested here directly, on plain synchronous connections.

Two of the branches cannot be reached with real SQLite: the read-back that refuses to
continue when `PRAGMA foreign_keys=OFF` did not take, and the non-SQLite early return.
Both are driven through a recording stand-in rather than left uncovered, because the
first is the one thing standing between a rebuild and data loss and the second is what
keeps the guards inert if the engine is ever pointed elsewhere.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import create_engine, event

from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import apply_sqlite_pragmas
from portfolio.db.migration_guards import (
    MigrationIntegrityError,
    assert_no_dangling_foreign_keys,
    disable_foreign_key_enforcement,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

ORPHAN_SESSION = (
    "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
    "VALUES (4242, 'orphan-token', '2026-01-01', '2026-01-01', '2026-01-01')"
)
A_USER = (
    "INSERT INTO users (id, username, password_hash, created_at) "
    "VALUES (1, 'owner', 'x', '2026-01-01')"
)
A_SESSION = (
    "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
    "VALUES (1, 'valid-token', '2026-01-01', '2026-01-01', '2026-01-01')"
)


class RecordingConnection:
    """A stand-in for `Connection` that records what the guards asked it to do."""

    def __init__(
        self,
        dialect_name: str = "sqlite",
        *,
        enforcement_after_switch: int = 0,
        violations: Sequence[tuple[str, int, str, int]] = (),
    ) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.statements: list[str] = []
        self.options_seen: list[dict[str, Any]] = []
        self.commits = 0
        self._enforcement_after_switch = enforcement_after_switch
        self._violations = list(violations)

    def get_isolation_level(self) -> str:
        return "SERIALIZABLE"

    def execution_options(self, **options: Any) -> RecordingConnection:
        self.options_seen.append(options)
        return self

    def exec_driver_sql(self, statement: str) -> SimpleNamespace:
        self.statements.append(statement)
        if statement == "PRAGMA foreign_keys":
            return SimpleNamespace(
                scalar=lambda: self._enforcement_after_switch,
                fetchall=lambda: [],
            )
        if statement == "PRAGMA foreign_key_check":
            return SimpleNamespace(scalar=lambda: None, fetchall=lambda: self._violations)
        return SimpleNamespace(scalar=lambda: None, fetchall=lambda: [])

    def commit(self) -> None:
        self.commits += 1


def as_connection(recording: RecordingConnection) -> Connection:
    """Hand the stand-in to code annotated for a real `Connection`."""
    return cast("Connection", recording)


@pytest.fixture
def enforcing_engine(sync_url: str) -> Iterator[Engine]:
    """A synchronous engine carrying the application's pragmas, so enforcement starts on."""
    engine = create_engine(sync_url)
    event.listen(engine, "connect", apply_sqlite_pragmas)
    try:
        yield engine
    finally:
        engine.dispose()


def test_migration_integrity_error_is_a_runtime_error() -> None:
    """A dedicated type is what lets a test be precise instead of matching on a string."""
    assert issubclass(MigrationIntegrityError, RuntimeError)


def test_disable_foreign_key_enforcement_turns_enforcement_off(
    database_url: str,
    enforcing_engine: Engine,
) -> None:
    """Read back from the connection, not asserted against the statement that was sent."""
    upgrade_to_head(database_url)

    with enforcing_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        # That read autobegan a transaction, and the guard may not change the isolation
        # level while one is open -- which is the same reason it runs before the
        # migration transaction rather than inside a revision.
        connection.rollback()

        disable_foreign_key_enforcement(connection)

        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0


def test_disable_foreign_key_enforcement_restores_the_isolation_level(
    database_url: str,
    enforcing_engine: Engine,
) -> None:
    """AUTOCOMMIT is a means, not an end; the migration transaction still has to work."""
    upgrade_to_head(database_url)

    with enforcing_engine.connect() as connection:
        before = connection.get_isolation_level()
        disable_foreign_key_enforcement(connection)

        assert connection.get_isolation_level() == before


def test_disable_foreign_key_enforcement_switches_to_autocommit_before_writing() -> None:
    """`PRAGMA foreign_keys` is a documented no-op inside a transaction."""
    recording = RecordingConnection()

    disable_foreign_key_enforcement(as_connection(recording))

    assert recording.options_seen[0] == {"isolation_level": "AUTOCOMMIT"}
    assert recording.statements[0] == "PRAGMA foreign_keys=OFF"
    assert recording.statements[1] == "PRAGMA foreign_keys"
    assert recording.commits == 1
    assert recording.options_seen[-1] == {"isolation_level": "SERIALIZABLE"}


def test_disable_foreign_key_enforcement_raises_when_the_pragma_does_not_take() -> None:
    """Continuing here would mean a batch rebuild deleting every referencing row."""
    recording = RecordingConnection(enforcement_after_switch=1)

    with pytest.raises(MigrationIntegrityError, match="still reports enforcement"):
        disable_foreign_key_enforcement(as_connection(recording))


def test_assert_no_dangling_foreign_keys_passes_on_a_clean_database(
    database_url: str,
    sync_engine: Engine,
) -> None:
    upgrade_to_head(database_url)

    with sync_engine.begin() as connection:
        connection.exec_driver_sql(A_USER)
        connection.exec_driver_sql(A_SESSION)

    with sync_engine.connect() as connection:
        # Returns nothing and raises nothing: a clean database is the silent case.
        assert_no_dangling_foreign_keys(connection)


def test_assert_no_dangling_foreign_keys_raises_and_names_the_table(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Enforcement is off for the whole run, so SQLite will not object on its own."""
    upgrade_to_head(database_url)

    with sync_engine.connect() as connection:
        # A plain engine carries no pragma listener, so enforcement is already off here,
        # exactly as it is during a migration run.
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
        connection.exec_driver_sql(ORPHAN_SESSION)

        with pytest.raises(MigrationIntegrityError, match="sessions") as raised:
            assert_no_dangling_foreign_keys(connection)

        connection.rollback()

    assert "1 dangling foreign key reference" in str(raised.value)


def test_assert_no_dangling_foreign_keys_names_every_offending_table() -> None:
    """The message has to say where to look, not merely that something is wrong."""
    recording = RecordingConnection(
        violations=[("sessions", 1, "users", 0), ("sessions", 2, "users", 0)],
    )

    with pytest.raises(MigrationIntegrityError) as raised:
        assert_no_dangling_foreign_keys(as_connection(recording))

    assert "2 dangling foreign key reference" in str(raised.value)
    assert "sessions" in str(raised.value)


@pytest.mark.parametrize("dialect_name", ["postgresql", "mysql"])
def test_the_guards_are_inert_on_a_non_sqlite_dialect(dialect_name: str) -> None:
    """Both guards exist for SQLite's quirks and must do nothing anywhere else."""
    recording = RecordingConnection(
        dialect_name, enforcement_after_switch=1, violations=[("x", 1, "y", 0)]
    )

    disable_foreign_key_enforcement(as_connection(recording))
    assert_no_dangling_foreign_keys(as_connection(recording))

    assert recording.statements == []
    assert recording.options_seen == []
    assert recording.commits == 0
