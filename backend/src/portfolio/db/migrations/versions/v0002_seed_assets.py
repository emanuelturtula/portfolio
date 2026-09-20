"""Seed the assets the first release actually reads.

Revision ID: 0002_seed_assets
Revises: 0001_initial_schema
Create Date: 2026-09-20

BTC and KAS are the two chains balances are read from; USDT is the quote asset both
exchanges price spot pairs in. No fiat row is seeded: the display currency is a later
decision, and guessing it here would plant a row that someone has to migrate away.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import UtcDateTime

revision: str = "0002_seed_assets"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# symbol, name, decimals, kind
SEED_ROWS: tuple[tuple[str, str, int, str], ...] = (
    ("BTC", "Bitcoin", 8, "crypto"),
    ("KAS", "Kaspa", 8, "crypto"),
    ("USDT", "Tether", 6, "crypto"),
)

# A lightweight table clause rather than the mapped class: a migration has to keep working
# against the schema as it was at this revision, whatever the models later become.
assets_table = sa.table(
    "assets",
    sa.column("symbol", sa.Text()),
    sa.column("name", sa.Text()),
    sa.column("decimals", sa.Integer()),
    sa.column("kind", sa.Text()),
    sa.column("created_at", UtcDateTime()),
)


def upgrade() -> None:
    """Insert the seed rows."""
    now = datetime.now(tz=UTC)
    op.bulk_insert(
        assets_table,
        [
            {"symbol": symbol, "name": name, "decimals": decimals, "kind": kind, "created_at": now}
            for symbol, name, decimals, kind in SEED_ROWS
        ],
    )


def downgrade() -> None:
    """Delete exactly the rows this revision inserted, and nothing else."""
    symbols = [symbol for symbol, _name, _decimals, _kind in SEED_ROWS]
    op.execute(assets_table.delete().where(assets_table.c.symbol.in_(symbols)))
