"""Manual rewards and network fees (spec 043).

Revision ID: 0017_manual_rewards_and_fees
Revises: 0016_exchange_operations
Create Date: 2026-10-10

Two `CHECK`s on `exchange_operations` change and nothing else does:

* **`ck_exchange_operations_kind`** admits `fee`, a network fee no export lists, which only
  a manual entry carries.
* **`ck_exchange_operations_manual`** admits `reward` and `fee` beside `buy` and `sell`, so the
  owner can enter what a miner paid and what a withdrawal cost on its way.

SQLite cannot alter a `CHECK`, so batch mode rebuilds the table, from the definitions written
out below rather than reflected, for the reasons `0011_extended_keys` gives. **The `CHECK` texts
are duplicated verbatim from `db/models.py`**, for the reason `0003_wallets` gives.

The downgrade deletes every manual reward and fee first, since the older `CHECK`s refuse them:
those entries are lost and have to be entered again after upgrading.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0017_manual_rewards_and_fees"
down_revision: str | None = "0016_exchange_operations"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_KIND_BEFORE = "kind IN ('buy', 'sell', 'reward', 'deposit', 'withdrawal', 'transfer', 'other')"
_MANUAL_BEFORE = "source != 'manual' OR kind IN ('buy', 'sell')"
_KIND_AFTER = (
    "kind IN ('buy', 'sell', 'reward', 'deposit', 'withdrawal', 'transfer', 'other', 'fee')"
)
_MANUAL_AFTER = "source != 'manual' OR kind IN ('buy', 'sell', 'reward', 'fee')"


def _operations(kind_check: str, manual_check: str) -> sa.Table:
    """`exchange_operations` as `0016_exchange_operations` created it, with these `CHECK`s."""
    return sa.Table(
        "exchange_operations",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("executed_at", UtcDateTime(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("quantity", NumericText(18), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=True),
        sa.Column("quote_amount", NumericText(18), nullable=True),
        sa.Column("fee_asset", sa.Text(), nullable=True),
        sa.Column("fee_amount", NumericText(18), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(kind_check, name="ck_exchange_operations_kind"),
        sa.CheckConstraint(manual_check, name="ck_exchange_operations_manual"),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["exchange_imports.id"],
            name="fk_exchange_operations_import_id_exchange_imports",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_exchange_operations_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exchange_operations"),
        sa.UniqueConstraint(
            "user_id", "source", "external_id", name="uq_exchange_operations_source_id"
        ),
        sa.Index("ix_exchange_operations_user_executed", "user_id", "executed_at"),
    )


def _replace_checks(*, before: sa.Table, kind_check: str, manual_check: str) -> None:
    with op.batch_alter_table("exchange_operations", copy_from=before) as batch_op:
        batch_op.drop_constraint(op.f("ck_exchange_operations_kind"), type_="check")
        batch_op.drop_constraint(op.f("ck_exchange_operations_manual"), type_="check")
        batch_op.create_check_constraint(op.f("ck_exchange_operations_kind"), kind_check)
        batch_op.create_check_constraint(op.f("ck_exchange_operations_manual"), manual_check)


def upgrade() -> None:
    """Let a manual entry be a reward or a fee."""
    _replace_checks(
        before=_operations(_KIND_BEFORE, _MANUAL_BEFORE),
        kind_check=_KIND_AFTER,
        manual_check=_MANUAL_AFTER,
    )


def downgrade() -> None:
    """Delete the manual rewards and fees, then restore the older `CHECK`s."""
    op.execute(
        "DELETE FROM exchange_operations WHERE source = 'manual' AND kind IN ('reward', 'fee')"
    )
    _replace_checks(
        before=_operations(_KIND_AFTER, _MANUAL_AFTER),
        kind_check=_KIND_BEFORE,
        manual_check=_MANUAL_BEFORE,
    )
