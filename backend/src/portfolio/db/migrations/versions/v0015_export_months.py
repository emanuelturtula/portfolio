"""Export months: the months whose manual exchange exports are done (spec 040).

Revision ID: 0015_export_months
Revises: 0014_reconstructed_balances
Create Date: 2026-10-09

One new table and no alteration of an existing one, so the downgrade drops what this revision
created and nothing else. What it loses is which months were marked done; after a re-upgrade
every closed month is owed again until it is marked a second time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import UtcDateTime

revision: str = "0015_export_months"
down_revision: str | None = "0014_reconstructed_balances"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the table. Its unique key leads with `user_id`, so it is the index too."""
    op.create_table(
        "export_months",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("done_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_export_months_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_months")),
        sa.UniqueConstraint("user_id", "month", name=op.f("uq_export_months_user_month")),
    )


def downgrade() -> None:
    """Drop the table; every closed month is owed again after a re-upgrade."""
    op.drop_table("export_months")
