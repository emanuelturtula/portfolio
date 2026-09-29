"""Manual adjustments: the inflows the owner records that no venue's history shows.

Revision ID: 0009_manual_adjustments
Revises: 0008_accounting
Create Date: 2026-09-29

One new table and no alteration of an existing one, so the downgrade drops what this
migration created and there is nothing to back-fill.

**`sqlite_autoincrement=True` is the point of the table's primary key.** An adjustment's id is
its identity in the replay (spec 023, *The event*), and without `AUTOINCREMENT` SQLite hands a
deleted row's id to the next insert. With it, `sqlite_sequence` remembers the largest id ever
used, so an id names one adjustment forever.

Two consequences of it, both measured rather than assumed:

* **The primary key has no name in the database.** SQLAlchemy renders an `AUTOINCREMENT` key
  inline -- `id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT` -- so the `pk_manual_adjustments`
  given below is dropped from the DDL, and reflection reports the key's name as `None`. Nothing
  refers to it by name.
* **A batch rebuild of this table loses `AUTOINCREMENT` unless it asks for it.** Alembic's
  `batch_alter_table` reflects the table and recreates it without the keyword, so a future
  migration that rebuilds it must pass `table_kwargs={"sqlite_autoincrement": True}`.

**The `CHECK` text below is duplicated verbatim from `_MANUAL_ADJUSTMENT_NOTE_CHECK` in
`db/models.py`, and nothing mechanical compares the two**, for the reason `0003_wallets` gives.
The reflection tests are what hold them together.

**Every amount is `NumericText(18)` and carries no `CHECK`**, for the reason `0006_exchanges`
gives: a sign check on a `TEXT` column is a comparison SQLite makes by numeric affinity, the
float coercion rule 2 forbids. The scale is a literal rather than `ADJUSTMENT_SCALE`, because a
migration describes the schema as it was on the day it ran.

**The downgrade loses data**, unlike `0008_accounting`'s: these rows are what the owner typed,
and no recompute rebuilds them. A rollback on the Pi runs after the host-side backup, which is
where they are recovered from; the snapshot the next startup computes simply leaves them out.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0009_manual_adjustments"
down_revision: str | None = "0008_accounting"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create `manual_adjustments` with its note check, its cascade and its owner index."""
    op.create_table(
        "manual_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        # The symbol exactly as the venues spell it; the service refuses anything else.
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("quantity", NumericText(18), nullable=False),
        # USD per unit. Null is an unknown cost, never zero.
        sa.Column("unit_cost", NumericText(18), nullable=True),
        # When the coins were acquired: what places the adjustment among the fills.
        sa.Column("occurred_at", UtcDateTime(), nullable=False),
        # Why, in the owner's words. Never logged.
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "trim(note) <> ''",
            name=op.f("ck_manual_adjustments_note_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_manual_adjustments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manual_adjustments")),
        sqlite_autoincrement=True,
    )
    op.create_index(
        op.f("ix_manual_adjustments_user_id"),
        "manual_adjustments",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the index and the table. Nothing references `manual_adjustments`."""
    op.drop_index(op.f("ix_manual_adjustments_user_id"), table_name="manual_adjustments")
    op.drop_table("manual_adjustments")
