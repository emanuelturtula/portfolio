"""Migration `0009_manual_adjustments`: the table, its constraints, and the reversal (#18).

Synchronous, like `test_migrations.py`, for the reason that module gives: Alembic's async
`env.py` calls `asyncio.run`. The schema is read back through a second, unconfigured engine,
so what is asserted is what is on disk.

## What is pinned, and why each one

* **The columns and their nullability**, as spec 023's *Data model* lists them. Every amount
  is `TEXT` (`NumericText(18)`), and only `unit_cost` may be null: **null is unknown cost,
  never zero**, so a `NOT NULL` there would force a zero onto the owner's entry.
* **`AUTOINCREMENT`**, proved by behaviour and not only by the DDL: an id is the event's
  identity (`external_id` is the id padded to twenty digits), so an id reused after a delete
  would give a new adjustment the identity of an old one.
* **The named note `CHECK`**, against the model's constant, because autogenerate has no
  check comparator (`test_migrations.py` documents the hazard). Exercised with real inserts:
  an empty note and a run of spaces are refused, a real one is kept.
* **No `CHECK` on a money column.** A comparison on a `TEXT` money column coerces to float
  (rule 2), so the table has exactly one `CHECK`, the note's.
* **The cascade from `users`** and the index on `user_id`, proved by deleting a row.
* **The reversal**, as one step: the table goes, everything else stays, and back up again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.alembic_config import MIGRATIONS_DIR, build_alembic_config, upgrade_to_head
from portfolio.db.migrations.versions import v0009_manual_adjustments
from portfolio.db.models import _MANUAL_ADJUSTMENT_NOTE_CHECK, ManualAdjustment, metadata

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

REVISION: Final = "0009_manual_adjustments"
PARENT: Final = "0008_accounting"
TABLE: Final = "manual_adjustments"
AT: Final = "2026-09-29 10:00:00.000000"
ONE: Final = "1.000000000000000000"

#: Spec 023's *Data model*: every column, its SQLite type, and whether it may be null.
EXPECTED_COLUMNS: Final[dict[str, tuple[str, bool]]] = {
    "id": ("INTEGER", False),
    "user_id": ("INTEGER", False),
    "asset": ("TEXT", False),
    "quantity": ("TEXT", False),
    "unit_cost": ("TEXT", True),
    "occurred_at": ("DATETIME", False),
    "note": ("TEXT", False),
    "created_at": ("DATETIME", False),
    "updated_at": ("DATETIME", False),
}


def normalise_sql(expression: str) -> str:
    return " ".join(expression.split())


def insert_user(connection: Connection, username: str = "owner") -> int:
    user: int = connection.execute(
        text(
            "INSERT INTO users (username, password_hash, created_at) "
            "VALUES (:name, 'not-a-hash', :at) RETURNING id"
        ),
        {"name": username, "at": AT},
    ).scalar_one()
    return user


def insert_adjustment(
    connection: Connection,
    user: int,
    *,
    note: str = "Opening balance",
    unit_cost: str | None = ONE,
) -> int:
    adjustment: int = connection.execute(
        text(
            "INSERT INTO manual_adjustments (user_id, asset, quantity, unit_cost, occurred_at, "
            "note, created_at, updated_at) VALUES (:user, 'BTC', :one, :cost, :at, :note, :at, "
            ":at) RETURNING id"
        ),
        {"user": user, "one": ONE, "cost": unit_cost, "at": AT, "note": note},
    ).scalar_one()
    return adjustment


def adjustment_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(text("SELECT * FROM manual_adjustments ORDER BY id"))
            .mappings()
            .all()
        ]


# --------------------------------------------------------------------------------------
# The revision and its shape
# --------------------------------------------------------------------------------------


def test_the_revision_sits_directly_on_top_of_the_accounting_one() -> None:
    """Adjacency, not the head, for the reason `test_migrations.py` gives."""
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert REVISION in revisions
    assert revisions.index(REVISION) == revisions.index(PARENT) - 1
    assert v0009_manual_adjustments.revision == REVISION
    assert v0009_manual_adjustments.down_revision == PARENT


def test_the_model_names_the_table() -> None:
    assert ManualAdjustment.__tablename__ == TABLE


def test_the_table_has_the_specs_columns(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)

    found = {
        str(column["name"]): (str(column["type"]), bool(column["nullable"]))
        for column in inspect(sync_engine).get_columns(TABLE)
    }

    assert found == EXPECTED_COLUMNS


def test_the_owner_is_a_cascading_foreign_key_with_an_index(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    rules = {
        (fk["constrained_columns"][0], fk["referred_table"], fk["options"].get("ondelete"))
        for fk in inspector.get_foreign_keys(TABLE)
    }
    indexes = {
        (str(index["name"]), tuple(index["column_names"])) for index in inspector.get_indexes(TABLE)
    }

    assert rules == {("user_id", "users", "CASCADE")}
    assert indexes == {("ix_manual_adjustments_user_id", ("user_id",))}
    assert inspector.get_unique_constraints(TABLE) == []


def test_the_table_is_declared_autoincrement(database_url: str, sync_engine: Engine) -> None:
    """The DDL SQLite holds, and the model that must agree with it (spec 023)."""
    upgrade_to_head(database_url)
    with sync_engine.connect() as connection:
        ddl = connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": TABLE},
        )

    assert "AUTOINCREMENT" in str(ddl).upper()
    assert metadata.tables[TABLE].dialect_kwargs.get("sqlite_autoincrement") is True


def test_an_id_is_never_reused_after_a_delete(database_url: str, sync_engine: Engine) -> None:
    """The behaviour `AUTOINCREMENT` buys. Without it SQLite hands the highest id out again.

    Deleting the newest row is the case that tells the two apart: a plain `INTEGER PRIMARY
    KEY` picks `max(id) + 1`, which is the deleted row's id.
    """
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)
        first = insert_adjustment(connection, user)
        second = insert_adjustment(connection, user)
        connection.execute(text("DELETE FROM manual_adjustments WHERE id = :id"), {"id": second})
        third = insert_adjustment(connection, user)

    assert second == first + 1
    assert third == second + 1, "the deleted adjustment's id was handed out again"


# --------------------------------------------------------------------------------------
# The one CHECK: the note
# --------------------------------------------------------------------------------------


def test_the_only_check_is_the_named_note_check(database_url: str, sync_engine: Engine) -> None:
    """Named, so a batch rebuild can re-create it; and alone, so no money column has one."""
    upgrade_to_head(database_url)

    reflected = {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspect(sync_engine).get_check_constraints(TABLE)
    }

    assert reflected == {
        "ck_manual_adjustments_note_not_blank": normalise_sql(_MANUAL_ADJUSTMENT_NOTE_CHECK)
    }


def test_the_check_text_is_the_specs() -> None:
    assert normalise_sql(_MANUAL_ADJUSTMENT_NOTE_CHECK) == "trim(note) <> ''"


@pytest.mark.parametrize("note", ["", " ", "     "], ids=["empty", "one space", "spaces"])
def test_a_blank_note_is_refused_by_the_table(
    database_url: str, sync_engine: Engine, note: str
) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)

    with pytest.raises(IntegrityError, match="CHECK"), sync_engine.begin() as connection:
        insert_adjustment(connection, user, note=note)

    assert adjustment_rows(sync_engine) == []


def test_a_real_note_and_a_null_cost_insert(database_url: str, sync_engine: Engine) -> None:
    """The control for the refusals above, and `unit_cost` really is nullable on disk."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)
        insert_adjustment(connection, user, note="  Bought before the history began  ")
        insert_adjustment(connection, user, unit_cost=None)

    stored = adjustment_rows(sync_engine)
    assert [row["note"] for row in stored] == [
        "  Bought before the history began  ",
        "Opening balance",
    ]
    assert [row["unit_cost"] for row in stored] == [ONE, None]


# --------------------------------------------------------------------------------------
# The cascade, by deleting rows
# --------------------------------------------------------------------------------------


def test_deleting_the_owner_takes_only_their_adjustments(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        user = insert_user(connection)
        other = insert_user(connection, "second")
        insert_adjustment(connection, user)
        insert_adjustment(connection, user)
        kept = insert_adjustment(connection, other)
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})
        connection.commit()

    assert [row["id"] for row in adjustment_rows(sync_engine)] == [kept]


# --------------------------------------------------------------------------------------
# The reversal
# --------------------------------------------------------------------------------------


def test_the_downgrade_drops_the_table_and_nothing_else(
    database_url: str, sync_engine: Engine
) -> None:
    """One step down and back up: the table goes, every other table and row stays."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)
        insert_adjustment(connection, user)
    tables_before = set(inspect(sync_engine).get_table_names())

    command.downgrade(build_alembic_config(database_url), PARENT)

    # #104's revision sits on top of this one and comes down with it.
    assert set(inspect(sync_engine).get_table_names()) == tables_before - {
        TABLE,
        "exchange_balances",
    }
    with sync_engine.connect() as connection:
        stamped = connection.scalar(text("SELECT version_num FROM alembic_version"))
        owners = connection.scalar(text("SELECT COUNT(*) FROM users"))
    assert stamped == PARENT
    assert owners == 1

    upgrade_to_head(database_url)

    assert set(inspect(sync_engine).get_table_names()) == tables_before
    assert adjustment_rows(sync_engine) == []
