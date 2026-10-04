"""Migration `0010_exchange_balances`: the table, the two account columns, the reversal (#104).

Synchronous, like `test_migrations.py`, for the reason that module gives: Alembic's async
`env.py` calls `asyncio.run`. The schema is read back through a second, unconfigured engine,
so what is asserted is what is on disk.

## What is pinned, and why each one (spec 025, *Design: storage*)

* **`exchange_balances`**: its four columns, all `NOT NULL`, `quantity` a `TEXT` column and
  never a numeric one; `UNIQUE (exchange_account_id, asset)`, named, and proved by an insert;
  the foreign key to the account with `ON DELETE CASCADE`, proved by deleting an account; no
  index beside the unique constraint; **and no `CHECK` at all** -- a sign check on a `TEXT`
  money column is a comparison SQLite makes by numeric affinity, the float path rule 2 bans.
* **`exchange_accounts` gains `balances_read_at` and `balances_error`**, both nullable, and
  keeps every column, default and constraint it had: the migration rebuilds the table, and a
  rebuild written from a stale copy of the table silently loses whatever the copy lacks.
* **The named `CHECK` over `ExchangeSyncErrorKind`**, compared with the model's constant and
  with the enum, and exercised with real writes: every kind and `NULL` are stored, anything
  else is refused.
* **The upgrade over data.** `exchange_accounts` is the parent of the fills (`RESTRICT`), of
  the pending windows and of the run outcomes (`CASCADE`). A rebuild that re-numbered it, or
  ran with foreign keys enforced, would orphan or delete history. Asserted with a row in
  every child, compared before and after, and with the append-only triggers still in place.
* **The reversal**, as one step: the table and the two columns go, everything else stays --
  fills, windows, outcomes, the account's own sync state -- and the upgrade runs again.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.alembic_config import MIGRATIONS_DIR, build_alembic_config, upgrade_to_head
from portfolio.db.migrations.versions import v0010_exchange_balances
from portfolio.db.models import (
    _EXCHANGE_ACCOUNT_BALANCES_ERROR_CHECK,
    _EXCHANGE_SYNC_RUN_ACCOUNT_ERROR_KIND_CHECK,
    FILL_SCALE,
    ExchangeAccount,
    ExchangeBalance,
)
from portfolio.db.types import NumericText, UtcDateTime
from portfolio.repositories.exchange_sync_runs import ExchangeSyncErrorKind

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

REVISION: Final = "0010_exchange_balances"
PARENT: Final = "0009_manual_adjustments"
TABLE: Final = "exchange_balances"
NEW_ACCOUNT_COLUMNS: Final = ("balances_read_at", "balances_error")
AT: Final = "2026-10-01 10:00:00.000000"
LATER: Final = "2026-10-01 11:00:00.000000"
TRIGGERS: Final = frozenset({"exchange_fills_no_update", "exchange_fills_no_delete"})

#: Spec 025's *Design: storage*: every column, its SQLite type, and whether it may be null.
EXPECTED_COLUMNS: Final[dict[str, tuple[str, bool]]] = {
    "id": ("INTEGER", False),
    "exchange_account_id": ("INTEGER", False),
    "asset": ("TEXT", False),
    "quantity": ("TEXT", False),
}

#: `exchange_accounts` as the revision leaves it: the nine columns `0007` left, then two.
EXPECTED_ACCOUNT_COLUMNS: Final[dict[str, tuple[str, bool]]] = {
    "id": ("INTEGER", False),
    "user_id": ("INTEGER", False),
    "exchange_key": ("TEXT", False),
    "created_at": ("DATETIME", False),
    "sync_status": ("TEXT", False),
    "requested_since": ("DATETIME", True),
    "effective_since": ("DATETIME", True),
    "planned_until": ("DATETIME", True),
    "last_synced_at": ("DATETIME", True),
    "balances_read_at": ("DATETIME", True),
    "balances_error": ("TEXT", True),
}

#: The nine kinds, written out. `ExchangeSyncErrorKind` is compared with this, not read for it.
ERROR_KINDS: Final = frozenset(
    {
        "auth",
        "conflict",
        "insufficient_scope",
        "internal",
        "invalid_request",
        "rate_limited",
        "retention_window",
        "schema",
        "unavailable",
    }
)

FILL_INSERT: Final = (
    "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, external_order_id, "
    "symbol, base_asset, quote_asset, side, quantity, price, quote_quantity, "
    "quote_quantity_derived, fee_amount, fee_asset, executed_at, raw_payload, ingested_at) "
    "VALUES (:account, :trade, '5001', 'BTCUSDT', 'BTC', 'USDT', 'buy', "
    "'0.500000000000000000', '60000.000000000000000000', '30000.000000000000000000', 0, "
    "'0.000500000000000000', 'BTC', :at, '{\"tradeId\":\"1\"}', :at)"
)


def normalise_sql(expression: str) -> str:
    """Collapse whitespace and nothing else, as `test_migrations.normalise_sql` does."""
    return " ".join(expression.split())


def check_values(expression: str) -> set[str]:
    """The quoted literals of an `IN (...)` list."""
    inside = re.search(r"IN \((.*)\)", expression)
    assert inside is not None, expression
    return set(re.findall(r"'([^']*)'", inside.group(1)))


def insert_user(connection: Connection, username: str = "owner") -> int:
    user: int = connection.execute(
        text(
            "INSERT INTO users (username, password_hash, created_at) "
            "VALUES (:name, 'not-a-hash', :at) RETURNING id"
        ),
        {"name": username, "at": AT},
    ).scalar_one()
    return int(user)


def insert_account(connection: Connection, user: int, key: str = "bitget") -> int:
    account: int = connection.execute(
        text(
            "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
            "VALUES (:user, :key, :at) RETURNING id"
        ),
        {"user": user, "key": key, "at": AT},
    ).scalar_one()
    return int(account)


def insert_balance(connection: Connection, account: int, asset: str, quantity: str) -> None:
    connection.execute(
        text(
            "INSERT INTO exchange_balances (exchange_account_id, asset, quantity) "
            "VALUES (:account, :asset, :quantity)"
        ),
        {"account": account, "asset": asset, "quantity": quantity},
    )


def all_rows(engine: Engine, table: str) -> list[dict[str, Any]]:
    """Every row of `table`, by id. `table` is one of this module's literals, never input."""
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id"))  # noqa: S608
            .mappings()
            .all()
        ]


def column_shapes(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    return {
        str(column["name"]): (str(column["type"]), bool(column["nullable"]))
        for column in inspect(engine).get_columns(table)
    }


def trigger_names(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        found = connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        return {str(name) for (name,) in found}


def seed_history(connection: Connection) -> int:
    """An owner, a synced Bitget account, and a row in each table that references it."""
    user = insert_user(connection)
    account = insert_account(connection, user)
    connection.execute(
        text(
            "UPDATE exchange_accounts SET sync_status = 'ok', requested_since = :at, "
            "effective_since = :at, planned_until = :later, last_synced_at = :later"
        ),
        {"at": AT, "later": LATER},
    )
    connection.execute(text(FILL_INSERT), {"account": account, "trade": "1001", "at": AT})
    connection.execute(
        text(
            'INSERT INTO exchange_sync_windows (exchange_account_id, "since", "until", '
            "\"cursor\") VALUES (:account, :at, :later, '1001')"
        ),
        {"account": account, "at": AT, "later": LATER},
    )
    run: int = connection.execute(
        text(
            "INSERT INTO exchange_sync_runs (trigger, status, started_at, accounts_total) "
            "VALUES ('manual', 'success', :at, 1) RETURNING id"
        ),
        {"at": AT},
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO exchange_sync_run_accounts (exchange_sync_run_id, exchange_account_id, "
            "status, windows_completed, pages, fills_seen, fills_inserted, error_kind, detail) "
            "VALUES (:run, :account, 'success', 1, 1, 1, 1, NULL, NULL)"
        ),
        {"run": run, "account": account},
    )
    return account


HISTORY_TABLES: Final = (
    "exchange_fills",
    "exchange_sync_windows",
    "exchange_sync_runs",
    "exchange_sync_run_accounts",
)


def history(engine: Engine) -> dict[str, list[dict[str, Any]]]:
    return {table: all_rows(engine, table) for table in HISTORY_TABLES}


# --------------------------------------------------------------------------------------
# The revision
# --------------------------------------------------------------------------------------


def test_the_revision_sits_directly_on_top_of_the_adjustments_one() -> None:
    """Adjacency, not the head, for the reason `test_migrations.py` gives."""
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert REVISION in revisions
    assert revisions.index(REVISION) == revisions.index(PARENT) - 1
    assert v0010_exchange_balances.revision == REVISION
    assert v0010_exchange_balances.down_revision == PARENT


def test_the_model_names_the_table() -> None:
    assert ExchangeBalance.__tablename__ == TABLE


# --------------------------------------------------------------------------------------
# `exchange_balances`
# --------------------------------------------------------------------------------------


def test_the_table_has_the_specs_columns(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)

    assert column_shapes(sync_engine, TABLE) == EXPECTED_COLUMNS


def test_the_quantity_is_money_as_text_at_the_fill_scale() -> None:
    """`NumericText(FILL_SCALE)`: the scale of the fills a balance is compared against.

    A `Numeric` here would round-trip every balance through a float on SQLite.
    """
    quantity = ExchangeBalance.__table__.c.quantity.type

    assert isinstance(quantity, NumericText)
    assert quantity.scale == FILL_SCALE == 18


def test_the_table_has_no_check_at_all(database_url: str, sync_engine: Engine) -> None:
    """No sign check on a `TEXT` money column: SQLite would compare it as a float."""
    upgrade_to_head(database_url)

    assert inspect(sync_engine).get_check_constraints(TABLE) == []


def test_the_keys_and_the_absent_index_are_the_specs(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    rules = {
        (fk["name"], fk["constrained_columns"][0], fk["referred_table"], fk["options"]["ondelete"])
        for fk in inspector.get_foreign_keys(TABLE)
    }
    uniques = [
        (unique["name"], tuple(unique["column_names"]))
        for unique in inspector.get_unique_constraints(TABLE)
    ]

    assert rules == {
        (
            "fk_exchange_balances_exchange_account_id_exchange_accounts",
            "exchange_account_id",
            "exchange_accounts",
            "CASCADE",
        )
    }
    assert uniques == [("uq_exchange_balances_account_asset", ("exchange_account_id", "asset"))]
    assert inspector.get_pk_constraint(TABLE)["name"] == "pk_exchange_balances"
    assert inspector.get_indexes(TABLE) == [], "the unique constraint leads with the account"


def test_one_row_per_asset_per_account(database_url: str, sync_engine: Engine) -> None:
    """The unique constraint, by insert: a second total for the same asset is refused."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)
        bitget = insert_account(connection, user, "bitget")
        bingx = insert_account(connection, user, "bingx")
        insert_balance(connection, bitget, "BTC", "0.250000000000000000")
        # The control: the same asset at another venue, and another asset at this one.
        insert_balance(connection, bingx, "BTC", "0.100000000000000000")
        insert_balance(connection, bitget, "KAS", "1500.000000000000000000")

    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        sync_engine.begin() as connection,
    ):
        insert_balance(connection, bitget, "BTC", "9.000000000000000000")

    assert [(row["exchange_account_id"], row["asset"]) for row in all_rows(sync_engine, TABLE)] == [
        (bitget, "BTC"),
        (bingx, "BTC"),
        (bitget, "KAS"),
    ]


@pytest.mark.parametrize("column", ["exchange_account_id", "asset", "quantity"])
def test_no_column_of_a_balance_may_be_null(
    database_url: str, sync_engine: Engine, column: str
) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        account = insert_account(connection, insert_user(connection))
    values: dict[str, object] = {"exchange_account_id": account, "asset": "BTC", "quantity": "1"}
    values[column] = None

    with (
        pytest.raises(IntegrityError, match="NOT NULL constraint failed"),
        sync_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO exchange_balances (exchange_account_id, asset, quantity) "
                "VALUES (:exchange_account_id, :asset, :quantity)"
            ),
            values,
        )

    assert all_rows(sync_engine, TABLE) == []


def test_removing_an_account_takes_its_balances_and_only_its_own(
    database_url: str, sync_engine: Engine
) -> None:
    """Derived data cascades from the account. The other account's reading stays."""
    upgrade_to_head(database_url)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        user = insert_user(connection)
        bitget = insert_account(connection, user, "bitget")
        bingx = insert_account(connection, user, "bingx")
        insert_balance(connection, bitget, "BTC", "0.250000000000000000")
        insert_balance(connection, bitget, "KAS", "1500.000000000000000000")
        insert_balance(connection, bingx, "BTC", "0.100000000000000000")
        connection.execute(text("DELETE FROM exchange_accounts WHERE id = :id"), {"id": bitget})
        connection.commit()

    assert [(row["exchange_account_id"], row["asset"]) for row in all_rows(sync_engine, TABLE)] == [
        (bingx, "BTC")
    ]


def test_removing_the_owner_takes_the_balances_through_the_account(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        user = insert_user(connection)
        account = insert_account(connection, user)
        insert_balance(connection, account, "BTC", "0.250000000000000000")
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})
        connection.commit()

    assert all_rows(sync_engine, TABLE) == []
    assert all_rows(sync_engine, "exchange_accounts") == []


def test_a_balance_cannot_reference_an_account_that_does_not_exist(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)

    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            insert_balance(connection, 4242, "BTC", "1.000000000000000000")

    assert all_rows(sync_engine, TABLE) == []


# --------------------------------------------------------------------------------------
# `exchange_accounts`: two columns added, nothing lost
# --------------------------------------------------------------------------------------


def test_the_account_gains_two_nullable_columns_and_keeps_the_rest(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)

    assert column_shapes(sync_engine, "exchange_accounts") == EXPECTED_ACCOUNT_COLUMNS


def test_the_two_columns_are_a_utc_datetime_and_text_on_the_model() -> None:
    columns = ExchangeAccount.__table__.c

    assert isinstance(columns.balances_read_at.type, UtcDateTime)
    assert columns.balances_read_at.nullable is True
    assert columns.balances_error.nullable is True


def test_the_rebuild_keeps_the_sync_status_default(database_url: str, sync_engine: Engine) -> None:
    """A rebuild copies the table from the migration's own description of it.

    `sync_status` has a server default, and every account the application creates relies on
    it. Proved by an insert that names neither it nor the new columns.
    """
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        account = insert_account(connection, insert_user(connection))

    (row,) = [row for row in all_rows(sync_engine, "exchange_accounts") if row["id"] == account]
    assert row["sync_status"] == "never_synced"
    assert row["balances_read_at"] is None
    assert row["balances_error"] is None


def test_the_rebuild_keeps_one_account_per_owner_and_venue(
    database_url: str, sync_engine: Engine
) -> None:
    """`uq_exchange_accounts_user_exchange` and the venue `CHECK` survive the rebuild."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)
        insert_account(connection, user, "bitget")

    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        sync_engine.begin() as connection,
    ):
        insert_account(connection, user, "bitget")
    with pytest.raises(IntegrityError, match="CHECK"), sync_engine.begin() as connection:
        insert_account(connection, user, "kraken")
    with pytest.raises(IntegrityError, match="CHECK"), sync_engine.begin() as connection:
        connection.execute(text("UPDATE exchange_accounts SET sync_status = 'fine'"))


# --------------------------------------------------------------------------------------
# The `CHECK` on `balances_error`
# --------------------------------------------------------------------------------------


def test_the_check_matches_the_model(database_url: str, sync_engine: Engine) -> None:
    """Reflected off the migrated file: autogenerate has no check-constraint comparator."""
    upgrade_to_head(database_url)

    reflected = {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspect(sync_engine).get_check_constraints("exchange_accounts")
    }

    assert reflected["ck_exchange_accounts_balances_error"] == normalise_sql(
        _EXCHANGE_ACCOUNT_BALANCES_ERROR_CHECK
    )


def test_the_check_text_is_the_specs_and_the_enums() -> None:
    """Model against spec, and model against the enum the column holds.

    The vocabulary is the one an account's failed fill sync is recorded in, so that one
    failure means one thing wherever it is shown.
    """
    text_ = normalise_sql(_EXCHANGE_ACCOUNT_BALANCES_ERROR_CHECK)

    assert text_.startswith("balances_error IS NULL OR balances_error IN (")
    assert check_values(_EXCHANGE_ACCOUNT_BALANCES_ERROR_CHECK) == ERROR_KINDS
    assert {member.value for member in ExchangeSyncErrorKind} == ERROR_KINDS
    assert check_values(_EXCHANGE_SYNC_RUN_ACCOUNT_ERROR_KIND_CHECK) == ERROR_KINDS


@pytest.mark.parametrize("kind", [None, *sorted(ERROR_KINDS)])
def test_every_kind_and_null_is_stored(
    database_url: str, sync_engine: Engine, kind: str | None
) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        account = insert_account(connection, insert_user(connection))
        connection.execute(
            text("UPDATE exchange_accounts SET balances_error = :kind WHERE id = :id"),
            {"kind": kind, "id": account},
        )

    (row,) = all_rows(sync_engine, "exchange_accounts")
    assert row["balances_error"] == kind


@pytest.mark.parametrize(
    "kind",
    ["", "AUTH", "auth ", "ok", "unknown", "auth_failed", "timeout", "0"],
)
def test_anything_else_is_refused_by_the_table(
    database_url: str, sync_engine: Engine, kind: str
) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        account = insert_account(connection, insert_user(connection))

    with pytest.raises(IntegrityError, match="CHECK"), sync_engine.begin() as connection:
        connection.execute(
            text("UPDATE exchange_accounts SET balances_error = :kind WHERE id = :id"),
            {"kind": kind, "id": account},
        )

    (row,) = all_rows(sync_engine, "exchange_accounts")
    assert row["balances_error"] is None


# --------------------------------------------------------------------------------------
# Upgrade over data, and the reversal
# --------------------------------------------------------------------------------------


def test_the_upgrade_keeps_every_account_and_everything_that_references_it(
    database_url: str, sync_engine: Engine
) -> None:
    """The rebuild of `exchange_accounts` runs under a fill, a window and an outcome.

    `RESTRICT` from the fills means a rebuild with foreign keys enforced would be refused;
    `CASCADE` from the windows and the outcomes means it would delete the checkpoint and the
    run log. A rebuild that re-numbered the accounts would orphan all three. Compared row
    for row, before and after.
    """
    command.upgrade(build_alembic_config(database_url), PARENT)
    with sync_engine.begin() as connection:
        account = seed_history(connection)
    before = history(sync_engine)
    accounts_before = all_rows(sync_engine, "exchange_accounts")
    assert set(column_shapes(sync_engine, "exchange_accounts")) == set(
        EXPECTED_ACCOUNT_COLUMNS
    ) - set(NEW_ACCOUNT_COLUMNS)

    upgrade_to_head(database_url)

    assert history(sync_engine) == before
    assert all(len(rows) == 1 for rows in before.values()), "the seed wrote one row in each"
    (row,) = all_rows(sync_engine, "exchange_accounts")
    assert row == {**accounts_before[0], "balances_read_at": None, "balances_error": None}
    assert row["id"] == account
    assert row["sync_status"] == "ok"
    assert trigger_names(sync_engine) == TRIGGERS, "the fills are still append-only"
    assert all_rows(sync_engine, TABLE) == []
    with sync_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []


def test_the_downgrade_drops_the_table_and_the_two_columns_and_nothing_else(
    database_url: str, sync_engine: Engine
) -> None:
    """One step down and back up. What is lost is the last reading, and how it went."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        account = seed_history(connection)
        insert_balance(connection, account, "BTC", "0.250000000000000000")
        connection.execute(
            text("UPDATE exchange_accounts SET balances_read_at = :at, balances_error = 'schema'"),
            {"at": LATER},
        )
    before = history(sync_engine)
    tables_before = set(inspect(sync_engine).get_table_names())
    (account_before,) = all_rows(sync_engine, "exchange_accounts")

    command.downgrade(build_alembic_config(database_url), PARENT)

    # #24's revision sits on top of this one and comes down with it.
    assert set(inspect(sync_engine).get_table_names()) == tables_before - {
        TABLE,
        "derived_addresses",
    }
    assert set(column_shapes(sync_engine, "exchange_accounts")) == set(
        EXPECTED_ACCOUNT_COLUMNS
    ) - set(NEW_ACCOUNT_COLUMNS)
    assert history(sync_engine) == before
    (account_down,) = all_rows(sync_engine, "exchange_accounts")
    assert account_down == {
        name: value for name, value in account_before.items() if name not in NEW_ACCOUNT_COLUMNS
    }
    assert trigger_names(sync_engine) == TRIGGERS
    assert {
        str(found["name"])
        for found in inspect(sync_engine).get_check_constraints("exchange_accounts")
    } == {"ck_exchange_accounts_exchange_key", "ck_exchange_accounts_sync_status"}
    with sync_engine.connect() as connection:
        stamped = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    assert stamped == PARENT

    upgrade_to_head(database_url)

    assert set(inspect(sync_engine).get_table_names()) == tables_before
    assert history(sync_engine) == before
    assert all_rows(sync_engine, TABLE) == [], "the reading is read again, not restored"
    (account_up,) = all_rows(sync_engine, "exchange_accounts")
    assert account_up == {**account_down, "balances_read_at": None, "balances_error": None}
    assert trigger_names(sync_engine) == TRIGGERS


def test_the_migrations_copy_of_the_table_is_the_table_the_parent_left(
    database_url: str, sync_engine: Engine
) -> None:
    """`_exchange_accounts_before()` is written out by hand, and the rebuild trusts it.

    A column or a constraint missing from it would be dropped by the upgrade without a word.
    Compared with what `0009_manual_adjustments` actually leaves on disk.
    """
    command.upgrade(build_alembic_config(database_url), PARENT)
    inspector = inspect(sync_engine)
    declared = v0010_exchange_balances._exchange_accounts_before()

    assert {column.name: bool(column.nullable) for column in declared.columns} == {
        str(column["name"]): bool(column["nullable"])
        for column in inspector.get_columns("exchange_accounts")
    }
    on_disk = {
        str(found["name"])
        for found in (
            *inspector.get_check_constraints("exchange_accounts"),
            *inspector.get_unique_constraints("exchange_accounts"),
            *inspector.get_foreign_keys("exchange_accounts"),
            inspector.get_pk_constraint("exchange_accounts"),
        )
    }
    assert {str(constraint.name) for constraint in declared.constraints} == on_disk
