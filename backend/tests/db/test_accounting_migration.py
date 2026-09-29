"""Migration `0008_accounting`: the four snapshot tables, their constraints, and the reversal.

Synchronous, like `test_migrations.py`, for the reason that module gives: Alembic's async
`env.py` calls `asyncio.run`. The schema is read back through a second, unconfigured engine,
so what is asserted is what is on disk.

## What is pinned

* **The columns and their nullability**, as spec 021's *Data model* table lists them. Every
  amount is `TEXT` (`NumericText(18)`), and only `average_cost` and `charged_to` may be null.
* **The cascade**, from the owner to the header and from the header to every child, proved by
  deleting rows rather than only by reflecting the rule: derived data goes with what it was
  derived for, and a recompute deletes one header to replace the whole snapshot.
* **The `CHECK`s against the model's constants**, because autogenerate has no check
  comparator (`test_migrations.py` documents the hazard), and the constants against the
  vocabularies they encode.
* **The reversal**, as one step: the four tables go, the fills they were derived from stay.
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
from portfolio.db.migrations.versions import v0008_accounting
from portfolio.db.models import (
    _ACCOUNTING_LOT_KIND_CHECK,
    _ACCOUNTING_WARNING_KIND_CHECK,
    AccountingLot,
    AccountingPosition,
    AccountingSnapshot,
    AccountingWarning,
)
from portfolio.repositories.accounting import AccountingWarningKind

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

REVISION: Final = "0008_accounting"
PARENT: Final = "0007_exchange_sync"
NEW_TABLES: Final = frozenset(
    {"accounting_snapshots", "accounting_positions", "accounting_lots", "accounting_warnings"}
)
AT: Final = "2026-09-29 10:00:00.000000"
ZERO: Final = "0.000000000000000000"

#: Spec 021's *Data model*: every column, its SQLite type, and whether it may be null.
EXPECTED_COLUMNS: Final[dict[str, dict[str, tuple[str, bool]]]] = {
    "accounting_snapshots": {
        "id": ("INTEGER", False),
        "user_id": ("INTEGER", False),
        "method": ("TEXT", False),
        "engine_version": ("INTEGER", False),
        "input_fingerprint": ("TEXT", False),
        "event_count": ("INTEGER", False),
        "unallocated_costs": ("TEXT", False),
        "computed_at": ("DATETIME", False),
    },
    "accounting_positions": {
        "id": ("INTEGER", False),
        "snapshot_id": ("INTEGER", False),
        "asset": ("TEXT", False),
        "quantity": ("TEXT", False),
        "unknown_basis_quantity": ("TEXT", False),
        "cost_basis": ("TEXT", False),
        "average_cost": ("TEXT", True),
        "realized_pnl": ("TEXT", False),
        "unmatched_proceeds": ("TEXT", False),
        "flags": ("TEXT", False),
    },
    "accounting_lots": {
        "id": ("INTEGER", False),
        "snapshot_id": ("INTEGER", False),
        "seq": ("INTEGER", False),
        "asset": ("TEXT", False),
        "occurred_at": ("DATETIME", False),
        "source": ("TEXT", False),
        "external_id": ("TEXT", False),
        "kind": ("TEXT", False),
        "quantity": ("TEXT", False),
        "cost_basis": ("TEXT", False),
        "unknown_basis_quantity": ("TEXT", False),
    },
    "accounting_warnings": {
        "id": ("INTEGER", False),
        "snapshot_id": ("INTEGER", False),
        "seq": ("INTEGER", False),
        "kind": ("TEXT", False),
        "occurred_at": ("DATETIME", False),
        "source": ("TEXT", False),
        "asset": ("TEXT", False),
        "quantity": ("TEXT", False),
        "charged_to": ("TEXT", True),
    },
}


def normalise_sql(expression: str) -> str:
    return " ".join(expression.split())


def check_values(expression: str) -> set[str]:
    inside = re.search(r"IN \((.*)\)", expression)
    assert inside is not None, expression
    return set(re.findall(r"'([^']*)'", inside.group(1)))


def seed_snapshot(connection: Connection, *, username: str = "owner") -> tuple[int, int]:
    """A user and a snapshot with one row in each child table. Returns their ids."""
    user: int = connection.execute(
        text(
            "INSERT INTO users (username, password_hash, created_at) "
            "VALUES (:name, 'not-a-hash', :at) RETURNING id"
        ),
        {"name": username, "at": AT},
    ).scalar_one()
    snapshot: int = connection.execute(
        text(
            "INSERT INTO accounting_snapshots (user_id, method, engine_version, "
            "input_fingerprint, event_count, unallocated_costs, computed_at) "
            "VALUES (:user, 'weighted_average', 1, 'f', 1, :zero, :at) RETURNING id"
        ),
        {"user": user, "zero": ZERO, "at": AT},
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO accounting_positions (snapshot_id, asset, quantity, "
            "unknown_basis_quantity, cost_basis, average_cost, realized_pnl, "
            "unmatched_proceeds, flags) VALUES (:s, 'BTC', :zero, :zero, :zero, NULL, :zero, "
            ":zero, '')"
        ),
        {"s": snapshot, "zero": ZERO},
    )
    insert_lot(connection, snapshot, seq=0)
    insert_warning(connection, snapshot, seq=0)
    return int(user), int(snapshot)


def insert_lot(connection: Connection, snapshot: int, *, seq: int, kind: str = "trade") -> None:
    connection.execute(
        text(
            "INSERT INTO accounting_lots (snapshot_id, seq, asset, occurred_at, source, "
            "external_id, kind, quantity, cost_basis, unknown_basis_quantity) VALUES "
            "(:s, :seq, 'BTC', :at, 'bitget', '1001', :kind, :zero, :zero, :zero)"
        ),
        {"s": snapshot, "seq": seq, "at": AT, "kind": kind, "zero": ZERO},
    )


def insert_warning(
    connection: Connection, snapshot: int, *, seq: int, kind: str = "negative_inventory"
) -> None:
    connection.execute(
        text(
            "INSERT INTO accounting_warnings (snapshot_id, seq, kind, occurred_at, source, "
            "asset, quantity, charged_to) VALUES (:s, :seq, :kind, :at, 'bitget', 'BTC', :zero, "
            "NULL)"
        ),
        {"s": snapshot, "seq": seq, "kind": kind, "at": AT, "zero": ZERO},
    )


def counts(connection: Connection) -> dict[str, int]:
    return {
        # The table name is one of four literals, never input.
        table: int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())  # noqa: S608
        for table in sorted(NEW_TABLES)
    }


# --------------------------------------------------------------------------------------
# The revision and its shape
# --------------------------------------------------------------------------------------


def test_the_revision_sits_directly_on_top_of_the_exchange_sync_one() -> None:
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert REVISION in revisions
    assert revisions.index(REVISION) == revisions.index(PARENT) - 1
    assert v0008_accounting.revision == REVISION
    assert v0008_accounting.down_revision == PARENT


def test_the_new_tables_have_the_specs_columns(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    for table, expected in EXPECTED_COLUMNS.items():
        found = {
            str(column["name"]): (str(column["type"]), bool(column["nullable"]))
            for column in inspector.get_columns(table)
        }
        assert found == expected, table


def test_the_models_name_the_same_tables() -> None:
    assert {
        AccountingSnapshot.__tablename__,
        AccountingPosition.__tablename__,
        AccountingLot.__tablename__,
        AccountingWarning.__tablename__,
    } == NEW_TABLES


def test_the_foreign_keys_cascade_from_the_owner_and_from_the_header(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    def rules(table: str) -> set[tuple[str, str, str | None]]:
        return {
            (fk["constrained_columns"][0], fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys(table)
        }

    assert rules("accounting_snapshots") == {("user_id", "users", "CASCADE")}
    for child in ("accounting_positions", "accounting_lots", "accounting_warnings"):
        assert rules(child) == {("snapshot_id", "accounting_snapshots", "CASCADE")}, child


def test_the_unique_constraints_are_the_specs(database_url: str, sync_engine: Engine) -> None:
    """One snapshot per owner and method; one position per asset; one row per `seq` (R3)."""
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    found = {
        table: {
            (str(unique["name"]), tuple(unique["column_names"]))
            for unique in inspector.get_unique_constraints(table)
        }
        for table in NEW_TABLES
    }

    assert found == {
        "accounting_snapshots": {("uq_accounting_snapshots_user_method", ("user_id", "method"))},
        "accounting_positions": {
            ("uq_accounting_positions_snapshot_asset", ("snapshot_id", "asset"))
        },
        "accounting_lots": {("uq_accounting_lots_snapshot_seq", ("snapshot_id", "seq"))},
        "accounting_warnings": {("uq_accounting_warnings_snapshot_seq", ("snapshot_id", "seq"))},
    }
    # No index beside them: each unique constraint leads with the column its lookup needs.
    for table in NEW_TABLES:
        assert inspector.get_indexes(table) == [], table


# --------------------------------------------------------------------------------------
# The CHECKs
# --------------------------------------------------------------------------------------


def test_the_check_constraints_match_the_models(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)
    expected = {
        "accounting_snapshots": {},
        "accounting_positions": {},
        "accounting_lots": {"ck_accounting_lots_kind": _ACCOUNTING_LOT_KIND_CHECK},
        "accounting_warnings": {"ck_accounting_warnings_kind": _ACCOUNTING_WARNING_KIND_CHECK},
    }

    for table, constraints in expected.items():
        reflected = {
            str(found["name"]): normalise_sql(str(found["sqltext"]))
            for found in inspector.get_check_constraints(table)
        }
        assert set(reflected) == set(constraints), table
        for name, sql in constraints.items():
            assert reflected[name] == normalise_sql(sql), f"{table}.{name}"


def test_the_check_texts_are_the_specs_and_the_vocabularies() -> None:
    """The warning kinds are the repository's enum; the lot kinds are the event kinds (R2)."""
    assert _ACCOUNTING_WARNING_KIND_CHECK == "kind IN ('negative_inventory', 'unattributed_fee')"
    assert check_values(_ACCOUNTING_WARNING_KIND_CHECK) == {
        member.value for member in AccountingWarningKind
    }
    assert _ACCOUNTING_LOT_KIND_CHECK == "kind IN ('adjustment', 'trade')"


@pytest.mark.parametrize(
    ("table", "kind"),
    [
        ("accounting_lots", "transfer"),
        ("accounting_lots", "TRADE"),
        ("accounting_warnings", "negative inventory"),
        ("accounting_warnings", ""),
    ],
)
def test_an_illegal_kind_is_refused(
    database_url: str, sync_engine: Engine, table: str, kind: str
) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        _user, snapshot = seed_snapshot(connection)

    insert = insert_lot if table == "accounting_lots" else insert_warning

    with pytest.raises(IntegrityError, match="CHECK"), sync_engine.begin() as connection:
        insert(connection, snapshot, seq=1, kind=kind)


def test_every_legal_kind_inserts(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        _user, snapshot = seed_snapshot(connection)
        insert_lot(connection, snapshot, seq=1, kind="adjustment")
        insert_warning(connection, snapshot, seq=1, kind="unattributed_fee")
        found = counts(connection)

    assert found["accounting_lots"] == 2
    assert found["accounting_warnings"] == 2


@pytest.mark.parametrize("table", ["accounting_lots", "accounting_warnings"])
def test_a_seq_is_unique_within_a_snapshot(
    database_url: str, sync_engine: Engine, table: str
) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        _user, snapshot = seed_snapshot(connection)

    insert = insert_lot if table == "accounting_lots" else insert_warning

    with pytest.raises(IntegrityError, match="UNIQUE"), sync_engine.begin() as connection:
        insert(connection, snapshot, seq=0)


def test_one_snapshot_per_owner_and_method(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user, _snapshot = seed_snapshot(connection)

    with pytest.raises(IntegrityError, match="UNIQUE"), sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO accounting_snapshots (user_id, method, engine_version, "
                "input_fingerprint, event_count, unallocated_costs, computed_at) "
                "VALUES (:user, 'weighted_average', 1, 'g', 0, :zero, :at)"
            ),
            {"user": user, "zero": ZERO, "at": AT},
        )


# --------------------------------------------------------------------------------------
# The cascade, by deleting rows
# --------------------------------------------------------------------------------------


def test_deleting_the_header_takes_every_child_with_it(
    database_url: str, sync_engine: Engine
) -> None:
    """What a recompute relies on: one `DELETE` of the header replaces the whole snapshot."""
    upgrade_to_head(database_url)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        _user, snapshot = seed_snapshot(connection)
        _other_user, other = seed_snapshot(connection, username="second")
        connection.execute(
            text("DELETE FROM accounting_snapshots WHERE id = :id"), {"id": snapshot}
        )
        connection.commit()
        remaining = counts(connection)
        survivors = {
            int(row[0])
            for table in ("accounting_positions", "accounting_lots", "accounting_warnings")
            # One of three literals, never input.
            for row in connection.execute(text(f"SELECT snapshot_id FROM {table}"))  # noqa: S608
        }

    assert remaining == dict.fromkeys(sorted(NEW_TABLES), 1)
    assert survivors == {other}, "only the deleted header's children went"


def test_deleting_the_owner_takes_the_whole_snapshot(
    database_url: str, sync_engine: Engine
) -> None:
    """Derived data cascades from `users`: it is recomputable, so nothing is lost."""
    upgrade_to_head(database_url)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        user, _snapshot = seed_snapshot(connection)
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})
        connection.commit()
        remaining = counts(connection)

    assert remaining == dict.fromkeys(sorted(NEW_TABLES), 0)


# --------------------------------------------------------------------------------------
# The reversal
# --------------------------------------------------------------------------------------


def fill_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(text("SELECT * FROM exchange_fills ORDER BY id"))
            .mappings()
            .all()
        ]


def test_the_downgrade_drops_the_four_tables_and_keeps_the_fills(
    database_url: str, sync_engine: Engine
) -> None:
    """One step down and back up: the snapshot goes, the history it came from stays."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user, _snapshot = seed_snapshot(connection)
        account: int = connection.execute(
            text(
                "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
                "VALUES (:user, 'bitget', :at) RETURNING id"
            ),
            {"user": user, "at": AT},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, "
                "external_order_id, symbol, base_asset, quote_asset, side, quantity, price, "
                "quote_quantity, quote_quantity_derived, fee_amount, fee_asset, executed_at, "
                "raw_payload, ingested_at) VALUES (:account, '1001', NULL, 'BTCUSDT', 'BTC', "
                "'USDT', 'buy', '0.500000000000000000', '60000.000000000000000000', "
                "'30000.000000000000000000', 0, :zero, NULL, :at, '{}', :at)"
            ),
            {"account": account, "zero": ZERO, "at": AT},
        )
    before = fill_rows(sync_engine)
    tables_before = set(inspect(sync_engine).get_table_names())

    command.downgrade(build_alembic_config(database_url), PARENT)

    after_down = set(inspect(sync_engine).get_table_names())
    # #18's revision sits on top of this one and comes down with it.
    assert after_down == tables_before - NEW_TABLES - {"manual_adjustments"}
    assert fill_rows(sync_engine) == before
    with sync_engine.connect() as connection:
        stamped = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert stamped == PARENT

    upgrade_to_head(database_url)

    assert set(inspect(sync_engine).get_table_names()) == tables_before
    assert fill_rows(sync_engine) == before
    with sync_engine.connect() as connection:
        assert counts(connection) == dict.fromkeys(sorted(NEW_TABLES), 0), "recomputed, not kept"
