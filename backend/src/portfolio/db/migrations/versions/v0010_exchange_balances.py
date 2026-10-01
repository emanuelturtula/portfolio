"""Exchange balances: the last reading of each account's spot balances, and how the read went.

Revision ID: 0010_exchange_balances
Revises: 0009_manual_adjustments
Create Date: 2026-10-01

Two changes, and one of them alters an existing table (spec 025, *Design: storage*):

* **`exchange_balances`**, new: one row per asset an account's spot account holds, as of its
  last good reading. A reading is replaced whole, so there is no history here.
* **`exchange_accounts` gains two columns** -- `balances_read_at`, when a read last succeeded,
  and `balances_error`, the kind the last attempt failed with -- through `batch_alter_table`,
  for the reason `0007_exchange_sync` gives: a named `CHECK` is something SQLite's
  `ALTER TABLE` cannot add, so batch mode rebuilds the table. The rebuild is safe for the
  reason given there too: `db/migration_guards.py` runs every migration with foreign key
  enforcement off, so `DROP TABLE exchange_accounts` neither is refused by the `RESTRICT` from
  `exchange_fills` nor cascades into `exchange_sync_windows` and
  `exchange_sync_run_accounts`, and dangling references are checked for before the commit.

**The rebuild does not touch `exchange_fills`**, so its two append-only triggers stay as
`0007_exchange_sync` created them. A trigger belongs to the table it is `ON`, and only a
rebuild of that table drops it.

**`exchange_accounts` has no `AUTOINCREMENT` to lose.** `0009_manual_adjustments` warns that
a batch rebuild drops the keyword; this table never had it.

**The `CHECK` text below is duplicated verbatim from `_EXCHANGE_ACCOUNT_BALANCES_ERROR_CHECK`
in `db/models.py`, and nothing mechanical compares the two**, for the reason `0003_wallets`
gives. The reflection tests are what hold them together. Its values are the
`ExchangeSyncErrorKind` members, the vocabulary `ck_exchange_sync_run_accounts_error_kind`
already holds.

**`quantity` is `NumericText(18)` and carries no `CHECK`**, for the reason `0006_exchanges`
gives: a sign check on a `TEXT` column is a comparison SQLite makes by numeric affinity, the
float coercion rule 2 forbids. `AssetBalance` refuses a negative amount where the venue's
answer is parsed. The scale is a literal rather than `FILL_SCALE`, because a migration
describes the schema as it was on the day it ran.

**Reversible, with no loss that matters.** The downgrade drops the table and the two columns.
What is lost is the last reading and how it went; the next successful exchange sync after a
re-upgrade reads the balances again. The fills are untouched.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0010_exchange_balances"
down_revision: str | None = "0009_manual_adjustments"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_BALANCES_ERROR_CHECK: Final = (
    "balances_error IS NULL OR "
    "balances_error IN ('auth', 'conflict', 'insufficient_scope', 'internal', "
    "'invalid_request', 'rate_limited', 'retention_window', 'schema', 'unavailable')"
)


def _exchange_accounts_before() -> sa.Table:
    """`exchange_accounts` exactly as `0007_exchange_sync` left it: the batch's `copy_from`.

    Written out rather than reflected, for the two reasons `0007_exchange_sync` gives: offline
    `--sql` mode has no connection to reflect through, and the rebuild then carries exactly
    the constraints named here, with their text, whatever the reflection of a `CHECK` would
    have recovered.
    """
    return sa.Table(
        "exchange_accounts",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("exchange_key", sa.Text(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column(
            "sync_status",
            sa.Text(),
            server_default=sa.text("'never_synced'"),
            nullable=False,
        ),
        sa.Column("requested_since", UtcDateTime(), nullable=True),
        sa.Column("effective_since", UtcDateTime(), nullable=True),
        sa.Column("planned_until", UtcDateTime(), nullable=True),
        sa.Column("last_synced_at", UtcDateTime(), nullable=True),
        sa.CheckConstraint(
            "exchange_key IN ('bingx', 'bitget')",
            name="ck_exchange_accounts_exchange_key",
        ),
        sa.CheckConstraint(
            "sync_status IN ('auth_failed', 'error', 'never_synced', 'ok')",
            name="ck_exchange_accounts_sync_status",
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
    table.append_column(sa.Column("balances_read_at", UtcDateTime(), nullable=True))
    table.append_column(sa.Column("balances_error", sa.Text(), nullable=True))
    table.append_constraint(
        sa.CheckConstraint(_BALANCES_ERROR_CHECK, name="ck_exchange_accounts_balances_error")
    )
    return table


def upgrade() -> None:
    """Add the two balance columns to the account, and the table of its balances."""
    with op.batch_alter_table(
        "exchange_accounts", copy_from=_exchange_accounts_before()
    ) as batch_op:
        # When a balance read last succeeded, on our clock. Null until one has.
        batch_op.add_column(sa.Column("balances_read_at", UtcDateTime(), nullable=True))
        # The kind the last attempt failed with. Null after a success, and before any attempt.
        batch_op.add_column(sa.Column("balances_error", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_exchange_accounts_balances_error"),
            _BALANCES_ERROR_CHECK,
        )

    op.create_table(
        "exchange_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange_account_id", sa.Integer(), nullable=False),
        # The venue's name for the asset, spelled as that venue's fills spell it.
        sa.Column("asset", sa.Text(), nullable=False),
        # The total held in the spot account. A zero balance has no row.
        sa.Column("quantity", NumericText(18), nullable=False),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name=op.f("fk_exchange_balances_exchange_account_id_exchange_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_balances")),
        sa.UniqueConstraint(
            "exchange_account_id",
            "asset",
            name=op.f("uq_exchange_balances_account_asset"),
        ),
    )


def downgrade() -> None:
    """Drop the balances table, then the two columns. Fills, windows and the run log stay."""
    op.drop_table("exchange_balances")
    with op.batch_alter_table(
        "exchange_accounts", copy_from=_exchange_accounts_after()
    ) as batch_op:
        batch_op.drop_constraint(op.f("ck_exchange_accounts_balances_error"), type_="check")
        batch_op.drop_column("balances_error")
        batch_op.drop_column("balances_read_at")
