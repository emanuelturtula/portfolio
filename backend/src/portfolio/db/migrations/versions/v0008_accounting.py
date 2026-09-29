"""Cost-basis snapshots: a header per owner and method, its positions, lots and warnings.

Revision ID: 0008_accounting
Revises: 0007_exchange_sync
Create Date: 2026-09-29

Four new tables and no alteration of an existing one, so the downgrade drops what this
migration created, children first, and there is nothing to back-fill. Everything here is
**derived data**: a recompute rebuilds it from `exchange_fills`, which is why the header
cascades from `users` and every child cascades from the header (spec 021, *Data model*).

**Every `CHECK` text below is duplicated verbatim from `db/models.py` and nothing mechanical
compares the two**, for the reason `0005_balances` and `0006_exchanges` give. The reflection
tests are what hold them together.

**Every amount is `NumericText(18)` and carries no `CHECK`**, for the reason `0006_exchanges`
gives: a sign check on a `TEXT` column is a comparison SQLite makes by numeric affinity, the
float coercion rule 2 forbids. The scale is a literal rather than `ACCOUNTING_SCALE`, because a
migration describes the schema as it was on the day it ran.

**`UNIQUE (snapshot_id, seq)` on the lots and the warnings** is the natural key, and it is the
index the cascade and the ordered read use (spec 021, R3). The positions' `UNIQUE
(snapshot_id, asset)` does the same for them, and the header's `UNIQUE (user_id, method)` for
the owner's lookup and the cascade from `users`.

**Reversible, with no loss that matters.** The downgrade drops the four tables; the next
startup after a re-upgrade recomputes the snapshot from the fills.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0008_accounting"
down_revision: str | None = "0007_exchange_sync"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def _snapshot_fk(table: str) -> sa.ForeignKeyConstraint:
    """A child's reference to its header: deleting the header takes the child with it."""
    return sa.ForeignKeyConstraint(
        ["snapshot_id"],
        ["accounting_snapshots.id"],
        name=op.f(f"fk_{table}_snapshot_id_accounting_snapshots"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    """Create the snapshot header and its three child tables."""
    op.create_table(
        "accounting_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("engine_version", sa.Integer(), nullable=False),
        # `AccountingResult.input_fingerprint`: equal means nothing needs writing.
        sa.Column("input_fingerprint", sa.Text(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("unallocated_costs", NumericText(18), nullable=False),
        # Our clock at the write; it does not move when a recompute changes nothing.
        sa.Column("computed_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_accounting_snapshots_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounting_snapshots")),
        # One current snapshot per owner and method.
        sa.UniqueConstraint(
            "user_id",
            "method",
            name=op.f("uq_accounting_snapshots_user_method"),
        ),
    )
    op.create_table(
        "accounting_positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("quantity", NumericText(18), nullable=False),
        sa.Column("unknown_basis_quantity", NumericText(18), nullable=False),
        sa.Column("cost_basis", NumericText(18), nullable=False),
        # Null with no known-cost quantity, or past the range (spec 019, R1).
        sa.Column("average_cost", NumericText(18), nullable=True),
        sa.Column("realized_pnl", NumericText(18), nullable=False),
        sa.Column("unmatched_proceeds", NumericText(18), nullable=False),
        # The sorted, comma-joined `PositionFlag` values; empty for none.
        sa.Column("flags", sa.Text(), nullable=False),
        _snapshot_fk("accounting_positions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounting_positions")),
        sa.UniqueConstraint(
            "snapshot_id",
            "asset",
            name=op.f("uq_accounting_positions_snapshot_asset"),
        ),
    )
    op.create_table(
        "accounting_lots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        # The lot's place in the result, in event order, from 0.
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("occurred_at", UtcDateTime(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        # A trade id for a fill. No endpoint reads it and no log line carries it.
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("quantity", NumericText(18), nullable=False),
        sa.Column("cost_basis", NumericText(18), nullable=False),
        sa.Column("unknown_basis_quantity", NumericText(18), nullable=False),
        sa.CheckConstraint(
            "kind IN ('adjustment', 'trade')",
            name=op.f("ck_accounting_lots_kind"),
        ),
        _snapshot_fk("accounting_lots"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounting_lots")),
        sa.UniqueConstraint(
            "snapshot_id",
            "seq",
            name=op.f("uq_accounting_lots_snapshot_seq"),
        ),
    )
    op.create_table(
        "accounting_warnings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("occurred_at", UtcDateTime(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("quantity", NumericText(18), nullable=False),
        # The position that leaves an unvalued fee out; null for a shortfall.
        sa.Column("charged_to", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('negative_inventory', 'unattributed_fee')",
            name=op.f("ck_accounting_warnings_kind"),
        ),
        _snapshot_fk("accounting_warnings"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounting_warnings")),
        sa.UniqueConstraint(
            "snapshot_id",
            "seq",
            name=op.f("uq_accounting_warnings_snapshot_seq"),
        ),
    )


def downgrade() -> None:
    """Drop the three child tables, then the header. Nothing else is touched."""
    op.drop_table("accounting_warnings")
    op.drop_table("accounting_lots")
    op.drop_table("accounting_positions")
    op.drop_table("accounting_snapshots")
