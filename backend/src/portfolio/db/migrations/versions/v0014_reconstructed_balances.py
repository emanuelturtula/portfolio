"""Reconstructed balances: each wallet's closing balance per UTC day, rebuilt (spec 038).

Revision ID: 0014_reconstructed_balances
Revises: 0013_price_history
Create Date: 2026-10-08

One new table and no alteration of an existing one, so the downgrade drops what this revision
created and nothing else. What it loses is derived data: the next rebuild reads every
transaction again and writes the same rows.

**The `CHECK` text below is duplicated verbatim from `_RECONSTRUCTED_BALANCE_CONFIRMED_CHECK`
in `db/models.py`**, and nothing mechanical compares them, for the reason `0003_wallets` gives;
the reflection tests hold them together.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import BaseUnits, UtcDateTime

revision: str = "0014_reconstructed_balances"
down_revision: str | None = "0013_price_history"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the table. Its unique key leads with `wallet_id`, so it is the index too."""
    op.create_table(
        "reconstructed_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wallet_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("confirmed", BaseUnits(), nullable=False),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("rebuilt_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint("confirmed >= 0", name=op.f("ck_reconstructed_balances_confirmed")),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallets.id"],
            name=op.f("fk_reconstructed_balances_wallet_id_wallets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconstructed_balances")),
        sa.UniqueConstraint("wallet_id", "day", name=op.f("uq_reconstructed_balances_wallet_day")),
    )


def downgrade() -> None:
    """Drop the table; the next rebuild after a re-upgrade writes it again."""
    op.drop_table("reconstructed_balances")
