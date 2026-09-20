"""Migrations: criteria 1, 2 and 6, plus the seed data and batch mode.

Every test in this module is synchronous on purpose. `upgrade_to_head` and
`downgrade_to_base` call Alembic's `command`, whose async `env.py` calls `asyncio.run`;
called from inside a running loop that raises `RuntimeError`. A test that needs the
schema from within an event loop uses the `migrated_engine` fixture instead, which hops
through a worker thread the way the application's lifespan does.

The schema is always read back through a second, unconfigured synchronous engine. The
question these tests answer is what is on disk, not what the migration believed it wrote.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from alembic import context as alembic_context
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, Table, inspect, select, text
from sqlalchemy.orm import Session as SyncSession

from portfolio.db.alembic_config import (
    DATABASE_URL_ATTRIBUTE,
    MIGRATIONS_DIR,
    build_alembic_config,
    downgrade_to_base,
    upgrade_to_head,
)
from portfolio.db.base import NAMING_CONVENTION
from portfolio.db.models import Asset, metadata

if TYPE_CHECKING:
    from sqlalchemy import Engine

APPLICATION_TABLES = frozenset({"users", "sessions", "assets"})
FIRST_REVISION = "0001_initial_schema"

EXPECTED_SEED_ROWS = [
    ("BTC", "Bitcoin", 8, "crypto"),
    ("KAS", "Kaspa", 8, "crypto"),
    ("USDT", "Tether", 6, "crypto"),
]

# Exactly the names `NAMING_CONVENTION` produces, as they must appear in the DDL the
# migration actually ran. A batch rebuild cannot re-create a constraint it cannot name.
EXPECTED_CONSTRAINT_NAMES = {
    "users": {"pk_users", "uq_users_username"},
    "assets": {"pk_assets", "uq_assets_symbol", "ck_assets_kind"},
    "sessions": {"pk_sessions", "uq_sessions_token_hash", "fk_sessions_user_id_users"},
}


def table_names(engine: Engine) -> set[str]:
    """Reflect the table names currently on disk."""
    return set(inspect(engine).get_table_names())


def seed_rows(engine: Engine) -> list[tuple[str, str, int, str]]:
    """Read the `assets` rows back through the mapped class."""
    with SyncSession(engine) as session:
        assets = session.scalars(select(Asset).order_by(Asset.symbol)).all()
        return [(asset.symbol, asset.name, asset.decimals, asset.kind) for asset in assets]


def test_upgrade_head_creates_every_table(database_url: str, sync_engine: Engine) -> None:
    """Criterion 1: head builds the schema from an empty database file."""
    upgrade_to_head(database_url)

    names = table_names(sync_engine)

    assert names >= APPLICATION_TABLES
    assert "alembic_version" in names


def test_upgrade_head_stamps_the_latest_revision(database_url: str, sync_engine: Engine) -> None:
    """A schema at head that is not stamped at head migrates itself again next boot."""
    upgrade_to_head(database_url)

    with sync_engine.connect() as connection:
        stamped = connection.scalar(text("SELECT version_num FROM alembic_version"))
    head = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()

    assert stamped == head


def test_downgrade_base_leaves_no_application_tables(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Criterion 2: every revision is reversible, all the way down."""
    upgrade_to_head(database_url)

    downgrade_to_base(database_url)

    assert table_names(sync_engine) & APPLICATION_TABLES == set()


def test_upgrade_downgrade_upgrade_round_trip(database_url: str, sync_engine: Engine) -> None:
    """A downgrade that leaves debris behind only fails on the *second* upgrade."""
    upgrade_to_head(database_url)
    downgrade_to_base(database_url)
    upgrade_to_head(database_url)

    assert table_names(sync_engine) >= APPLICATION_TABLES
    assert seed_rows(sync_engine) == EXPECTED_SEED_ROWS


def test_asset_seed_rows(database_url: str, sync_engine: Engine) -> None:
    """The seed migration inserts exactly the three symbols the first release reads."""
    upgrade_to_head(database_url)

    assert seed_rows(sync_engine) == EXPECTED_SEED_ROWS

    with SyncSession(sync_engine) as session:
        created = [asset.created_at for asset in session.scalars(select(Asset)).all()]
    # Written through `UtcDateTime`, so every seeded timestamp comes back aware and UTC.
    assert created
    assert all(timestamp.utcoffset() == timedelta(0) for timestamp in created)


def test_asset_seed_downgrade_removes_the_rows(database_url: str, sync_engine: Engine) -> None:
    """The seed downgrade deletes its own rows and leaves the table standing."""
    upgrade_to_head(database_url)

    command.downgrade(build_alembic_config(database_url), FIRST_REVISION)

    assert "assets" in table_names(sync_engine)
    assert seed_rows(sync_engine) == []


def test_the_seed_downgrade_leaves_other_rows_alone(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """`DELETE FROM assets` would take a user's row with it; the migration must not."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO assets (symbol, name, decimals, kind, created_at) "
                "VALUES ('TEST', 'Test asset', 2, 'crypto', '2026-01-01 00:00:00')"
            )
        )

    command.downgrade(build_alembic_config(database_url), FIRST_REVISION)

    assert seed_rows(sync_engine) == [("TEST", "Test asset", 2, "crypto")]


def test_the_migrated_schema_carries_the_convention_names(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Criterion-adjacent: an anonymous constraint cannot survive a batch rebuild."""
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    for table, expected in EXPECTED_CONSTRAINT_NAMES.items():
        found = {inspector.get_pk_constraint(table)["name"]}
        found |= {unique["name"] for unique in inspector.get_unique_constraints(table)}
        found |= {check["name"] for check in inspector.get_check_constraints(table)}
        found |= {foreign["name"] for foreign in inspector.get_foreign_keys(table)}
        assert found == expected, table

    assert {index["name"] for index in inspector.get_indexes("sessions")} == {"ix_sessions_user_id"}


def test_models_and_migrations_have_not_drifted(database_url: str, sync_engine: Engine) -> None:
    """Criterion 6: the migrations and the models describe the same schema.

    `compare_type` and `render_as_batch` mirror `env.py` exactly. There is deliberately
    no `include_object` filter: nothing is being excluded from the comparison, so a
    column that only exists in one of the two places has nowhere to hide.
    """
    upgrade_to_head(database_url)

    with sync_engine.connect() as connection:
        migration_context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "render_as_batch": True},
        )
        differences = compare_metadata(migration_context, metadata)

    assert differences == []


def test_the_drift_check_detects_a_deliberate_difference(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """A drift check that cannot fail is worth nothing, so prove that it can.

    The metadata handed to the comparison is the real one plus one table the migrations
    never create. If this returns no differences, the test above is vacuous.
    """
    upgrade_to_head(database_url)
    drifted = MetaData(naming_convention=NAMING_CONVENTION)
    for table in metadata.tables.values():
        table.to_metadata(drifted)
    Table("a_table_no_migration_creates", drifted, Column("id", Integer, primary_key=True))

    with sync_engine.connect() as connection:
        migration_context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "render_as_batch": True},
        )
        differences = compare_metadata(migration_context, drifted)

    assert differences != []
    assert any("a_table_no_migration_creates" in repr(difference) for difference in differences)


def test_env_runs_with_render_as_batch(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite cannot `ALTER COLUMN`; without batch mode a column change is unwritable.

    The assertion is on the options `env.py` actually hands Alembic during a real
    migration run, not on the text of `env.py`.
    """
    captured: list[dict[str, Any]] = []
    real_configure = alembic_context.configure

    def spy(**options: Any) -> Any:
        captured.append(options)
        return real_configure(**options)

    monkeypatch.setattr(alembic_context, "configure", spy)

    upgrade_to_head(database_url)

    assert captured, "env.py never configured a migration context, so it never ran"
    assert all(options["render_as_batch"] for options in captured)
    assert all(options["compare_type"] for options in captured)
    assert all(options["target_metadata"] is metadata for options in captured)


def test_the_config_carries_the_url_rather_than_the_ini(database_url: str) -> None:
    """One source of truth for the database URL, and it is not `alembic.ini`."""
    config = build_alembic_config(database_url)

    assert config.attributes[DATABASE_URL_ATTRIBUTE] == database_url
    assert config.get_main_option("sqlalchemy.url") is None
    assert config.get_main_option("script_location") == str(MIGRATIONS_DIR)


def test_the_migrations_ship_inside_the_package() -> None:
    """The image copies `backend/src`; migrations outside it would not exist in prod."""
    assert MIGRATIONS_DIR.is_dir()
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert (MIGRATIONS_DIR / "versions").is_dir()
    assert MIGRATIONS_DIR.parts[-3:] == ("portfolio", "db", "migrations")


def test_the_revision_history_is_linear() -> None:
    """Two heads is a merge conflict that only shows up at `upgrade head`."""
    script_directory = ScriptDirectory(str(MIGRATIONS_DIR))

    assert len(script_directory.get_heads()) == 1


@pytest.mark.parametrize("table", sorted(APPLICATION_TABLES))
def test_every_model_table_is_created_by_a_migration(
    database_url: str,
    sync_engine: Engine,
    table: str,
) -> None:
    """A model with no migration behind it is a table that never exists in production."""
    upgrade_to_head(database_url)

    assert table in table_names(sync_engine)
    assert table in metadata.tables
