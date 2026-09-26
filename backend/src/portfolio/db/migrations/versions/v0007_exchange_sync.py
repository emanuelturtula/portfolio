"""Exchange sync state: account status, the pending-window queue, the run log, append-only fills.

Revision ID: 0007_exchange_sync
Revises: 0006_exchanges
Create Date: 2026-09-25

Four changes, and one of them alters an existing table:

* **`exchange_accounts` gains five columns** -- `sync_status` and the four instants that
  describe the planned history -- through `batch_alter_table`. Adding a named `CHECK` is
  something SQLite's `ALTER TABLE` cannot do, so batch mode rebuilds the table: create a
  copy, `INSERT ... SELECT`, `DROP TABLE`, rename. The rebuild is safe here only because
  `db/migration_guards.py` runs every migration with foreign key enforcement off -- with it
  on, `DROP TABLE exchange_accounts` would be refused by the `RESTRICT` from
  `exchange_fills` once a fill exists -- and checks for dangling references before it commits.
* **`exchange_sync_windows`**, the pending queue: one row per window not yet read, with the
  cursor of its next page.
* **`exchange_sync_runs` and `exchange_sync_run_accounts`**, the run log, the shape of
  `sync_runs`/`sync_run_chains`.
* **Two triggers make `exchange_fills` append-only.** Their SQL is in the two module
  constants below, and a reflection test compares `sqlite_master.sql` against them. **Alembic
  batch mode does not recreate triggers**: a later migration that rebuilds `exchange_fills`
  drops them without a word and has to create them again from these constants' text.

**Every `CHECK` text below is duplicated verbatim from `db/models.py` and nothing mechanical
compares the two**, for the reason `0005_balances` and `0006_exchanges` give. The reflection
tests are what hold them together.

**No `CHECK (since < until)` on the window queue**: it would compare two `TEXT` datetimes in
SQL. `FillWindow` refuses an inverted window when the row is read.

**Reversible, with loss of bookkeeping only.** The downgrade drops the triggers first, then
the three tables, then the five columns. The fills are untouched. What is lost is the run log
and the checkpoints; the next sync after a re-upgrade plans from scratch, and the unique
constraint makes the re-read free.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import UtcDateTime

revision: str = "0007_exchange_sync"
down_revision: str | None = "0006_exchanges"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

EXCHANGE_FILLS_NO_UPDATE_TRIGGER: Final = (
    "CREATE TRIGGER exchange_fills_no_update BEFORE UPDATE ON exchange_fills "
    "BEGIN SELECT RAISE(ABORT, 'exchange_fills is append-only'); END"
)
"""The trigger that refuses every `UPDATE` of a fill, exactly as `sqlite_master.sql` holds it.

A fill is the venue's record of a trade; a corrected one is refused as a conflict by the sync,
and an adjustment model is M4's. `RAISE(ABORT, ...)` rolls back the statement and surfaces as
an integrity error, with a message that quotes no value.
"""

EXCHANGE_FILLS_NO_DELETE_TRIGGER: Final = (
    "CREATE TRIGGER exchange_fills_no_delete BEFORE DELETE ON exchange_fills "
    "BEGIN SELECT RAISE(ABORT, 'exchange_fills is append-only'); END"
)
"""The trigger that refuses every `DELETE` of a fill, exactly as `sqlite_master.sql` holds it.

A `DROP TABLE` does not fire it: SQLite drops a table's triggers before the implicit
`DELETE FROM` a drop performs, which is what lets `0006_exchanges`' downgrade still drop the
table -- and this revision's downgrade drops both triggers first anyway.
"""

_TRIGGER_NAMES: Final = ("exchange_fills_no_delete", "exchange_fills_no_update")

_SYNC_STATUS_CHECK: Final = "sync_status IN ('auth_failed', 'error', 'never_synced', 'ok')"


def _exchange_accounts_before() -> sa.Table:
    """`exchange_accounts` exactly as `0006_exchanges` created it: the batch's `copy_from`.

    **Batch mode on SQLite rebuilds the table, and to rebuild it has to know it.** Without
    `copy_from` it reflects the live table, which offline `--sql` mode cannot do -- there is no
    connection -- and the generated script would not exist. Written out rather than reflected,
    the rebuild also stops depending on what the reflection of a `CHECK` happens to recover:
    every constraint the copy carries is named here, with its text.
    """
    return sa.Table(
        "exchange_accounts",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("exchange_key", sa.Text(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "exchange_key IN ('bingx', 'bitget')",
            name="ck_exchange_accounts_exchange_key",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_exchange_accounts_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exchange_accounts"),
        sa.UniqueConstraint(
            "user_id",
            "exchange_key",
            name="uq_exchange_accounts_user_exchange",
        ),
    )


def _exchange_accounts_after() -> sa.Table:
    """`exchange_accounts` as this revision leaves it: the downgrade's `copy_from`."""
    table = _exchange_accounts_before()
    table.append_column(
        sa.Column(
            "sync_status",
            sa.Text(),
            server_default=sa.text("'never_synced'"),
            nullable=False,
        )
    )
    for name in ("requested_since", "effective_since", "planned_until", "last_synced_at"):
        table.append_column(sa.Column(name, UtcDateTime(), nullable=True))
    table.append_constraint(
        sa.CheckConstraint(_SYNC_STATUS_CHECK, name="ck_exchange_accounts_sync_status")
    )
    return table


def upgrade() -> None:
    """Add the sync state, the queue, the run log, and the append-only triggers."""
    with op.batch_alter_table(
        "exchange_accounts", copy_from=_exchange_accounts_before()
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "sync_status",
                sa.Text(),
                server_default=sa.text("'never_synced'"),
                nullable=False,
            )
        )
        # What the owner asked for, as of the last plan. The configured value, never clamped.
        batch_op.add_column(sa.Column("requested_since", UtcDateTime(), nullable=True))
        # The floor of the planned history: once no window is pending, everything from here
        # to `planned_until` is held.
        batch_op.add_column(sa.Column("effective_since", UtcDateTime(), nullable=True))
        batch_op.add_column(sa.Column("planned_until", UtcDateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_synced_at", UtcDateTime(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_exchange_accounts_sync_status"),
            _SYNC_STATUS_CHECK,
        )

    op.create_table(
        "exchange_sync_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange_account_id", sa.Integer(), nullable=False),
        sa.Column("since", UtcDateTime(), nullable=False),
        sa.Column("until", UtcDateTime(), nullable=False),
        # Set only for a venue that asks per symbol.
        sa.Column("symbol", sa.Text(), nullable=True),
        # The next page's cursor, NULL for the window's first page. A trade id at Bitget.
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name=op.f("fk_exchange_sync_windows_exchange_account_id_exchange_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_sync_windows")),
    )
    op.create_index(
        op.f("ix_exchange_sync_windows_exchange_account_id"),
        "exchange_sync_windows",
        ["exchange_account_id"],
        unique=False,
    )

    op.create_table(
        "exchange_sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", UtcDateTime(), nullable=False),
        # Null while in flight and for an interrupted run, as in `sync_runs`.
        sa.Column("finished_at", UtcDateTime(), nullable=True),
        # Milliseconds from a monotonic clock.
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("accounts_total", sa.Integer(), nullable=False),
        sa.Column("accounts_succeeded", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("accounts_failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("accounts_skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'manual', 'startup')",
            name=op.f("ck_exchange_sync_runs_trigger"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed', 'interrupted')",
            name=op.f("ck_exchange_sync_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_sync_runs")),
    )
    op.create_index(
        op.f("ix_exchange_sync_runs_started_at"),
        "exchange_sync_runs",
        ["started_at"],
        unique=False,
    )

    op.create_table(
        "exchange_sync_run_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange_sync_run_id", sa.Integer(), nullable=False),
        sa.Column("exchange_account_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("windows_completed", sa.Integer(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("fills_seen", sa.Integer(), nullable=False),
        sa.Column("fills_inserted", sa.Integer(), nullable=False),
        sa.Column("error_kind", sa.Text(), nullable=True),
        # An exchange error's own message, a conflict's count-only message, or a type name.
        # Rendered by an endpoint, so nothing else may reach it.
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('failed', 'skipped', 'success')",
            name=op.f("ck_exchange_sync_run_accounts_status"),
        ),
        sa.CheckConstraint(
            "error_kind IS NULL OR "
            "error_kind IN ('auth', 'conflict', 'insufficient_scope', 'internal', "
            "'invalid_request', 'rate_limited', 'retention_window', 'schema', 'unavailable')",
            name=op.f("ck_exchange_sync_run_accounts_error_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name=op.f("fk_exchange_sync_run_accounts_exchange_account_id_exchange_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_sync_run_id"],
            ["exchange_sync_runs.id"],
            name=op.f("fk_exchange_sync_run_accounts_exchange_sync_run_id_exchange_sync_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_sync_run_accounts")),
        sa.UniqueConstraint(
            "exchange_sync_run_id",
            "exchange_account_id",
            name=op.f("uq_exchange_sync_run_accounts_run_account"),
        ),
    )

    op.execute(EXCHANGE_FILLS_NO_UPDATE_TRIGGER)
    op.execute(EXCHANGE_FILLS_NO_DELETE_TRIGGER)


def downgrade() -> None:
    """Drop the triggers, then the three tables, then the five columns. Fills are untouched.

    The triggers go first so that nothing below can be refused by them; nothing below touches
    `exchange_fills`, and dropping them first keeps that true for a later edit too.
    """
    for name in _TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("exchange_sync_run_accounts")
    op.drop_index(op.f("ix_exchange_sync_runs_started_at"), table_name="exchange_sync_runs")
    op.drop_table("exchange_sync_runs")
    op.drop_index(
        op.f("ix_exchange_sync_windows_exchange_account_id"),
        table_name="exchange_sync_windows",
    )
    op.drop_table("exchange_sync_windows")
    with op.batch_alter_table(
        "exchange_accounts", copy_from=_exchange_accounts_after()
    ) as batch_op:
        batch_op.drop_constraint(op.f("ck_exchange_accounts_sync_status"), type_="check")
        batch_op.drop_column("last_synced_at")
        batch_op.drop_column("planned_until")
        batch_op.drop_column("effective_since")
        batch_op.drop_column("requested_since")
        batch_op.drop_column("sync_status")
