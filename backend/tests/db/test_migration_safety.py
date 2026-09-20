"""End-to-end: a real migration run, through the real `env.py`, on a real file.

These tests reach `do_run_migrations` -- the guards, the explicit transaction and the
integrity check together -- which no unit test can. They do it by copying the packaged
migrations into `tmp_path` and adding a revision to the copy. The packaged `versions/`
directory is deliberately left alone: a third revision in it would be picked up by
`test_the_revision_history_is_linear` and by the drift check, and the suite would start
failing for reasons that have nothing to do with the code.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from portfolio.db.alembic_config import DATABASE_URL_ATTRIBUTE, MIGRATIONS_DIR
from portfolio.db.migration_guards import MigrationIntegrityError

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

SEED_REVISION = "0002_seed_assets"

A_USER = (
    "INSERT INTO users (id, username, password_hash, created_at) "
    "VALUES (1, 'owner', 'x', '2026-01-01')"
)
A_SESSION = (
    "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
    "VALUES (1, 'valid-token', '2026-01-01', '2026-01-01', '2026-01-01')"
)

# Forces the batch path that drops and re-creates the table, which is the whole point.
REBUILD_USERS = '''"""Rebuild a table that another table references."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_rebuild_users"
down_revision = "0002_seed_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    pass
'''

LEAVE_AN_ORPHAN = '''"""Insert a row whose foreign key points at nothing."""

from __future__ import annotations

from alembic import op

revision = "0003_leave_an_orphan"
down_revision = "0002_seed_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
        "VALUES (4242, 'orphan-token', '2026-01-01', '2026-01-01', '2026-01-01')"
    )


def downgrade() -> None:
    pass
'''

CREATE_A_TABLE = '''"""Succeed, so that the next revision has something to undo."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_create_a_table"
down_revision = "0002_seed_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "only_if_the_whole_run_commits",
        sa.Column("id", sa.Integer(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("only_if_the_whole_run_commits")
'''

THEN_LEAVE_AN_ORPHAN = '''"""Fail, after the previous revision has already done its work."""

from __future__ import annotations

from alembic import op

revision = "0004_then_leave_an_orphan"
down_revision = "0003_create_a_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at) "
        "VALUES (4242, 'orphan-token', '2026-01-01', '2026-01-01', '2026-01-01')"
    )


def downgrade() -> None:
    pass
'''


@pytest.fixture
def migrations_copy(tmp_path: Path) -> Path:
    """The packaged migrations, copied so a test can add a revision to them safely."""
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def add_revision(migrations: Path, filename: str, source: str) -> None:
    """Drop a revision script into the copied tree."""
    (migrations / "versions" / filename).write_text(source, encoding="utf-8")


def config_for(migrations: Path, database_url: str) -> Config:
    """A `Config` shaped exactly like `build_alembic_config`, but pointed at the copy."""
    config = Config()
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("path_separator", "os")
    config.attributes[DATABASE_URL_ATTRIBUTE] = database_url
    return config


def stamped_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        stamped = connection.scalar(text("SELECT version_num FROM alembic_version"))
    return None if stamped is None else str(stamped)


def count(engine: Engine, statement: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text(statement)) or 0)


def test_a_batch_rebuild_preserves_rows_in_a_referencing_table(
    database_url: str,
    sync_engine: Engine,
    migrations_copy: Path,
) -> None:
    """The severe one: rebuilding `users` used to empty `sessions` and report success.

    Batch mode rebuilds by `DROP TABLE users`, and SQLite treats a drop as an implicit
    `DELETE FROM`, which fires `ON DELETE CASCADE` on every referencing row.
    """
    add_revision(migrations_copy, "v0003_rebuild_users.py", REBUILD_USERS)
    command.upgrade(config_for(migrations_copy, database_url), SEED_REVISION)
    with sync_engine.begin() as connection:
        connection.exec_driver_sql(A_USER)
        connection.exec_driver_sql(A_SESSION)
    assert count(sync_engine, "SELECT COUNT(*) FROM sessions") == 1

    command.upgrade(config_for(migrations_copy, database_url), "head")

    assert count(sync_engine, "SELECT COUNT(*) FROM sessions") == 1
    assert count(sync_engine, "SELECT COUNT(*) FROM users") == 1
    assert stamped_revision(sync_engine) == "0003_rebuild_users"


def test_a_batch_rebuild_under_enforcement_would_delete_the_child_rows(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """The failure the guard prevents, reproduced directly so the test above can fail.

    This performs by hand what batch mode does -- drop the parent table -- with
    enforcement on. If SQLite ever stops cascading on `DROP TABLE`, this goes red and
    the guard above becomes unnecessary rather than merely untested.
    """
    from portfolio.db.alembic_config import upgrade_to_head

    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        connection.exec_driver_sql(A_USER)
        connection.exec_driver_sql(A_SESSION)

    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        connection.exec_driver_sql("DROP TABLE users")
        survivors = connection.exec_driver_sql("SELECT COUNT(*) FROM sessions").scalar()
        connection.rollback()

    assert survivors == 0, "SQLite no longer cascades on DROP TABLE; re-read the guard"


def test_a_migration_that_leaves_an_orphan_is_refused(
    database_url: str,
    sync_engine: Engine,
    migrations_copy: Path,
) -> None:
    """Enforcement is off for the run, so the deferred check is the only thing looking."""
    add_revision(migrations_copy, "v0003_leave_an_orphan.py", LEAVE_AN_ORPHAN)
    command.upgrade(config_for(migrations_copy, database_url), SEED_REVISION)

    with pytest.raises(MigrationIntegrityError, match="dangling foreign key"):
        command.upgrade(config_for(migrations_copy, database_url), "head")

    assert stamped_revision(sync_engine) == SEED_REVISION
    assert (
        count(sync_engine, "SELECT COUNT(*) FROM sessions WHERE token_hash = 'orphan-token'") == 0
    )


def test_a_failed_upgrade_leaves_the_earlier_revision_unapplied(
    database_url: str,
    sync_engine: Engine,
    migrations_copy: Path,
) -> None:
    """A multi-revision upgrade has to be all or nothing, schema included.

    `do_run_migrations` opens an explicit `connection.begin()` before configuring the
    context precisely so that a later failure undoes everything an earlier revision did.
    """
    add_revision(migrations_copy, "v0003_create_a_table.py", CREATE_A_TABLE)
    add_revision(migrations_copy, "v0004_then_leave_an_orphan.py", THEN_LEAVE_AN_ORPHAN)
    command.upgrade(config_for(migrations_copy, database_url), SEED_REVISION)

    with pytest.raises(MigrationIntegrityError, match="dangling foreign key"):
        command.upgrade(config_for(migrations_copy, database_url), "head")

    assert stamped_revision(sync_engine) == SEED_REVISION
    tables = set(inspect(sync_engine).get_table_names())
    assert "only_if_the_whole_run_commits" not in tables, (
        "0003 created the table and 0004 failed, but the table survived the rollback. "
        "pysqlite opens a transaction for DML only, never for DDL, so the explicit "
        "connection.begin() does not bracket CREATE TABLE. alembic_version is back at "
        "0002 while the schema is at 0003, and every later `upgrade head` now dies with "
        "'table already exists'."
    )


def test_the_database_can_still_be_upgraded_after_a_refused_run(
    database_url: str,
    database_path: Path,
    migrations_copy: Path,
) -> None:
    """A refusal must leave a database the next deploy can still migrate."""
    add_revision(migrations_copy, "v0003_create_a_table.py", CREATE_A_TABLE)
    add_revision(migrations_copy, "v0004_then_leave_an_orphan.py", THEN_LEAVE_AN_ORPHAN)
    command.upgrade(config_for(migrations_copy, database_url), SEED_REVISION)
    with pytest.raises(MigrationIntegrityError):
        command.upgrade(config_for(migrations_copy, database_url), "head")

    # The operator removes the bad revision and redeploys with only the good one.
    (migrations_copy / "versions" / "v0004_then_leave_an_orphan.py").unlink()
    command.upgrade(config_for(migrations_copy, database_url), "head")

    retry_engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert stamped_revision(retry_engine) == "0003_create_a_table"
    finally:
        retry_engine.dispose()
