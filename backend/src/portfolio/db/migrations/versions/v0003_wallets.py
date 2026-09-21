"""The wallet registry: one row per on-chain address balances are read from.

Revision ID: 0003_wallets
Revises: 0002_seed_assets
Create Date: 2026-09-21

Every constraint and index is named explicitly, with the names the metadata naming
convention produces, and `op.f()` marks each one as final so Alembic does not run the
convention over it a second time.

**The `CHECK` text below is duplicated verbatim from `_WALLET_CHAIN_KEY_CHECK` in
`db/models.py`, and nothing mechanical compares the two.** Alembic's autogenerate has no
check-constraint comparator at all, so editing one copy without the other passes ruff,
mypy, the layering contract and the drift check, and then rejects inserts in production.
What covers it is a test that reflects `ck_wallets_chain_key` off a migrated database and
compares its `sqltext` to the constant -- the same treatment `ck_assets_kind` already has.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import UtcDateTime

revision: str = "0003_wallets"
down_revision: str | None = "0002_seed_assets"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create `wallets` with its uniqueness rule, its chain check and its lookup index."""
    op.create_table(
        "wallets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chain_key", sa.Text(), nullable=False),
        # The two forms are stored side by side because one of them is case sensitive and
        # the other is not. See the `Wallet` docstring; the short version is that
        # lowercasing a Base58Check address produces a different address.
        sa.Column("address_canonical", sa.Text(), nullable=False),
        sa.Column("address_display", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        # Null means active. `DELETE` sets this rather than removing the row, so that the
        # balance snapshots that will reference `wallets.id` keep something to point at.
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint("chain_key IN ('bitcoin', 'kaspa')", name=op.f("ck_wallets_chain_key")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_wallets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallets")),
        # Deliberately not a partial index over unarchived rows, although SQLite would
        # allow it. A partial constraint would let re-adding a retired address quietly
        # resurrect it -- old label, old history -- while looking to the owner like a new
        # wallet. An archived row keeps its slot, so a re-add is a conflict and
        # un-archiving is an explicit act on a row the owner can already see.
        sa.UniqueConstraint(
            "user_id",
            "chain_key",
            "address_canonical",
            name=op.f("uq_wallets_user_chain_address"),
        ),
    )
    op.create_index(op.f("ix_wallets_user_id"), "wallets", ["user_id"], unique=False)


def downgrade() -> None:
    """Drop the index and the table. `wallets` has no children, so nothing precedes it."""
    op.drop_index(op.f("ix_wallets_user_id"), table_name="wallets")
    op.drop_table("wallets")
