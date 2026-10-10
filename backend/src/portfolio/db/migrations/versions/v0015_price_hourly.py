"""Hourly prices: one row per asset, quote currency and UTC hour (spec 041).

Revision ID: 0015_price_hourly
Revises: 0014_reconstructed_balances
Create Date: 2026-10-10

One new table and no alteration of an existing one, so the downgrade drops what this
revision created and nothing else. What it loses is the hourly closes, and the hourly price
timer stores the last 30 days of them again on its next run.

**The `CHECK` text below is duplicated verbatim from `db/models.py`**
(`_PRICE_QUOTE_CURRENCY_CHECK`), for the reason `0003_wallets` gives. `amount` is
`NumericText(12)`, a literal, for the reason `0013_price_history` gives.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0015_price_hourly"
down_revision: str | None = "0014_reconstructed_balances"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create `price_hourly`. Its unique key leads with `asset_id`, so it is the index too."""
    op.create_table(
        "price_hourly",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=False),
        sa.Column("hour", UtcDateTime(), nullable=False),
        sa.Column("amount", NumericText(12), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("recorded_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "quote_currency IN ('EUR', 'USD')", name=op.f("ck_price_hourly_quote_currency")
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.id"], name=op.f("fk_price_hourly_asset_id_assets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_hourly")),
        sa.UniqueConstraint(
            "asset_id", "quote_currency", "hour", name=op.f("uq_price_hourly_asset_hour")
        ),
    )


def downgrade() -> None:
    """Drop the table; see the module docstring for what that loses."""
    op.drop_table("price_hourly")
