"""Exchange operations and the uploads that stored them (spec 042).

Revision ID: 0016_exchange_operations
Revises: 0015_price_hourly
Create Date: 2026-10-10

Two new tables and no alteration of an existing one, so the downgrade drops what this
revision created and nothing else. What it loses is every uploaded operation and the manual
entries; the uploads can be repeated, and the manual entries cannot.

**The `CHECK` texts below are duplicated verbatim from `db/models.py`**
(`_EXCHANGE_OPERATION_KIND_CHECK`, `_EXCHANGE_OPERATION_MANUAL_CHECK`), for the reason
`0003_wallets` gives. The amounts are `NumericText(18)`, a literal, for the reason
`0013_price_history` gives.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0016_exchange_operations"
down_revision: str | None = "0015_price_hourly"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create `exchange_imports`, then `exchange_operations`, which points at it."""
    op.create_table(
        "exchange_imports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("stored", sa.Integer(), nullable=False),
        sa.Column("already_stored", sa.Integer(), nullable=False),
        sa.Column("imported_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_exchange_imports_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_imports")),
    )
    op.create_table(
        "exchange_operations",
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
        sa.CheckConstraint(
            "kind IN ('buy', 'sell', 'reward', 'deposit', 'withdrawal', 'transfer', 'other')",
            name=op.f("ck_exchange_operations_kind"),
        ),
        sa.CheckConstraint(
            "source != 'manual' OR kind IN ('buy', 'sell')",
            name=op.f("ck_exchange_operations_manual"),
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["exchange_imports.id"],
            name=op.f("fk_exchange_operations_import_id_exchange_imports"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_exchange_operations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_operations")),
        sa.UniqueConstraint(
            "user_id", "source", "external_id", name=op.f("uq_exchange_operations_source_id")
        ),
    )
    op.create_index(
        "ix_exchange_operations_user_executed",
        "exchange_operations",
        ["user_id", "executed_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop both tables, the operations first; see the module docstring for what that loses."""
    op.drop_index("ix_exchange_operations_user_executed", table_name="exchange_operations")
    op.drop_table("exchange_operations")
    op.drop_table("exchange_imports")
