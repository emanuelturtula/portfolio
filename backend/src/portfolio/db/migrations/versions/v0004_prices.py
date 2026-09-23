"""The current price of each asset in each quote currency: one row per pair.

Revision ID: 0004_prices
Revises: 0003_wallets
Create Date: 2026-09-23

Every constraint is named explicitly, with the names the metadata naming convention
produces, and `op.f()` marks each one as final so Alembic does not run the convention over
it a second time.

**`amount` is `UtcDateTime`'s money-shaped sibling and not `sa.Numeric`.** `NumericText`
stores a canonical fixed-point string in a `TEXT` column; `sa.Numeric` on SQLite round-trips
every value through a C double and would silently destroy precision on the one column in
this schema where precision is the entire point. The scale is declared here as the literal
`12` rather than imported from `db/models.py`, because a migration is a historical record of
what the schema was at this revision: a later change to `PRICE_SCALE` must produce a new
migration, not retroactively alter this one.

**The `CHECK` text below is duplicated verbatim from `_PRICE_QUOTE_CURRENCY_CHECK` in
`db/models.py`, and nothing mechanical compares the two.** Alembic's autogenerate has no
check-constraint comparator at all, so editing one copy without the other passes ruff, mypy,
the layering contract and the drift check, and then rejects inserts in production. What
covers it is a test that reflects `ck_prices_quote_currency` off a migrated database and
compares its `sqltext` to the constant -- the same treatment `ck_assets_kind` and
`ck_wallets_chain_key` already have.

**No index beyond the unique constraint**, deliberately. The table holds one row per pair --
four today -- and every read is either the whole table or a lookup on
`(asset_id, quote_currency)`, which is exactly what `uq_prices_asset_currency` indexes. An
index on money would be worse than useless: `ORDER BY` on a `TEXT` money column coerces it
to a float, which is what this column type exists to prevent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0004_prices"
down_revision: str | None = "0003_wallets"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create `prices` with its one-row-per-pair rule and its currency check."""
    op.create_table(
        "prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=False),
        # TEXT, via the type decorator that renders a canonical fixed-point string. Never
        # `sa.Numeric`; see the module docstring.
        sa.Column("amount", NumericText(12), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        # The observation time, not a vendor's quote time: no vendor supplies one.
        sa.Column("as_of", UtcDateTime(), nullable=False),
        sa.Column("fetched_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "quote_currency IN ('EUR', 'USD')",
            name=op.f("ck_prices_quote_currency"),
        ),
        # No `ondelete`: nothing deletes an asset, and a cascade would discard a price
        # rather than refusing a delete that should not be happening.
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_prices_asset_id_assets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prices")),
        # What makes this the *current* price rather than a history: the refresh upserts
        # into this slot instead of appending a row per observation.
        sa.UniqueConstraint(
            "asset_id",
            "quote_currency",
            name=op.f("uq_prices_asset_currency"),
        ),
    )


def downgrade() -> None:
    """Drop the table. `prices` has no children, so nothing precedes it.

    Reversible without a data decision, which is the part worth stating: every row here is
    a cached copy of something a vendor will answer again on the next refresh, so dropping
    the table loses nothing that cannot be re-fetched in one request. That is not true of
    `wallets` and will not be true of a trade history, and it is why this migration needs
    no backup step and no warning.
    """
    op.drop_table("prices")
