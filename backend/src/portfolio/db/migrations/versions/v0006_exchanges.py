"""Exchange accounts and the spot fills imported from them.

Revision ID: 0006_exchanges
Revises: 0005_balances
Create Date: 2026-09-25

Two tables and no alteration of an existing one, so the downgrade drops what this migration
created, children first, and there is nothing to back-fill.

**Every `CHECK` text below is duplicated verbatim from `db/models.py` and nothing mechanical
compares the two.** Alembic's autogenerate has no check-constraint comparator at all, so
editing one copy without the other passes ruff, mypy, the layering contract and the drift
check, and then rejects inserts in production. What covers it is a test that reflects each
constraint off a migrated database and compares its `sqltext` against the model's constant --
the treatment every earlier `CHECK` already has.

**`uq_exchange_fills_account_trade` is criterion 7 of #12**, and the empty-string `CHECK` on
`external_trade_id` beside it is what makes it mean anything: two fills with an empty id
collide, and under #15's `ON CONFLICT DO NOTHING` the second would be dropped without a
word.

**The four amount columns are `NumericText(18)` and carry no `CHECK`, deliberately.**
`quantity > 0` on a `TEXT` column is a comparison SQLite performs by numeric affinity -- the
float coercion rule 2 forbids, applied inside the database. `NormalizedFill` enforces signs,
bounds and scale in Python, where they are exact. The scale is written as a literal rather
than imported as `FILL_SCALE`: a migration describes the schema as it was on the day it ran,
and a later change to the constant must not quietly rewrite what this revision creates.

**No column here holds a credential.** Exchange credentials come from the environment into
`SecretStr` and are never persisted (rule 3).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0006_exchanges"
down_revision: str | None = "0005_balances"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the account registry and the fill log, with the uniqueness criterion 7 asks for."""
    op.create_table(
        "exchange_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("exchange_key", sa.Text(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "exchange_key IN ('bingx', 'bitget')",
            name=op.f("ck_exchange_accounts_exchange_key"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_exchange_accounts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_accounts")),
        # One account per venue: credentials are one set per venue, read from the
        # environment, so no other configuration can exist yet.
        sa.UniqueConstraint(
            "user_id",
            "exchange_key",
            name=op.f("uq_exchange_accounts_user_exchange"),
        ),
    )
    op.create_table(
        "exchange_fills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange_account_id", sa.Integer(), nullable=False),
        sa.Column("external_trade_id", sa.Text(), nullable=False),
        sa.Column("external_order_id", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("base_asset", sa.Text(), nullable=False),
        sa.Column("quote_asset", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("quantity", NumericText(18), nullable=False),
        sa.Column("price", NumericText(18), nullable=False),
        # As the venue reported it, never recomputed; `quote_quantity_derived` says when the
        # venue omitted it and the provider had to derive it.
        sa.Column("quote_quantity", NumericText(18), nullable=False),
        sa.Column("quote_quantity_derived", sa.Boolean(), nullable=False),
        # Signed: positive is a fee paid, negative a rebate.
        sa.Column("fee_amount", NumericText(18), nullable=False),
        sa.Column("fee_asset", sa.Text(), nullable=True),
        # The venue's clock.
        sa.Column("executed_at", UtcDateTime(), nullable=False),
        # The venue's own fill object as canonical JSON -- never the envelope or the
        # request, which is where a key or a signature could be.
        sa.Column("raw_payload", sa.Text(), nullable=False),
        # Our clock.
        sa.Column("ingested_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "external_trade_id <> ''",
            name=op.f("ck_exchange_fills_external_trade_id"),
        ),
        sa.CheckConstraint(
            "side IN ('buy', 'sell')",
            name=op.f("ck_exchange_fills_side"),
        ),
        sa.CheckConstraint(
            "quote_quantity_derived IN (0, 1)",
            name=op.f("ck_exchange_fills_quote_quantity_derived"),
        ),
        # RESTRICT, not CASCADE: the fills are the history a cost basis is computed from,
        # and deleting an account must not take it with it.
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name=op.f("fk_exchange_fills_exchange_account_id_exchange_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_fills")),
        sa.UniqueConstraint(
            "exchange_account_id",
            "external_trade_id",
            name=op.f("uq_exchange_fills_account_trade"),
        ),
    )


def downgrade() -> None:
    """Drop both tables, the fills first, because they reference the accounts.

    **Not reversible without loss.** The fills are re-fetchable only for as long as each
    venue retains them, and a venue that keeps ninety days of history has already forgotten
    most of what this table held. Said out loud for the reason `0005_balances` says it: a
    reader moving between migrations should not carry `0004_prices`' reassurance across.
    """
    op.drop_table("exchange_fills")
    op.drop_table("exchange_accounts")
