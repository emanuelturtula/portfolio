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
import structlog
from sqlalchemy import create_engine, event, text
from structlog.testing import capture_logs

from portfolio.config import Settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import apply_sqlite_pragmas
from portfolio.db.migration_guards import (
    MigrationIntegrityError,
    assert_no_dangling_foreign_keys,
    disable_foreign_key_enforcement,
    foreign_key_violations,
    snapshot_foreign_key_violations,
)
from portfolio.logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

# Invented, unique, and asserted absent from every rendered log line. This goes into
# `sessions.token_hash`, a real column on the table these tests damage, so a guard that
# logged the offending row would put this exact string into the log stream.
SENTINEL_COLUMN_VALUE = "sentinel-value-that-must-never-be-logged"
SENTINEL_USER_ID = 424242
INTRODUCED_USER_ID = 515151

ORPHAN_SESSION = (
    "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
    "VALUES (4242, 'orphan-token', '2026-01-01', '2026-01-01', '2026-01-01')"
)

# Bound parameters rather than a formatted string: the values under test include one the
# whole point is to track, and building SQL around it invites quoting bugs in the test
# itself rather than in the code.
INSERT_ORPHAN = text(
    "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
    "VALUES (:user_id, :token_hash, '2026-01-01', '2026-01-01', '2026-01-01')"
)
DELETE_BY_USER = text("DELETE FROM sessions WHERE user_id = :user_id")

A_USER = (
    "INSERT INTO users (id, username, password_hash, created_at) "
    "VALUES (1, 'owner', 'x', '2026-01-01')"
)
A_SESSION = (
    "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
    "VALUES (1, 'valid-token', '2026-01-01', '2026-01-01', '2026-01-01')"
)


def insert_orphan(connection: Connection, user_id: int, token_hash: str) -> None:
    """Add a `sessions` row whose `user_id` points at a `users` row that does not exist."""
    connection.execute(INSERT_ORPHAN, {"user_id": user_id, "token_hash": token_hash})


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


def test_a_pre_existing_orphan_does_not_block_a_migration_run(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """A damaged file must not wedge the deploy, because the rollback cannot fix it.

    Every startup runs this check and the overwhelmingly common startup applies no
    migrations at all. Without a baseline, a database that arrived broken -- a restored
    backup, a torn WAL copy, `sqlite3` surgery on the Pi -- makes the container refuse to
    start, `deploy.py` roll back, and the previous image refuse identically, on the only
    copy of the trade history.
    """
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_orphan(connection, SENTINEL_USER_ID, SENTINEL_COLUMN_VALUE)

    with sync_engine.connect() as connection:
        pre_existing = snapshot_foreign_key_violations(connection)
        assert_no_dangling_foreign_keys(connection, pre_existing)

    assert sum(pre_existing.values()) == 1


def test_the_same_orphan_without_a_baseline_is_still_refused(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Omitting the baseline means "assume the database started clean"."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_orphan(connection, SENTINEL_USER_ID, SENTINEL_COLUMN_VALUE)

    with sync_engine.connect() as connection, pytest.raises(MigrationIntegrityError):
        assert_no_dangling_foreign_keys(connection)


def test_a_pre_existing_orphan_is_reported_rather_than_passed_over(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Tolerated is not the same as unnoticed: the operator has to be told."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_orphan(connection, SENTINEL_USER_ID, SENTINEL_COLUMN_VALUE)

    with capture_logs() as captured, sync_engine.connect() as connection:
        snapshot_foreign_key_violations(connection)

    warnings = [event for event in captured if event["log_level"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["event"] == "pre_existing_foreign_key_violations"
    assert warnings[0]["table"] == "sessions"
    assert warnings[0]["count"] == 1
    # The exact key set, not merely the presence of these two: an extra field is how a
    # column value would arrive in a log record.
    assert set(warnings[0]) == {"event", "log_level", "table", "count", "detail"}


def test_the_pre_existing_warning_carries_no_column_value(
    database_url: str,
    sync_engine: Engine,
    restored_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rule 3: `sessions.token_hash` must not reach a log record.

    The guard reads the offending row's foreign key values to build the identity it
    compares on, so the values exist in memory a few frames from the log call. This
    renders through the real production pipeline and asserts they did not travel.

    Redaction would not save us here. It matches on the *key* name, and a value logged
    under a field called `values` or `identity` is not a name it recognises --
    `test_a_value_logged_under_an_innocuous_key_would_reach_stdout` proves that.
    """
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_orphan(connection, SENTINEL_USER_ID, SENTINEL_COLUMN_VALUE)
    configure_logging(Settings(environment="prod"))

    with sync_engine.connect() as connection:
        snapshot_foreign_key_violations(connection)

    written = capsys.readouterr().out

    assert "pre_existing_foreign_key_violations" in written
    assert "sessions" in written
    assert SENTINEL_COLUMN_VALUE not in written
    assert str(SENTINEL_USER_ID) not in written


def test_a_value_logged_under_an_innocuous_key_would_reach_stdout(
    restored_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The discriminating half of the test above: prove the assertion can fail.

    `detail` is not a name the redaction processor treats as sensitive, which is exactly
    the point -- nothing downstream would catch a column value logged under a key like
    this, so the guard itself has to not pass one.
    """
    configure_logging(Settings(environment="prod"))

    structlog.get_logger("test").warning("deliberate_leak", detail=SENTINEL_COLUMN_VALUE)

    assert SENTINEL_COLUMN_VALUE in capsys.readouterr().out


def test_a_run_introduced_orphan_is_refused_even_with_a_baseline(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Tolerating what arrived broken must not tolerate what the run broke."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_orphan(connection, SENTINEL_USER_ID, SENTINEL_COLUMN_VALUE)

    with sync_engine.connect() as connection:
        pre_existing = snapshot_foreign_key_violations(connection)
        insert_orphan(connection, INTRODUCED_USER_ID, "introduced-value")

        with pytest.raises(MigrationIntegrityError) as raised:
            assert_no_dangling_foreign_keys(connection, pre_existing)

        connection.rollback()

    message = str(raised.value)
    assert "introduced 1 dangling foreign key reference" in message
    assert "arrived with 1 dangling" in message
    assert "sessions" in message


def test_repairing_one_orphan_and_introducing_another_is_refused(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """The swap: identical totals before and after, and a count comparison would commit it.

    This is why the baseline is a `Counter` of whole identities rather than a number.
    """
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_orphan(connection, SENTINEL_USER_ID, SENTINEL_COLUMN_VALUE)

    with sync_engine.connect() as connection:
        pre_existing = snapshot_foreign_key_violations(connection)
        connection.execute(DELETE_BY_USER, {"user_id": SENTINEL_USER_ID})
        insert_orphan(connection, INTRODUCED_USER_ID, "introduced-value")
        after = foreign_key_violations(connection)

        assert sum(after.values()) == sum(pre_existing.values())

        with pytest.raises(MigrationIntegrityError, match="introduced 1 dangling"):
            assert_no_dangling_foreign_keys(connection, pre_existing)

        connection.rollback()


def test_a_violation_is_identified_by_its_values_not_by_its_rowid(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """The reason rowid is excluded, proved directly rather than inferred.

    Batch mode rebuilds a table with `INSERT ... SELECT`, which renumbers rowids unless
    the primary key aliases them. Renumbering here stands in for that rebuild: if rowid
    were part of the identity, the same orphan would subtract to nothing and every
    pre-existing orphan in a rebuilt table would look brand new.
    """
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_orphan(connection, SENTINEL_USER_ID, SENTINEL_COLUMN_VALUE)
    with sync_engine.connect() as connection:
        before = foreign_key_violations(connection)

    with sync_engine.begin() as connection:
        connection.exec_driver_sql("UPDATE sessions SET id = id + 1000")
    with sync_engine.connect() as connection:
        after = foreign_key_violations(connection)

    assert before == after
    assert not (after - before)
    assert list(before) == [("sessions", "users", 0, (SENTINEL_USER_ID,))]


def test_two_identical_orphans_are_counted_not_collapsed(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """A set would make repairing one of two identical orphans look like a clean run."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_orphan(connection, SENTINEL_USER_ID, "first-value")
        insert_orphan(connection, SENTINEL_USER_ID, "second-value")

    with sync_engine.connect() as connection:
        violations = foreign_key_violations(connection)

    assert violations[("sessions", "users", 0, (SENTINEL_USER_ID,))] == 2


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
