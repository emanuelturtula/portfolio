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
from portfolio.db.models import (
    _ASSET_KIND_CHECK,
    _BALANCE_SNAPSHOT_CONFIRMED_CHECK,
    _EXCHANGE_OPERATION_KIND_CHECK,
    _EXCHANGE_OPERATION_MANUAL_CHECK,
    _PRICE_HISTORY_BASIS_CHECK,
    _PRICE_QUOTE_CURRENCY_CHECK,
    _RECONSTRUCTED_BALANCE_CONFIRMED_CHECK,
    _SYNC_RUN_CHAIN_ERROR_KIND_CHECK,
    _SYNC_RUN_CHAIN_STATUS_CHECK,
    _SYNC_RUN_STATUS_CHECK,
    _SYNC_RUN_TRIGGER_CHECK,
    _WALLET_CHAIN_KEY_CHECK,
    Asset,
    metadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

APPLICATION_TABLES = frozenset(
    {
        "users",
        "sessions",
        "assets",
        "wallets",
        "prices",
        # #10. `sync_runs` is the record of every attempt, `sync_run_chains` is which chain
        # did what within one attempt, and `balance_snapshots` is the append-only history a
        # chart is drawn from.
        "sync_runs",
        "sync_run_chains",
        "balance_snapshots",
        # #24. The addresses an extended-key wallet has derived, with whether each one has
        # ever been used. Derived from the key; kept so a rescan does not derive them again.
        "derived_addresses",
        # Spec 037. One price per asset, quote currency and UTC day, so a past day can be
        # valued; the hourly refresh and the daily backfill write it.
        "price_history",
        # Spec 038. Each wallet's closing balance per day, rebuilt from its transactions.
        "reconstructed_balances",
        # Spec 041. One closing price per asset, quote currency and UTC hour.
        "price_hourly",
        # Spec 042. Every operation of the uploaded exchange exports, and the uploads.
        "exchange_imports",
        "exchange_operations",
    }
)
"""Every table the application owns, compared **exactly** rather than with `>=`.

`>=` was the original spelling and it covered less than it looked like it did: a table a
migration created but nobody listed here satisfied it, so the list could fall behind the
schema without a single test noticing. Exact comparison means the next migration either
updates this set or fails, which is what the set was presumably always meant to guarantee.

`alembic_version` is Alembic's own bookkeeping and is added where a live schema is
compared, rather than being listed here as though the application owned it.

The exchange, accounting and adjustment tables of #12 to #104 are not here: `0012` drops them
(spec 036). They exist only between `0006_exchanges` and `0011_extended_keys`, and
`DROPPED_TABLES` names them for the tests that reach that part of the history.
"""

STAMP_TABLE = "alembic_version"
FIRST_REVISION = "0001_initial_schema"

#: The revision immediately below #9's, so the prices migration can be reversed on its own
#: rather than only as part of a walk all the way down to base. A downgrade to base drops
#: everything and would pass for a `downgrade()` that dropped the wrong table; a downgrade
#: of one step has to leave the other four standing, which is the property an operator
#: actually relies on when a release is rolled back on the Pi.
REVISION_BEFORE_PRICES = "0003_wallets"
PRICES_REVISION = "0004_prices"

#: #10's revision and its parent, for the same single-step reversal. A rollback on the Pi
#: moves one step, and one step is the only thing that can tell a `downgrade()` which drops
#: the right three tables from one which drops somebody else's.
BALANCES_REVISION = "0005_balances"
BALANCE_TABLES = frozenset({"sync_runs", "sync_run_chains", "balance_snapshots"})

#: #12's revision and its two tables, for the same single-step reversal.
EXCHANGES_REVISION = "0006_exchanges"
EXCHANGE_TABLES = frozenset({"exchange_accounts", "exchange_fills"})

#: #15's three tables. Its revision sits on top of #12's, so every single-step reversal
#: below `0007_exchange_sync` takes these down as well, and each test subtracts them.
EXCHANGE_SYNC_TABLES = frozenset(
    {"exchange_sync_windows", "exchange_sync_runs", "exchange_sync_run_accounts"}
)

#: #19's four. Its revision sits on top of #15's, so every single-step reversal below
#: `0008_accounting` takes these down as well, and each test subtracts them.
ACCOUNTING_TABLES = frozenset(
    {"accounting_snapshots", "accounting_positions", "accounting_lots", "accounting_warnings"}
)

#: #18's one. Its revision sits on top of #19's, so every single-step reversal below
#: `0009_manual_adjustments` takes it down as well, and each test subtracts it.
ADJUSTMENT_TABLES = frozenset({"manual_adjustments"})

#: #104's one. Its revision sits on top of #18's, so every single-step reversal below
#: `0010_exchange_balances` takes it down as well, and each test subtracts it.
EXCHANGE_BALANCE_TABLES = frozenset({"exchange_balances"})

#: #24's one. Its revision sits on top of #104's, so every single-step reversal below
#: `0011_extended_keys` takes it down as well, and each test subtracts it.
DERIVED_ADDRESS_TABLES = frozenset({"derived_addresses"})

#: Spec 037's one. Its revision sits on top of `0012`, so every single-step reversal below
#: `0013_price_history` takes it down as well, and each test subtracts it.
PRICE_HISTORY_TABLES = frozenset({"price_history"})

#: Spec 038's one. Its revision sits on top of `0013`, so every single-step reversal below
#: `0014_reconstructed_balances` takes it down as well, and each test subtracts it.
RECONSTRUCTED_TABLES = frozenset({"reconstructed_balances"})

#: Spec 041's one. Its revision sits on top of `0014`, so every single-step reversal below
#: `0015_price_hourly` takes it down as well, and each test subtracts it.
HOURLY_TABLES = frozenset({"price_hourly"})

#: Spec 042's two. Its revision sits on top of `0015`, so every single-step reversal below
#: `0016_exchange_operations` takes them down as well, and each test subtracts them.
OPERATION_TABLES = frozenset({"exchange_imports", "exchange_operations"})

#: Spec 036's revision, and the one below it: the last schema that has the tables it drops.
DROP_REVISION = "0012_drop_exchanges_accounting"
REVISION_BEFORE_DROP = "0011_extended_keys"
DROPPED_TABLES = (
    EXCHANGE_TABLES
    | EXCHANGE_SYNC_TABLES
    | ACCOUNTING_TABLES
    | ADJUSTMENT_TABLES
    | EXCHANGE_BALANCE_TABLES
)

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
        # #24: the batch rebuild that added `kind` has to keep every name above, and adds
        # these two. Compared with their model constants in
        # `tests/db/test_extended_keys_migration.py`.
        "ck_wallets_kind",
        "ck_wallets_kind_chain",
        "fk_wallets_user_id_users",
    },
    # #9. `uq_prices_asset_currency` is what makes this the *current* price rather than a
    # history: the refresh upserts onto it. `ck_prices_quote_currency` is the only thing
    # standing between the column and a third currency arriving without a migration --
    # and, like every other CHECK here, it is invisible to the drift check.
    "prices": {
        "pk_prices",
        "uq_prices_asset_currency",
        "ck_prices_quote_currency",
        "fk_prices_asset_id_assets",
    },
    # #10. Every CHECK here is invisible to the drift check, exactly as `ck_assets_kind`
    # is; `test_the_new_check_constraints_match_the_models` below compares each one's
    # text against the model's constant, and the repository suites exercise each with a
    # real insert. What this map adds is that the constraints exist *and are named*, which is
    # what a batch rebuild needs in order to re-create them at all.
    "sync_runs": {
        "pk_sync_runs",
        "ck_sync_runs_trigger",
        "ck_sync_runs_status",
    },
    "sync_run_chains": {
        "pk_sync_run_chains",
        "uq_sync_run_chains_run_chain",
        "ck_sync_run_chains_chain_key",
        "ck_sync_run_chains_status",
        "ck_sync_run_chains_error_kind",
        "fk_sync_run_chains_sync_run_id_sync_runs",
    },
    "balance_snapshots": {
        "pk_balance_snapshots",
        "uq_balance_snapshots_wallet_run",
        "ck_balance_snapshots_confirmed",
        "fk_balance_snapshots_wallet_id_wallets",
        "fk_balance_snapshots_sync_run_id_sync_runs",
    },
    # #24. Three CHECKs, compared with their model constants and exercised with real inserts
    # in `tests/db/test_extended_keys_migration.py`. The unique key is what makes a rescan
    # an upsert rather than a second copy of the same index.
    # Spec 037. Two CHECKs, compared with their model constants by
    # `test_the_price_history_check_constraints_match_the_model`. The unique key leads with
    # `asset_id`, so it is also the index every read uses.
    "price_history": {
        "pk_price_history",
        "uq_price_history_asset_day",
        "ck_price_history_quote_currency",
        "ck_price_history_basis",
        "fk_price_history_asset_id_assets",
    },
    # Spec 038. One CHECK, the balance's sign, compared with its constant by
    # `test_the_reconstructed_balance_check_matches_the_model`.
    "reconstructed_balances": {
        "pk_reconstructed_balances",
        "uq_reconstructed_balances_wallet_day",
        "ck_reconstructed_balances_confirmed",
        "fk_reconstructed_balances_wallet_id_wallets",
    },
    # Spec 041. One CHECK, the quote currency, compared with its constant by
    # `test_the_price_hourly_check_matches_the_model`.
    "price_hourly": {
        "pk_price_hourly",
        "uq_price_hourly_asset_hour",
        "ck_price_hourly_quote_currency",
        "fk_price_hourly_asset_id_assets",
    },
    # Spec 042. Two CHECKs, compared with their constants by
    # `test_the_exchange_operation_checks_match_the_model`.
    "exchange_imports": {"pk_exchange_imports", "fk_exchange_imports_user_id_users"},
    "exchange_operations": {
        "pk_exchange_operations",
        "uq_exchange_operations_source_id",
        "ck_exchange_operations_kind",
        "ck_exchange_operations_manual",
        "fk_exchange_operations_user_id_users",
        "fk_exchange_operations_import_id_exchange_imports",
    },
    "derived_addresses": {
        "pk_derived_addresses",
        "uq_derived_addresses_wallet_branch_index",
        "ck_derived_addresses_branch",
        "ck_derived_addresses_child_index",
        "ck_derived_addresses_used",
        "fk_derived_addresses_wallet_id_wallets",
    },
}

#: The names the dropped tables carried at `0011_extended_keys`, the last revision that has
#: them. `0012`'s downgrade rebuilds them by running `0006` to `0010` again, so these are also
#: what a rollback past it recreates.
PRE_DROP_CONSTRAINT_NAMES = {
    # #12. Three CHECKs, compared with their texts by
    # `test_the_exchange_check_constraints_are_the_specs`.
    "exchange_accounts": {
        "pk_exchange_accounts",
        "uq_exchange_accounts_user_exchange",
        "ck_exchange_accounts_exchange_key",
        # #15: the batch rebuild that added the sync state has to keep every name above.
        "ck_exchange_accounts_sync_status",
        # #104: a second batch rebuild, for the two balance columns. It has to keep every
        # name above too, and it adds this one.
        "ck_exchange_accounts_balances_error",
        "fk_exchange_accounts_user_id_users",
    },
    "exchange_fills": {
        "pk_exchange_fills",
        "uq_exchange_fills_account_trade",
        "ck_exchange_fills_external_trade_id",
        "ck_exchange_fills_side",
        "ck_exchange_fills_quote_quantity_derived",
        "fk_exchange_fills_exchange_account_id_exchange_accounts",
    },
    # #15.
    "exchange_sync_windows": {
        "pk_exchange_sync_windows",
        "fk_exchange_sync_windows_exchange_account_id_exchange_accounts",
    },
    "exchange_sync_runs": {
        "pk_exchange_sync_runs",
        "ck_exchange_sync_runs_trigger",
        "ck_exchange_sync_runs_status",
    },
    "exchange_sync_run_accounts": {
        "pk_exchange_sync_run_accounts",
        "uq_exchange_sync_run_accounts_run_account",
        "ck_exchange_sync_run_accounts_status",
        "ck_exchange_sync_run_accounts_error_kind",
        "fk_exchange_sync_run_accounts_exchange_sync_run_id_exchange_sync_runs",
        "fk_exchange_sync_run_accounts_exchange_account_id_exchange_accounts",
    },
    # #19. Every child cascades from the header and the header from `users`, and each natural
    # key is a named unique constraint (spec 021, R3).
    "accounting_snapshots": {
        "pk_accounting_snapshots",
        "uq_accounting_snapshots_user_method",
        "fk_accounting_snapshots_user_id_users",
    },
    "accounting_positions": {
        "pk_accounting_positions",
        "uq_accounting_positions_snapshot_asset",
        "fk_accounting_positions_snapshot_id_accounting_snapshots",
    },
    "accounting_lots": {
        "pk_accounting_lots",
        "uq_accounting_lots_snapshot_seq",
        "ck_accounting_lots_kind",
        "fk_accounting_lots_snapshot_id_accounting_snapshots",
    },
    "accounting_warnings": {
        "pk_accounting_warnings",
        "uq_accounting_warnings_snapshot_seq",
        "ck_accounting_warnings_kind",
        "fk_accounting_warnings_snapshot_id_accounting_snapshots",
    },
    # #18. One CHECK, the note's. No CHECK on a money column.
    #
    # **The primary key is the one anonymous constraint in the schema, and it has to be.**
    # `AUTOINCREMENT` is only legal on SQLite's inline `INTEGER PRIMARY KEY`, and SQLAlchemy
    # renders `sqlite_autoincrement=True` as `id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT`
    # with no `CONSTRAINT` clause, so the `pk_manual_adjustments` the migration passes is
    # dropped. The rebuild hazard this map guards against is therefore a different one here:
    # a batch migration must pass `sqlite_autoincrement` again, or it silently loses
    # `AUTOINCREMENT`.
    "manual_adjustments": {
        None,
        "ck_manual_adjustments_note_not_blank",
        "fk_manual_adjustments_user_id_users",
    },
    # #104. No CHECK, and the absence is part of the pin: `quantity` is a `TEXT` money
    # column, and a sign check on one is a numeric-affinity comparison.
    "exchange_balances": {
        "pk_exchange_balances",
        "uq_exchange_balances_account_asset",
        "fk_exchange_balances_exchange_account_id_exchange_accounts",
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


def test_the_prices_migration_reverses_on_its_own_and_leaves_the_rest_standing(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """#9's migration, downgraded one step. `prices` goes; nothing else moves.

    `test_downgrade_base_leaves_no_application_tables` already walks the whole history
    down, and it would pass for a `downgrade()` that dropped `wallets` instead of
    `prices` -- by the time base is reached, everything is gone either way. A single-step
    reversal is the only thing that can tell the two apart, and a single step is what a
    rollback on the Pi actually performs.

    The seed rows are asserted afterwards because a `downgrade()` written with a stray
    `op.execute` would be invisible to a table-name comparison.
    """
    upgrade_to_head(database_url)
    assert "prices" in table_names(sync_engine)

    command.downgrade(build_alembic_config(database_url), REVISION_BEFORE_PRICES)

    # Everything above `0003_wallets` comes down, which since #10 is `prices` *and* the
    # three balance tables, and since #24 `derived_addresses` too. Subtracting them all
    # is what keeps this test about the prices migration rather than about how many
    # revisions happen to sit on top of it.
    assert table_names(sync_engine) == (
        APPLICATION_TABLES
        - {"prices"}
        - BALANCE_TABLES
        - DERIVED_ADDRESS_TABLES
        - PRICE_HISTORY_TABLES
        - RECONSTRUCTED_TABLES
        - HOURLY_TABLES
        - OPERATION_TABLES
    ) | {STAMP_TABLE}
    assert seed_rows(sync_engine) == EXPECTED_SEED_ROWS

    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_balances_migration_reverses_on_its_own_and_leaves_the_rest_standing(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """#10's migration, downgraded one step. The three new tables go; nothing else moves.

    The same argument the prices test makes, and it is worth repeating for this revision in
    particular: `balance_snapshots` has foreign keys into both `wallets` and `sync_runs`, so
    a `downgrade()` that drops the tables in the wrong order fails on the Pi at exactly the
    moment a release is being rolled back -- which is the worst possible time to discover
    it, and the only time it would ever be run.

    The seed rows are asserted afterwards because a `downgrade()` written with a stray
    `op.execute` would be invisible to a comparison of table names.
    """
    upgrade_to_head(database_url)
    assert table_names(sync_engine) >= BALANCE_TABLES

    command.downgrade(build_alembic_config(database_url), PRICES_REVISION)

    # `0012`'s downgrade recreates the exchange tables on the way down, and `0010` to `0006`
    # drop them again, so none of them is left over.
    assert table_names(sync_engine) == (
        APPLICATION_TABLES
        - BALANCE_TABLES
        - DERIVED_ADDRESS_TABLES
        - PRICE_HISTORY_TABLES
        - RECONSTRUCTED_TABLES
        - HOURLY_TABLES
        - OPERATION_TABLES
    ) | {STAMP_TABLE}
    assert seed_rows(sync_engine) == EXPECTED_SEED_ROWS

    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_balances_revision_sits_directly_on_top_of_the_prices_one() -> None:
    """Adjacency, pinned, because the single-step downgrade above is written in terms of it.

    Asserted as adjacency rather than as the head, for the reason the prices test gives: the
    next issue adds a revision on top of this one, and a test pinning the head would fail on
    that change for a reason that has nothing to do with balances.
    """
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert BALANCES_REVISION in revisions
    assert revisions.index(BALANCES_REVISION) == revisions.index(PRICES_REVISION) - 1


def test_the_exchanges_migration_reverses_on_its_own_and_leaves_the_rest_standing(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """#12's migration, downgraded one step. The two exchange tables go; nothing else moves.

    `exchange_fills` references `exchange_accounts` with `ON DELETE RESTRICT`, so the order
    the `downgrade()` drops them in matters, and it only matters with rows present -- which
    is why one of each is written first. The owner row is asserted afterwards because a
    `downgrade()` with a stray `op.execute` would be invisible to a comparison of names.

    Since spec 036 the tables exist only below `0012`, so the rows are written at the last
    revision that has them.
    """
    config = build_alembic_config(database_url)
    command.upgrade(config, REVISION_BEFORE_DROP)
    with sync_engine.begin() as connection:
        user_id: int = connection.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES ('owner', 'not-a-hash', '2026-09-25 12:00:00.000000') RETURNING id"
            )
        ).scalar_one()
        account_id: int = connection.execute(
            text(
                "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
                "VALUES (:user_id, 'bitget', '2026-09-25 12:00:00.000000') RETURNING id"
            ),
            {"user_id": user_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, symbol, "
                "base_asset, quote_asset, side, quantity, price, quote_quantity, "
                "quote_quantity_derived, fee_amount, executed_at, raw_payload, ingested_at) "
                "VALUES (:account_id, '1001', 'BTCUSDT', 'BTC', 'USDT', 'buy', '1.0', '2.0', "
                "'2.0', 0, '0', '2026-09-25 12:00:00.000000', '{}', "
                "'2026-09-25 12:00:00.000000')"
            ),
            {"account_id": account_id},
        )
    assert table_names(sync_engine) >= EXCHANGE_TABLES

    command.downgrade(config, BALANCES_REVISION)

    assert table_names(sync_engine) == (
        APPLICATION_TABLES
        - DERIVED_ADDRESS_TABLES
        - PRICE_HISTORY_TABLES
        - RECONSTRUCTED_TABLES
        - HOURLY_TABLES
        - OPERATION_TABLES
    ) | {STAMP_TABLE}
    assert seed_rows(sync_engine) == EXPECTED_SEED_ROWS
    with sync_engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM users")) == 1

    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_exchanges_revision_sits_directly_on_top_of_the_balances_one() -> None:
    """Adjacency, for the single-step downgrade above; not the head, which #15 will move."""
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert EXCHANGES_REVISION in revisions
    assert revisions.index(EXCHANGES_REVISION) == revisions.index(BALANCES_REVISION) - 1


def test_the_prices_revision_sits_directly_on_top_of_the_wallets_one() -> None:
    """The revision ids are pinned, because the downgrade test is written in terms of them.

    `REVISION_BEFORE_PRICES` and `PRICES_REVISION` are strings handed to
    `command.downgrade`, and Alembic answers an unknown revision with an error rather than
    a no-op -- which would at least be loud. The quieter failure is a revision that still
    exists under a different parent: the single-step downgrade above would then reverse a
    different migration, and its assertions would be about the wrong table while still
    passing.

    Their *adjacency* is asserted rather than the head, deliberately. #10 adds a revision
    on top of this one, and a test pinning the head would fail on that change for a reason
    that has nothing to do with prices -- which is how a pin teaches people to edit it
    without reading it.
    """
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert PRICES_REVISION in revisions
    assert REVISION_BEFORE_PRICES in revisions
    # `walk_revisions` yields newest first, so the child comes one before its parent.
    assert revisions.index(PRICES_REVISION) == revisions.index(REVISION_BEFORE_PRICES) - 1


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
    # #10's two, named exactly as the spec's DDL writes them. An index the model declares
    # and the migration forgets is invisible to the drift check on SQLite and shows up only
    # as a table scan per chart render, on the slowest hardware this runs on.
    assert {index["name"] for index in inspector.get_indexes("sync_runs")} == {
        "ix_sync_runs_started_at"
    }
    assert {index["name"] for index in inspector.get_indexes("balance_snapshots")} == {
        "ix_balance_snapshots_wallet_observed"
    }
    # **No index on either `sync_run_id`**, and the absence is part of the pin, exactly as
    # `prices` having none at all is. `uq_sync_run_chains_run_chain` already leads with that
    # column, so an index beside it would be a second copy paid for on every write; and
    # nothing queries snapshots by run -- the two reads are "the latest per wallet", which
    # is the primary key, and "one wallet's history", which is the index above. A name
    # appearing here later is a decision somebody has to make on purpose.
    assert inspector.get_indexes("sync_run_chains") == []


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


def test_the_new_check_constraints_match_the_models(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Every `CHECK` #10 adds, reflected off a migrated file and compared with its constant.

    The same hazard `test_the_kind_check_constraint_matches_the_model` documents, six times
    over: autogenerate has no check-constraint comparator, so editing one of these constants
    without editing `v0005_balances.py` passes ruff, mypy, the layering contract *and* the
    drift check, and then fails on the Pi with `CHECK constraint failed`.

    `sync_run_chains.chain_key` reuses `_WALLET_CHAIN_KEY_CHECK` -- one constant for one fact
    -- and the reuse is asserted rather than assumed, because a second copy of the chain list
    is exactly how the two tables would come to admit different chains. The inserts that
    prove each constraint actually refuses what it should are in
    `tests/db/test_sync_runs_repository.py` and `tests/db/test_balances_repository.py`.
    """
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)
    expected = {
        "sync_runs": {
            "ck_sync_runs_trigger": _SYNC_RUN_TRIGGER_CHECK,
            "ck_sync_runs_status": _SYNC_RUN_STATUS_CHECK,
        },
        "sync_run_chains": {
            "ck_sync_run_chains_chain_key": _WALLET_CHAIN_KEY_CHECK,
            "ck_sync_run_chains_status": _SYNC_RUN_CHAIN_STATUS_CHECK,
            "ck_sync_run_chains_error_kind": _SYNC_RUN_CHAIN_ERROR_KIND_CHECK,
        },
        "balance_snapshots": {
            "ck_balance_snapshots_confirmed": _BALANCE_SNAPSHOT_CONFIRMED_CHECK,
        },
    }

    for table, constraints in expected.items():
        reflected = {
            str(found["name"]): normalise_sql(str(found["sqltext"]))
            for found in inspector.get_check_constraints(table)
        }
        assert set(reflected) == set(constraints), table
        for name, sql in constraints.items():
            assert reflected[name] == normalise_sql(sql), f"{table}.{name}"


def test_the_dropped_tables_carry_the_convention_names_below_the_drop(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """At `0011_extended_keys` the eleven tables exist, with every constraint named."""
    command.upgrade(build_alembic_config(database_url), REVISION_BEFORE_DROP)
    inspector = inspect(sync_engine)

    assert set(PRE_DROP_CONSTRAINT_NAMES) == DROPPED_TABLES
    assert table_names(sync_engine) == (
        (
            APPLICATION_TABLES
            - PRICE_HISTORY_TABLES
            - RECONSTRUCTED_TABLES
            - HOURLY_TABLES
            - OPERATION_TABLES
        )
        | DROPPED_TABLES
        | {STAMP_TABLE}
    )
    for table, expected in PRE_DROP_CONSTRAINT_NAMES.items():
        found = {inspector.get_pk_constraint(table)["name"]}
        found |= {unique["name"] for unique in inspector.get_unique_constraints(table)}
        found |= {check["name"] for check in inspector.get_check_constraints(table)}
        found |= {foreign["name"] for foreign in inspector.get_foreign_keys(table)}
        assert found == expected, table


def test_the_exchange_check_constraints_are_the_specs(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Every `CHECK` #12 adds, reflected at the last revision that has it, against the spec.

    The model constants went with the models in spec 036, so the texts are pinned by hand from
    the spec's data model. **The absence of a money `CHECK` is part of the pin**: exactly these
    three exist on `exchange_fills`, because `quantity > 0` on a `TEXT` column is a
    numeric-affinity comparison -- the float path rule 2 bans.
    """
    command.upgrade(build_alembic_config(database_url), REVISION_BEFORE_DROP)
    inspector = inspect(sync_engine)
    expected = {
        "exchange_accounts": {
            "ck_exchange_accounts_exchange_key": "exchange_key IN ('bingx', 'bitget')",
            "ck_exchange_accounts_sync_status": (
                "sync_status IN ('auth_failed', 'error', 'never_synced', 'ok')"
            ),
            "ck_exchange_accounts_balances_error": (
                "balances_error IS NULL OR "
                "balances_error IN ('auth', 'conflict', 'insufficient_scope', 'internal', "
                "'invalid_request', 'rate_limited', 'retention_window', 'schema', 'unavailable')"
            ),
        },
        "exchange_fills": {
            "ck_exchange_fills_external_trade_id": "external_trade_id <> ''",
            "ck_exchange_fills_side": "side IN ('buy', 'sell')",
            "ck_exchange_fills_quote_quantity_derived": "quote_quantity_derived IN (0, 1)",
        },
    }

    for table, constraints in expected.items():
        reflected = {
            str(found["name"]): normalise_sql(str(found["sqltext"]))
            for found in inspector.get_check_constraints(table)
        }
        assert set(reflected) == set(constraints), table
        for name, sql in constraints.items():
            assert reflected[name] == normalise_sql(sql), f"{table}.{name}"


def test_the_exchange_foreign_keys_carry_their_delete_rules(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Cascade from the owner to the account; restrict from the account to its fills.

    Reflected at the last revision that has the tables. No index on either table, and the
    absence is part of the pin.
    """
    command.upgrade(build_alembic_config(database_url), REVISION_BEFORE_DROP)
    inspector = inspect(sync_engine)

    accounts = inspector.get_foreign_keys("exchange_accounts")
    fills = inspector.get_foreign_keys("exchange_fills")

    assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in accounts] == [
        ("users", "CASCADE")
    ]
    assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in fills] == [
        ("exchange_accounts", "RESTRICT")
    ]
    assert inspector.get_indexes("exchange_accounts") == []
    assert inspector.get_indexes("exchange_fills") == []


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


def test_the_drop_revision_sits_directly_on_top_of_the_extended_keys_one() -> None:
    """Adjacency, for the tests above that stop one revision below the drop."""
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert revisions.index(DROP_REVISION) == revisions.index(REVISION_BEFORE_DROP) - 1


def test_the_price_history_check_constraints_match_the_model(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Spec 037's two `CHECK`s, reflected off a migrated file and compared with their constants.

    The quote currency reuses `_PRICE_QUOTE_CURRENCY_CHECK`, one constant for one fact, so the
    history cannot come to admit a currency the current prices do not.
    """
    upgrade_to_head(database_url)

    reflected = {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspect(sync_engine).get_check_constraints("price_history")
    }

    assert reflected == {
        "ck_price_history_quote_currency": normalise_sql(_PRICE_QUOTE_CURRENCY_CHECK),
        "ck_price_history_basis": normalise_sql(_PRICE_HISTORY_BASIS_CHECK),
    }
    assert _PRICE_HISTORY_BASIS_CHECK == "basis IN ('close', 'observed')"


def test_the_price_history_revision_sits_directly_on_top_of_the_drop() -> None:
    """Adjacency, for the single-step reversals above that subtract its table."""
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert revisions.index("0013_price_history") == revisions.index(DROP_REVISION) - 1


def test_the_price_history_migration_reverses_on_its_own(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """One step down drops `price_history` and nothing else; the upgrade brings it back."""
    upgrade_to_head(database_url)

    command.downgrade(build_alembic_config(database_url), DROP_REVISION)

    assert table_names(sync_engine) == (
        APPLICATION_TABLES
        - PRICE_HISTORY_TABLES
        - RECONSTRUCTED_TABLES
        - HOURLY_TABLES
        - OPERATION_TABLES
    ) | {STAMP_TABLE}
    assert seed_rows(sync_engine) == EXPECTED_SEED_ROWS

    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_reconstructed_balance_check_matches_the_model(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Spec 038's one `CHECK`, and the wallet foreign key's cascade."""
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    reflected = {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspector.get_check_constraints("reconstructed_balances")
    }
    foreign = inspector.get_foreign_keys("reconstructed_balances")

    assert reflected == {
        "ck_reconstructed_balances_confirmed": normalise_sql(_RECONSTRUCTED_BALANCE_CONFIRMED_CHECK)
    }
    assert _RECONSTRUCTED_BALANCE_CONFIRMED_CHECK == "confirmed >= 0"
    assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in foreign] == [
        ("wallets", "CASCADE")
    ]


def test_the_reconstructed_balances_migration_reverses_on_its_own(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """One step down drops `reconstructed_balances` and nothing else; the upgrade restores it."""
    upgrade_to_head(database_url)
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]
    assert revisions.index("0014_reconstructed_balances") == (
        revisions.index("0013_price_history") - 1
    )

    command.downgrade(build_alembic_config(database_url), "0013_price_history")

    assert table_names(sync_engine) == (
        APPLICATION_TABLES - RECONSTRUCTED_TABLES - HOURLY_TABLES - OPERATION_TABLES
    ) | {STAMP_TABLE}

    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_price_hourly_check_matches_the_model(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Spec 041's one `CHECK`: the quote currencies the current prices admit, no more."""
    upgrade_to_head(database_url)

    reflected = {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspect(sync_engine).get_check_constraints("price_hourly")
    }

    assert reflected == {
        "ck_price_hourly_quote_currency": normalise_sql(_PRICE_QUOTE_CURRENCY_CHECK),
    }


def test_the_price_hourly_migration_reverses_on_its_own(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """One step down drops `price_hourly` and nothing else; the upgrade restores it."""
    upgrade_to_head(database_url)
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]
    assert revisions.index("0015_price_hourly") == (
        revisions.index("0014_reconstructed_balances") - 1
    )

    command.downgrade(build_alembic_config(database_url), "0014_reconstructed_balances")

    assert table_names(sync_engine) == (APPLICATION_TABLES - HOURLY_TABLES - OPERATION_TABLES) | {
        STAMP_TABLE
    }
    assert seed_rows(sync_engine) == EXPECTED_SEED_ROWS

    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_exchange_operation_checks_match_the_model(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Spec 042's two `CHECK`s: R1's kinds, and R11's buy or sell for a manual entry."""
    upgrade_to_head(database_url)

    reflected = {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspect(sync_engine).get_check_constraints("exchange_operations")
    }

    assert reflected == {
        "ck_exchange_operations_kind": normalise_sql(_EXCHANGE_OPERATION_KIND_CHECK),
        "ck_exchange_operations_manual": normalise_sql(_EXCHANGE_OPERATION_MANUAL_CHECK),
    }


def test_the_manual_rewards_migration_reverses_on_its_own(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """Spec 043: one step down restores 0016's `CHECK`s, deleting the manual rewards and fees
    they refuse and keeping every other row; the upgrade admits them again."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, created_at) "
                "VALUES (90, 'rewards', 'x', '2026-10-10 00:00:00.000000')"
            )
        )
        for number, (source, kind) in enumerate(
            [("manual", "buy"), ("manual", "reward"), ("manual", "fee"), ("bitget", "reward")]
        ):
            connection.execute(
                text(
                    "INSERT INTO exchange_operations (user_id, source, venue, external_id, "
                    "executed_at, kind, asset, quantity, description, created_at) VALUES "
                    "(90, :source, 'x', :id, '2026-10-10 00:00:00.000000', :kind, 'KAS', "
                    "'1', '', '2026-10-10 00:00:00.000000')"
                ),
                {"source": source, "id": f"id-{number}", "kind": kind},
            )

    command.downgrade(build_alembic_config(database_url), "0016_exchange_operations")

    with sync_engine.begin() as connection:
        kept = connection.execute(
            text("SELECT source, kind FROM exchange_operations WHERE user_id = 90 ORDER BY id")
        ).all()
    assert [tuple(row) for row in kept] == [("manual", "buy"), ("bitget", "reward")]
    assert {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspect(sync_engine).get_check_constraints("exchange_operations")
    }["ck_exchange_operations_manual"] == normalise_sql(
        "source != 'manual' OR kind IN ('buy', 'sell')"
    )

    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_exchange_operations_migration_reverses_on_its_own(
    database_url: str,
    sync_engine: Engine,
) -> None:
    """One step down drops the two tables and nothing else; the upgrade restores them."""
    upgrade_to_head(database_url)
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]
    assert revisions.index("0016_exchange_operations") == (revisions.index("0015_price_hourly") - 1)

    command.downgrade(build_alembic_config(database_url), "0015_price_hourly")

    assert table_names(sync_engine) == (APPLICATION_TABLES - OPERATION_TABLES) | {STAMP_TABLE}
    assert seed_rows(sync_engine) == EXPECTED_SEED_ROWS

    upgrade_to_head(database_url)

    assert table_names(sync_engine) == APPLICATION_TABLES | {STAMP_TABLE}
