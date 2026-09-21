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
from typing import TYPE_CHECKING, Any, Final

import pytest
from alembic import command
from alembic import context as alembic_context
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    Column,
    Integer,
    MetaData,
    Table,
    Text,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Session as SyncSession

from portfolio.db.alembic_config import (
    DATABASE_URL_ATTRIBUTE,
    MIGRATIONS_DIR,
    build_alembic_config,
    downgrade_to_base,
    upgrade_to_head,
)
from portfolio.db.base import NAMING_CONVENTION
from portfolio.db.models import _ASSET_KIND_CHECK, Asset, metadata

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

APPLICATION_TABLES = frozenset({"users", "sessions", "assets", "wallets"})
"""Every table the application owns, compared **exactly** rather than with `>=`.

`>=` was the original spelling and it covered less than it looked like it did: a table a
migration created but nobody listed here satisfied it, so the list could fall behind the
schema without a single test noticing. Exact comparison means the next migration either
updates this set or fails, which is what the set was presumably always meant to guarantee.

`alembic_version` is Alembic's own bookkeeping and is added where a live schema is
compared, rather than being listed here as though the application owned it.
"""

STAMP_TABLE = "alembic_version"
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
    "wallets": {
        "pk_wallets",
        "uq_wallets_user_chain_address",
        "ck_wallets_chain_key",
        "fk_wallets_user_id_users",
    },
}


def table_names(engine: Engine) -> set[str]:
    """Reflect the table names currently on disk."""
    return set(inspect(engine).get_table_names())


def seed_rows(engine: Engine) -> list[tuple[str, str, int, str]]:
    """Read the `assets` rows back through the mapped class."""
    with SyncSession(engine) as session:
        assets = session.scalars(select(Asset).order_by(Asset.symbol)).all()
        return [(asset.symbol, asset.name, asset.decimals, asset.kind) for asset in assets]


# The one place the comparison options are written down. Both the drift check and the
# test that proves the drift check can fail go through `compare_against`, because two
# duplicated option dicts are bound together by nothing: quieting the real check with an
# `include_object` filter would leave its companion passing and still claiming the check
# was live.
COMPARISON_OPTIONS: Final[dict[str, bool]] = {"compare_type": True, "render_as_batch": True}


def compare_against(connection: Connection, target: MetaData) -> list[Any]:
    """Diff a live database against a metadata, exactly as the drift check does."""
    migration_context = MigrationContext.configure(connection, opts=dict(COMPARISON_OPTIONS))
    return list(compare_metadata(migration_context, target))


def a_copy_of_the_real_metadata() -> MetaData:
    """The real schema, detached, so a test can bend it without touching the models."""
    copied = MetaData(naming_convention=NAMING_CONVENTION)
    for table in metadata.tables.values():
        table.to_metadata(copied)
    return copied


def with_an_extra_table() -> MetaData:
    drifted = a_copy_of_the_real_metadata()
    Table("a_table_no_migration_creates", drifted, Column("id", Integer, primary_key=True))
    return drifted


def with_an_added_column() -> MetaData:
    drifted = a_copy_of_the_real_metadata()
    drifted.tables["assets"].append_column(Column("a_column_no_migration_creates", Text()))
    return drifted


def with_a_changed_column_type() -> MetaData:
    """The drift `compare_type=True` exists for: same column, different type."""
    drifted = a_copy_of_the_real_metadata()
    drifted.tables["assets"].c.decimals.type = Text()
    return drifted


def normalise_sql(expression: str) -> str:
    """Collapse runs of whitespace and nothing else.

    Anything more forgiving -- stripping parentheses, folding case, ignoring quotes --
    and the comparison stops discriminating, which is the whole point of it.
    `test_the_check_constraint_comparison_discriminates` pins that down.
    """
    return " ".join(expression.split())


def test_upgrade_head_creates_every_table(database_url: str, sync_engine: Engine) -> None:
    """Criterion 1: head builds the schema from an empty database file."""
    upgrade_to_head(database_url)

    names = table_names(sync_engine)

    assert names == APPLICATION_TABLES | {STAMP_TABLE}


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

    # Exactly the stamp table and nothing else: a downgrade that left one table behind
    # would satisfy "none of the application's tables remain" only until the next release
    # added a table it also forgot to drop.
    assert table_names(sync_engine) == {STAMP_TABLE}


def test_upgrade_downgrade_upgrade_round_trip(database_url: str, sync_engine: Engine) -> None:
    """A downgrade that leaves debris behind only fails on the *second* upgrade."""
    upgrade_to_head(database_url)
    downgrade_to_base(database_url)
    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}
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

    # Exact, for the same reason `APPLICATION_TABLES` is: a table missing from this map
    # is a table whose constraint names nothing checks.
    assert set(EXPECTED_CONSTRAINT_NAMES) == APPLICATION_TABLES

    for table, expected in EXPECTED_CONSTRAINT_NAMES.items():
        found = {inspector.get_pk_constraint(table)["name"]}
        found |= {unique["name"] for unique in inspector.get_unique_constraints(table)}
        found |= {check["name"] for check in inspector.get_check_constraints(table)}
        found |= {foreign["name"] for foreign in inspector.get_foreign_keys(table)}
        assert found == expected, table

    assert {index["name"] for index in inspector.get_indexes("sessions")} == {"ix_sessions_user_id"}
    assert {index["name"] for index in inspector.get_indexes("wallets")} == {"ix_wallets_user_id"}


def test_models_and_migrations_have_not_drifted(database_url: str, sync_engine: Engine) -> None:
    """Criterion 6: the migrations and the models describe the same schema.

    `compare_type` and `render_as_batch` mirror `env.py` exactly, and they come from
    `COMPARISON_OPTIONS` rather than from a literal here, so the companion test below
    cannot drift away from this one. There is deliberately no `include_object` filter:
    nothing is excluded, so a column that exists in only one of the two has nowhere to
    hide. Check constraints are the documented exception -- see
    `test_the_kind_check_constraint_matches_the_model`.
    """
    upgrade_to_head(database_url)

    with sync_engine.connect() as connection:
        differences = compare_against(connection, metadata)

    assert differences == []


@pytest.mark.parametrize(
    ("build_drift", "expected_operation", "expected_subject"),
    [
        (with_an_extra_table, "add_table", "a_table_no_migration_creates"),
        (with_an_added_column, "add_column", "a_column_no_migration_creates"),
        (with_a_changed_column_type, "modify_type", "decimals"),
    ],
    ids=["an extra table", "an added column", "a changed column type"],
)
def test_the_drift_check_detects_a_deliberate_difference(
    database_url: str,
    sync_engine: Engine,
    build_drift: Callable[[], MetaData],
    expected_operation: str,
    expected_subject: str,
) -> None:
    """A drift check that cannot fail is worth nothing, so prove that it can.

    An extra table is the weakest possible drift -- it would be caught even with type
    comparison switched off. The added column and the changed type are what the real
    options actually buy, and they go through the same `compare_against` helper as the
    test above, so the two cannot be quietly configured differently.
    """
    upgrade_to_head(database_url)

    with sync_engine.connect() as connection:
        differences = compare_against(connection, build_drift())

    assert differences != []
    rendered = repr(differences)
    assert expected_operation in rendered
    assert expected_subject in rendered


def test_type_comparison_is_load_bearing_not_decorative(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Turn type comparison off and a column with the wrong type becomes invisible.

    `compare_type` has defaulted to `True` since Alembic 1.12, so declaring it in
    `COMPARISON_OPTIONS` and in `env.py` is belt and braces rather than strictly
    required. It is still worth pinning: this is what the option buys, and a future
    Alembic that flipped the default back would otherwise silently blind the check.
    """
    upgrade_to_head(database_url)
    drifted = with_a_changed_column_type()

    with sync_engine.connect() as connection:
        blind_context = MigrationContext.configure(
            connection,
            opts={"render_as_batch": True, "compare_type": False},
        )
        without_type_comparison = list(compare_metadata(blind_context, drifted))
        with_type_comparison = compare_against(connection, drifted)

    assert without_type_comparison == []
    assert with_type_comparison != []
    assert COMPARISON_OPTIONS["compare_type"] is True


def test_the_kind_check_constraint_matches_the_model(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Autogenerate has no check-constraint comparator, so the drift test cannot see this.

    Editing `_ASSET_KIND_CHECK` without writing the matching migration passes ruff, mypy,
    the layering contract, every other test here *and* the drift check, and then fails in
    production with `CHECK constraint failed: ck_assets_kind`. This is the only thing
    looking.
    """
    upgrade_to_head(database_url)

    reflected = {
        str(constraint["name"]): str(constraint["sqltext"])
        for constraint in inspect(sync_engine).get_check_constraints("assets")
    }

    assert set(reflected) == {"ck_assets_kind"}
    assert normalise_sql(reflected["ck_assets_kind"]) == normalise_sql(_ASSET_KIND_CHECK)


def test_the_check_constraint_comparison_discriminates() -> None:
    """Whitespace is normalised; content is not. Without this the test above is hollow."""
    assert normalise_sql("kind   IN\n\t('crypto', 'fiat')") == normalise_sql(_ASSET_KIND_CHECK)
    assert normalise_sql("kind IN ('crypto')") != normalise_sql(_ASSET_KIND_CHECK)
    assert normalise_sql("kind IN ('crypto', 'fiat', 'equity')") != normalise_sql(_ASSET_KIND_CHECK)
    assert normalise_sql("kind IN ('CRYPTO', 'FIAT')") != normalise_sql(_ASSET_KIND_CHECK)


def test_the_drift_check_is_blind_to_a_changed_check_constraint(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """The justification for the bespoke test above, pinned so it can expire.

    If Alembic ever grows a check-constraint comparator this goes red, and at that point
    `test_the_kind_check_constraint_matches_the_model` can be deleted in favour of the
    drift check. Until then, deleting it would remove the only guard there is.
    """
    upgrade_to_head(database_url)
    drifted = a_copy_of_the_real_metadata()
    assets = drifted.tables["assets"]
    for constraint in list(assets.constraints):
        if isinstance(constraint, CheckConstraint):
            assets.constraints.discard(constraint)
    assets.append_constraint(CheckConstraint("kind IN ('crypto')", name="kind"))

    with sync_engine.connect() as connection:
        differences = compare_against(connection, drifted)

    assert differences == [], (
        "Alembic now compares check constraints; the bespoke sqltext test can be retired"
    )


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
