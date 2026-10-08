"""Price history: one row per asset, quote currency and UTC day (spec 037).

Revision ID: 0013_price_history
Revises: 0012_drop_exchanges_accounting
Create Date: 2026-10-08

One new table and no alteration of an existing one, so the downgrade drops what this
revision created and nothing else. What it loses is the history itself: the hourly refresh
writes today's row again on its next run, and the backfill writes back every close Kraken
still serves, so a re-upgrade recovers the last 720 days and nothing older.

**The `CHECK` texts below are duplicated verbatim from `db/models.py`**
(`_PRICE_QUOTE_CURRENCY_CHECK`, `_PRICE_HISTORY_BASIS_CHECK`), and nothing mechanical compares
them, for the reason `0003_wallets` gives. The reflection tests hold them together. `amount`
is `NumericText(12)`, a literal rather than `PRICE_SCALE`, because a migration describes the
schema as it was on the day it ran.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0013_price_history"
down_revision: str | None = "0012_drop_exchanges_accounting"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create `price_history`. Its unique key leads with `asset_id`, so it is the index too."""
    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("amount", NumericText(12), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("recorded_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "quote_currency IN ('EUR', 'USD')", name=op.f("ck_price_history_quote_currency")
        ),
        sa.CheckConstraint("basis IN ('close', 'observed')", name=op.f("ck_price_history_basis")),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.id"], name=op.f("fk_price_history_asset_id_assets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_history")),
        sa.UniqueConstraint(
            "asset_id", "quote_currency", "day", name=op.f("uq_price_history_asset_day")
        ),
    )


def downgrade() -> None:
    """Drop the table; see the module docstring for what that loses."""
    op.drop_table("price_history")
