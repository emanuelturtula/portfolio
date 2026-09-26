"""Migration `0007_exchange_sync`: the append-only triggers, the three tables, the reversal.

Synchronous, like `test_migrations.py`, for the reason that module gives: Alembic's async
`env.py` calls `asyncio.run`. The schema is read back through a second, unconfigured engine,
so what is asserted is what is on disk.

## Append-only is a property of the database

Criterion 2's "resync is append-only" is first a property of the code -- no path updates or
deletes a fill -- and the triggers make it a property of the file as well: a hand edit on
the Pi, a future repository with a bug, a backfill script, all meet `RAISE(ABORT)`.

**Alembic's batch mode does not recreate triggers.** A later migration that rebuilds
`exchange_fills` with `batch_alter_table` drops both without a word, and nothing else in the
schema tooling notices: the drift check compares tables, not triggers. So the triggers'
`sqlite_master.sql` is compared with the migration's own constants, and
`test_a_batch_rebuild_of_the_fills_table_loses_the_triggers` shows the rebuild really does
lose them -- the reason the comparison is the guard.

## The reversal loses bookkeeping, never fills

The downgrade drops the triggers, the three tables and the five columns, and leaves every
fill as it was. That is asserted with rows present on both sides of it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import pytest
from alembic import command
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from portfolio.db.alembic_config import MIGRATIONS_DIR, build_alembic_config, upgrade_to_head
from portfolio.db.migrations.versions import v0007_exchange_sync
from portfolio.db.models import (
    _EXCHANGE_ACCOUNT_SYNC_STATUS_CHECK,
    _EXCHANGE_SYNC_RUN_ACCOUNT_ERROR_KIND_CHECK,
    _EXCHANGE_SYNC_RUN_ACCOUNT_STATUS_CHECK,
    _SYNC_RUN_STATUS_CHECK,
    _SYNC_RUN_TRIGGER_CHECK,
)
from portfolio.domain.exchanges import AccountSyncStatus
from portfolio.repositories.exchange_sync_runs import AccountOutcomeStatus, ExchangeSyncErrorKind

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

REVISION: Final = "0007_exchange_sync"
PARENT: Final = "0006_exchanges"
NEW_TABLES: Final = frozenset(
    {"exchange_sync_windows", "exchange_sync_runs", "exchange_sync_run_accounts"}
)
NEW_ACCOUNT_COLUMNS: Final = (
    "sync_status",
    "requested_since",
    "effective_since",
    "planned_until",
    "last_synced_at",
)
TRIGGERS: Final = frozenset({"exchange_fills_no_update", "exchange_fills_no_delete"})
APPEND_ONLY: Final = "exchange_fills is append-only"
AT: Final = "2026-09-25 12:00:00.000000"

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


def seed_owner_account_and_fill(connection: Connection) -> tuple[int, int, int]:
    """One owner, one Bitget account and one fill, by raw SQL. Returns their ids."""
    user = connection.execute(
        text(
            "INSERT INTO users (username, password_hash, created_at) "
            "VALUES ('owner', 'not-a-hash', :at) RETURNING id"
        ),
        {"at": AT},
    ).scalar_one()
    account = connection.execute(
        text(
            "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
            "VALUES (:user, 'bitget', :at) RETURNING id"
        ),
        {"user": user, "at": AT},
    ).scalar_one()
    connection.execute(text(FILL_INSERT), {"account": account, "trade": "1001", "at": AT})
    fill = connection.execute(text("SELECT id FROM exchange_fills")).scalar_one()
    return int(user), int(account), int(fill)


def fills(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(text("SELECT * FROM exchange_fills ORDER BY id"))
            .mappings()
            .all()
        ]


def triggers(engine: Engine) -> dict[str, dict[str, str]]:
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'")
        ).mappings()
        return {str(row["name"]): {"table": row["tbl_name"], "sql": row["sql"]} for row in found}


def columns(engine: Engine, table: str) -> dict[str, dict[str, Any]]:
    return {str(column["name"]): dict(column) for column in inspect(engine).get_columns(table)}


# --------------------------------------------------------------------------------------
# The revision
# --------------------------------------------------------------------------------------


def test_the_revision_sits_directly_on_top_of_the_exchanges_one() -> None:
    """Adjacency rather than the head, for the reason `test_migrations.py` gives."""
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert REVISION in revisions
    assert revisions.index(REVISION) == revisions.index(PARENT) - 1
    assert v0007_exchange_sync.revision == REVISION
    assert v0007_exchange_sync.down_revision == PARENT


# --------------------------------------------------------------------------------------
# Append-only
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "assignment",
    [
        "quantity = '9.000000000000000000'",
        "raw_payload = '{}'",
        "ingested_at = '2027-01-01 00:00:00.000000'",
        "external_trade_id = external_trade_id",
    ],
    ids=["an amount", "the payload", "our own clock", "a no-op"],
)
def test_updating_a_fill_is_refused(
    database_url: str, sync_engine: Engine, assignment: str
) -> None:
    """Every column, even a write of the same value: `BEFORE UPDATE` fires on the statement."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        seed_owner_account_and_fill(connection)
    before = fills(sync_engine)

    with pytest.raises(DBAPIError, match=APPEND_ONLY), sync_engine.begin() as connection:
        # The assignment is one of four literals above, never input.
        connection.execute(text(f"UPDATE exchange_fills SET {assignment}"))  # noqa: S608

    assert fills(sync_engine) == before


def test_deleting_a_fill_is_refused(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        seed_owner_account_and_fill(connection)
    before = fills(sync_engine)

    with pytest.raises(DBAPIError, match=APPEND_ONLY), sync_engine.begin() as connection:
        connection.execute(text("DELETE FROM exchange_fills"))

    assert fills(sync_engine) == before


def test_inserting_a_fill_is_still_allowed(database_url: str, sync_engine: Engine) -> None:
    """The control: the triggers refuse changes, not the log growing."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        _user, account, _fill = seed_owner_account_and_fill(connection)
        connection.execute(text(FILL_INSERT), {"account": account, "trade": "1002", "at": AT})

    assert [row["external_trade_id"] for row in fills(sync_engine)] == ["1001", "1002"]


def test_the_triggers_match_the_migration_text(database_url: str, sync_engine: Engine) -> None:
    """Exactly two triggers, on `exchange_fills`, each stored as the migration wrote it.

    Compared with the migration's own constants, so a later migration that rebuilt the
    table and forgot them -- or recreated them with different text -- fails here.
    """
    upgrade_to_head(database_url)

    found = triggers(sync_engine)

    assert set(found) == TRIGGERS
    assert {entry["table"] for entry in found.values()} == {"exchange_fills"}
    assert normalise_sql(found["exchange_fills_no_update"]["sql"]) == normalise_sql(
        v0007_exchange_sync.EXCHANGE_FILLS_NO_UPDATE_TRIGGER
    )
    assert normalise_sql(found["exchange_fills_no_delete"]["sql"]) == normalise_sql(
        v0007_exchange_sync.EXCHANGE_FILLS_NO_DELETE_TRIGGER
    )


def test_the_trigger_texts_are_the_specs() -> None:
    """The model of each trigger, pinned from the spec: before the event, on the table, abort."""
    for constant, event in (
        (v0007_exchange_sync.EXCHANGE_FILLS_NO_UPDATE_TRIGGER, "UPDATE"),
        (v0007_exchange_sync.EXCHANGE_FILLS_NO_DELETE_TRIGGER, "DELETE"),
    ):
        sql = normalise_sql(constant)
        assert f"BEFORE {event} ON exchange_fills" in sql
        assert f"SELECT RAISE(ABORT, '{APPEND_ONLY}')" in sql
        assert sql.startswith("CREATE TRIGGER exchange_fills_no_")


def test_a_batch_rebuild_of_the_fills_table_loses_the_triggers(
    database_url: str, sync_engine: Engine
) -> None:
    """Why the comparison above is the guard: `batch_alter_table` drops them silently.

    A rebuild of `exchange_fills` -- what any future column change on SQLite is -- leaves the
    table without a trigger and the drift check green. Measured here rather than asserted
    from the Alembic documentation, so the day Alembic starts carrying triggers across a
    rebuild this test says so.
    """
    upgrade_to_head(database_url)
    assert set(triggers(sync_engine)) == TRIGGERS

    with sync_engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("exchange_fills", recreate="always"):
            pass

    assert triggers(sync_engine) == {}, "a batch rebuild kept the triggers"


# --------------------------------------------------------------------------------------
# The schema
# --------------------------------------------------------------------------------------


def test_the_account_columns_are_added_with_their_defaults(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)

    found = columns(sync_engine, "exchange_accounts")

    assert found["sync_status"]["nullable"] is False
    assert "never_synced" in str(found["sync_status"]["default"])
    for name in NEW_ACCOUNT_COLUMNS[1:]:
        assert found[name]["nullable"] is True, name


def test_the_new_tables_have_the_specs_columns(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)

    windows = columns(sync_engine, "exchange_sync_windows")
    runs = columns(sync_engine, "exchange_sync_runs")
    outcomes = columns(sync_engine, "exchange_sync_run_accounts")

    assert set(windows) == {"id", "exchange_account_id", "since", "until", "symbol", "cursor"}
    assert {name for name, column in windows.items() if column["nullable"]} == {
        "symbol",
        "cursor",
    }
    assert set(runs) == {
        "id",
        "trigger",
        "status",
        "started_at",
        "finished_at",
        "duration_ms",
        "accounts_total",
        "accounts_succeeded",
        "accounts_failed",
        "accounts_skipped",
    }
    assert {name for name, column in runs.items() if column["nullable"]} == {
        "finished_at",
        "duration_ms",
    }
    for counter in ("accounts_succeeded", "accounts_failed", "accounts_skipped"):
        assert str(runs[counter]["default"]).strip("'\"") == "0", counter
    assert set(outcomes) == {
        "id",
        "exchange_sync_run_id",
        "exchange_account_id",
        "status",
        "windows_completed",
        "pages",
        "fills_seen",
        "fills_inserted",
        "error_kind",
        "detail",
    }
    assert {name for name, column in outcomes.items() if column["nullable"]} == {
        "error_kind",
        "detail",
    }


def test_the_new_foreign_keys_cascade_and_the_indexes_are_the_specs(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    def rules(table: str) -> set[tuple[str, str, str | None]]:
        return {
            (fk["constrained_columns"][0], fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys(table)
        }

    assert rules("exchange_sync_windows") == {
        ("exchange_account_id", "exchange_accounts", "CASCADE")
    }
    assert rules("exchange_sync_run_accounts") == {
        ("exchange_sync_run_id", "exchange_sync_runs", "CASCADE"),
        ("exchange_account_id", "exchange_accounts", "CASCADE"),
    }
    assert rules("exchange_sync_runs") == set()
    assert {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("exchange_sync_windows")
    } == {("ix_exchange_sync_windows_exchange_account_id", ("exchange_account_id",))}
    assert {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("exchange_sync_runs")
    } == {("ix_exchange_sync_runs_started_at", ("started_at",))}
    assert inspector.get_indexes("exchange_sync_run_accounts") == []
    assert inspector.get_indexes("exchange_accounts") == []
    assert [
        (unique["name"], tuple(unique["column_names"]))
        for unique in inspector.get_unique_constraints("exchange_sync_run_accounts")
    ] == [
        (
            "uq_exchange_sync_run_accounts_run_account",
            ("exchange_sync_run_id", "exchange_account_id"),
        )
    ]


def test_the_new_check_constraints_match_the_models(database_url: str, sync_engine: Engine) -> None:
    """Every `CHECK` #15 adds, reflected off the migrated file and compared with its constant.

    The hazard `test_migrations.py` documents for each earlier one: autogenerate has no
    check-constraint comparator. `exchange_sync_runs` reuses the `sync_runs` constants --
    one vocabulary, one spelling -- and the reuse is asserted.
    """
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)
    expected = {
        "exchange_accounts": {
            "ck_exchange_accounts_exchange_key": "exchange_key IN ('bingx', 'bitget')",
            "ck_exchange_accounts_sync_status": _EXCHANGE_ACCOUNT_SYNC_STATUS_CHECK,
        },
        "exchange_sync_runs": {
            "ck_exchange_sync_runs_trigger": _SYNC_RUN_TRIGGER_CHECK,
            "ck_exchange_sync_runs_status": _SYNC_RUN_STATUS_CHECK,
        },
        "exchange_sync_run_accounts": {
            "ck_exchange_sync_run_accounts_status": _EXCHANGE_SYNC_RUN_ACCOUNT_STATUS_CHECK,
            "ck_exchange_sync_run_accounts_error_kind": _EXCHANGE_SYNC_RUN_ACCOUNT_ERROR_KIND_CHECK,
        },
        "exchange_sync_windows": {},
    }

    for table, constraints in expected.items():
        reflected = {
            str(found["name"]): normalise_sql(str(found["sqltext"]))
            for found in inspector.get_check_constraints(table)
        }
        assert set(reflected) == set(constraints), table
        for name, sql in constraints.items():
            assert reflected[name] == normalise_sql(sql), f"{table}.{name}"


def test_the_new_check_texts_are_the_specs_and_the_enums() -> None:
    """Model against spec, and model against the enum each column holds."""
    assert _EXCHANGE_ACCOUNT_SYNC_STATUS_CHECK == (
        "sync_status IN ('auth_failed', 'error', 'never_synced', 'ok')"
    )
    assert _EXCHANGE_SYNC_RUN_ACCOUNT_STATUS_CHECK == "status IN ('failed', 'skipped', 'success')"
    assert check_values(_EXCHANGE_ACCOUNT_SYNC_STATUS_CHECK) == {
        member.value for member in AccountSyncStatus
    }
    assert check_values(_EXCHANGE_SYNC_RUN_ACCOUNT_STATUS_CHECK) == {
        member.value for member in AccountOutcomeStatus
    }
    kinds = {
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
    assert check_values(_EXCHANGE_SYNC_RUN_ACCOUNT_ERROR_KIND_CHECK) == kinds
    assert {member.value for member in ExchangeSyncErrorKind} == kinds
    assert normalise_sql(_EXCHANGE_SYNC_RUN_ACCOUNT_ERROR_KIND_CHECK).startswith(
        "error_kind IS NULL OR"
    )


# --------------------------------------------------------------------------------------
# The constraints refuse what they should, by insert
# --------------------------------------------------------------------------------------


def a_run(connection: Connection, *, trigger: str = "manual", status: str = "running") -> int:
    return int(
        connection.execute(
            text(
                "INSERT INTO exchange_sync_runs (trigger, status, started_at, accounts_total) "
                "VALUES (:trigger, :status, :at, 1) RETURNING id"
            ),
            {"trigger": trigger, "status": status, "at": AT},
        ).scalar_one()
    )


def an_outcome(
    connection: Connection, run: int, account: int, *, status: str, kind: str | None
) -> None:
    connection.execute(
        text(
            "INSERT INTO exchange_sync_run_accounts (exchange_sync_run_id, exchange_account_id, "
            "status, windows_completed, pages, fills_seen, fills_inserted, error_kind, detail) "
            "VALUES (:run, :account, :status, 0, 0, 0, 0, :kind, NULL)"
        ),
        {"run": run, "account": account, "status": status, "kind": kind},
    )


def test_every_legal_value_inserts(database_url: str, sync_engine: Engine) -> None:
    """The companion of the refusals below: each constraint admits its whole vocabulary."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        _user, account, _fill = seed_owner_account_and_fill(connection)
        for member in AccountSyncStatus:
            connection.execute(
                text("UPDATE exchange_accounts SET sync_status = :status"),
                {"status": member.value},
            )
        for trigger in ("scheduled", "manual", "startup"):
            for status in ("running", "success", "partial", "failed", "interrupted"):
                a_run(connection, trigger=trigger, status=status)
        an_outcome(connection, a_run(connection), account, status="success", kind=None)
        for index, kind in enumerate(sorted(member.value for member in ExchangeSyncErrorKind)):
            an_outcome(
                connection,
                a_run(connection),
                account,
                status=("failed", "skipped")[index % 2],
                kind=kind,
            )
        outcomes = connection.execute(
            text("SELECT COUNT(*) FROM exchange_sync_run_accounts")
        ).scalar()

    assert outcomes == 1 + len(ExchangeSyncErrorKind)


#: One illegal value per new `CHECK`. `:run` and `:account` are the seeded rows' ids.
REFUSALS: Final[dict[str, str]] = {
    "sync_status": "UPDATE exchange_accounts SET sync_status = 'paused'",
    "run trigger": (
        "INSERT INTO exchange_sync_runs (trigger, status, started_at, accounts_total) "
        "VALUES ('cron', 'running', '2026-09-25 12:00:00.000000', 1)"
    ),
    "run status": (
        "INSERT INTO exchange_sync_runs (trigger, status, started_at, accounts_total) "
        "VALUES ('manual', 'done', '2026-09-25 12:00:00.000000', 1)"
    ),
    "outcome status": (
        "INSERT INTO exchange_sync_run_accounts (exchange_sync_run_id, exchange_account_id, "
        "status, windows_completed, pages, fills_seen, fills_inserted) "
        "VALUES (:run, :account, 'partial', 0, 0, 0, 0)"
    ),
    "outcome error kind": (
        "INSERT INTO exchange_sync_run_accounts (exchange_sync_run_id, exchange_account_id, "
        "status, windows_completed, pages, fills_seen, fills_inserted, error_kind) "
        "VALUES (:run, :account, 'failed', 0, 0, 0, 0, 'response')"
    ),
}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_an_illegal_value_is_refused(database_url: str, sync_engine: Engine, case: str) -> None:
    """`response` is the balance sync's kind, and it is not an exchange one."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        _user, account, _fill = seed_owner_account_and_fill(connection)
        run = a_run(connection)

    with (
        pytest.raises(IntegrityError, match="CHECK constraint failed"),
        sync_engine.begin() as connection,
    ):
        connection.execute(text(REFUSALS[case]), {"run": run, "account": account})


def test_one_outcome_per_account_per_run(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        _user, account, _fill = seed_owner_account_and_fill(connection)
        run = a_run(connection)
        an_outcome(connection, run, account, status="success", kind=None)

    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        sync_engine.begin() as connection,
    ):
        an_outcome(connection, run, account, status="failed", kind="auth")


def test_removing_an_account_without_fills_takes_its_windows_and_outcomes(
    database_url: str, sync_engine: Engine
) -> None:
    """`CASCADE` from the account; the fills' `RESTRICT` is what keeps a traded account."""
    upgrade_to_head(database_url)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        user = connection.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES ('owner', 'x', :at) RETURNING id"
            ),
            {"at": AT},
        ).scalar_one()
        account = connection.execute(
            text(
                "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
                "VALUES (:user, 'bingx', :at) RETURNING id"
            ),
            {"user": user, "at": AT},
        ).scalar_one()
        connection.execute(
            text(
                'INSERT INTO exchange_sync_windows (exchange_account_id, "since", "until") '
                "VALUES (:account, :at, '2026-09-26 00:00:00.000000')"
            ),
            {"account": account, "at": AT},
        )
        run = a_run(connection)
        an_outcome(connection, run, int(account), status="success", kind=None)
        connection.execute(text("DELETE FROM exchange_accounts WHERE id = :id"), {"id": account})
        connection.commit()
        windows = connection.execute(text("SELECT COUNT(*) FROM exchange_sync_windows")).scalar()
        outcomes = connection.execute(
            text("SELECT COUNT(*) FROM exchange_sync_run_accounts")
        ).scalar()
        runs = connection.execute(text("SELECT COUNT(*) FROM exchange_sync_runs")).scalar()

    assert (windows, outcomes, runs) == (0, 0, 1), "the run itself is history and stays"


# --------------------------------------------------------------------------------------
# Upgrade over data, and the reversal
# --------------------------------------------------------------------------------------


def test_the_upgrade_keeps_every_account_and_fill_and_starts_them_never_synced(
    database_url: str, sync_engine: Engine
) -> None:
    """The batch rebuild of `exchange_accounts` runs under a fill that references it.

    `ON DELETE RESTRICT` from the fills means a rebuild that dropped and re-created the
    parent with foreign keys enforced would fail -- or, worse, one that re-numbered it would
    orphan every fill. Asserted with a row on each side.
    """
    config = build_alembic_config(database_url)
    command.upgrade(config, PARENT)
    with sync_engine.begin() as connection:
        _user, account, _fill = seed_owner_account_and_fill(connection)
    before = fills(sync_engine)

    upgrade_to_head(database_url)

    assert fills(sync_engine) == before
    with sync_engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT * FROM exchange_accounts WHERE id = :id"), {"id": account}
            )
            .mappings()
            .one()
        )
    assert row["sync_status"] == "never_synced"
    assert all(row[name] is None for name in NEW_ACCOUNT_COLUMNS[1:])
    assert set(triggers(sync_engine)) == TRIGGERS


def test_the_downgrade_leaves_the_fills_untouched(database_url: str, sync_engine: Engine) -> None:
    """Bookkeeping is lost; the history a cost basis is computed from is not."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        _user, account, _fill = seed_owner_account_and_fill(connection)
        connection.execute(
            text(
                "UPDATE exchange_accounts SET sync_status = 'ok', requested_since = :at, "
                "effective_since = :at, planned_until = :at, last_synced_at = :at"
            ),
            {"at": AT},
        )
        connection.execute(
            text(
                'INSERT INTO exchange_sync_windows (exchange_account_id, "since", "until", '
                "\"cursor\") VALUES (:account, :at, '2026-09-26 00:00:00.000000', '1001')"
            ),
            {"account": account, "at": AT},
        )
        run = a_run(connection, status="success")
        an_outcome(connection, run, account, status="success", kind=None)
    before = fills(sync_engine)

    command.downgrade(build_alembic_config(database_url), PARENT)

    assert fills(sync_engine) == before
    assert not NEW_TABLES & set(inspect(sync_engine).get_table_names())
    assert set(columns(sync_engine, "exchange_accounts")) == {
        "id",
        "user_id",
        "exchange_key",
        "created_at",
    }
    assert triggers(sync_engine) == {}
    with sync_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM exchange_accounts")).scalar() == 1

    upgrade_to_head(database_url)

    assert fills(sync_engine) == before
    assert set(triggers(sync_engine)) == TRIGGERS
    with sync_engine.connect() as connection:
        state = connection.execute(
            text("SELECT sync_status, planned_until FROM exchange_accounts")
        ).one()
    assert tuple(state) == ("never_synced", None), "a re-upgrade plans from scratch"
