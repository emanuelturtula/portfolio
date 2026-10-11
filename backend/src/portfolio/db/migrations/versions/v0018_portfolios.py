"""Portfolios: named groups of wallets and exchange operations (spec 044).

Revision ID: 0018_portfolios
Revises: 0017_manual_rewards_and_fees
Create Date: 2026-10-11

Three changes, in an order that never leaves a row without a portfolio:

* **`portfolios`** is created, and every account gets one named `Main`. Its timestamps are the
  account's own `created_at`, so the migration reads no clock and writes the column's own
  stored form.
* **`wallets` and `exchange_operations` gain `portfolio_id`**, added nullable, filled with the
  owner's `Main`, and only then rebuilt `NOT NULL` with the foreign key and an index. SQLite's
  `ALTER TABLE` can add none of the three, so batch mode rebuilds each table, from the
  definitions written out below rather than reflected, for the reasons `0011_extended_keys`
  gives. The rebuilds are safe for the reason it gives too: migrations run with foreign key
  enforcement off, so dropping `wallets` does not cascade into its snapshots.
* The foreign keys have **no `ON DELETE`**: a portfolio that holds rows cannot be deleted
  (R10), and the database refuses it again for a writer that skips the service.

The downgrade drops the two columns and the table. It loses the grouping and nothing else:
every wallet and every operation stays.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import NumericText, UtcDateTime

revision: str = "0018_portfolios"
down_revision: str | None = "0017_manual_rewards_and_fees"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_OWNERS_PORTFOLIO = (
    "SELECT portfolios.id FROM portfolios WHERE portfolios.user_id = {table}.user_id "
    "AND portfolios.name = 'Main'"
)


def _wallets(*, with_portfolio: bool) -> sa.Table:
    """`wallets` as `0011_extended_keys` left it, with a nullable `portfolio_id` if asked."""
    table = sa.Table(
        "wallets",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chain_key", sa.Text(), nullable=False),
        sa.Column("address_canonical", sa.Text(), nullable=False),
        sa.Column("address_display", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'address'"), nullable=False),
        sa.CheckConstraint("chain_key IN ('bitcoin', 'kaspa')", name="ck_wallets_chain_key"),
        sa.CheckConstraint("kind IN ('address', 'extended_key')", name="ck_wallets_kind"),
        sa.CheckConstraint(
            "kind = 'address' OR chain_key = 'bitcoin'", name="ck_wallets_kind_chain"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_wallets_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wallets"),
        sa.UniqueConstraint(
            "user_id",
            "chain_key",
            "address_canonical",
            name="uq_wallets_user_chain_address",
        ),
        sa.Index("ix_wallets_user_id", "user_id"),
    )
    if with_portfolio:
        table.append_column(sa.Column("portfolio_id", sa.Integer(), nullable=True))
    return table


def _operations(*, with_portfolio: bool) -> sa.Table:
    """`exchange_operations` as `0017_manual_rewards_and_fees` left it, with a nullable
    `portfolio_id` if asked."""
    table = sa.Table(
        "exchange_operations",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("executed_at", UtcDateTime(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("quantity", NumericText(18), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=True),
        sa.Column("quote_amount", NumericText(18), nullable=True),
        sa.Column("fee_asset", sa.Text(), nullable=True),
        sa.Column("fee_amount", NumericText(18), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('buy', 'sell', 'reward', 'deposit', 'withdrawal', 'transfer', 'other', "
            "'fee')",
            name="ck_exchange_operations_kind",
        ),
        sa.CheckConstraint(
            "source != 'manual' OR kind IN ('buy', 'sell', 'reward', 'fee')",
            name="ck_exchange_operations_manual",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["exchange_imports.id"],
            name="fk_exchange_operations_import_id_exchange_imports",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_exchange_operations_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exchange_operations"),
        sa.UniqueConstraint(
            "user_id", "source", "external_id", name="uq_exchange_operations_source_id"
        ),
        sa.Index("ix_exchange_operations_user_executed", "user_id", "executed_at"),
    )
    if with_portfolio:
        table.append_column(sa.Column("portfolio_id", sa.Integer(), nullable=True))
    return table


def _file_in_main(table: str, before: sa.Table) -> None:
    """Add `portfolio_id` to `table`, point every row at its owner's `Main`, then make it
    required, with its foreign key and index."""
    op.add_column(table, sa.Column("portfolio_id", sa.Integer(), nullable=True))
    op.execute(
        f"UPDATE {table} SET portfolio_id = ({_OWNERS_PORTFOLIO.format(table=table)})"  # noqa: S608
    )
    before.append_column(sa.Column("portfolio_id", sa.Integer(), nullable=True))
    with op.batch_alter_table(table, copy_from=before) as batch_op:
        batch_op.alter_column("portfolio_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(
            op.f(f"fk_{table}_portfolio_id_portfolios"), "portfolios", ["portfolio_id"], ["id"]
        )
        batch_op.create_index(op.f(f"ix_{table}_portfolio_id"), ["portfolio_id"])


def _unfile(table: str, after: sa.Table) -> None:
    """Drop `portfolio_id` from `table`, with its foreign key and index."""
    after.c.portfolio_id.nullable = False
    after.append_constraint(
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"], name=f"fk_{table}_portfolio_id_portfolios"
        )
    )
    after.append_constraint(sa.Index(f"ix_{table}_portfolio_id", "portfolio_id"))
    with op.batch_alter_table(table, copy_from=after) as batch_op:
        batch_op.drop_index(op.f(f"ix_{table}_portfolio_id"))
        batch_op.drop_constraint(op.f(f"fk_{table}_portfolio_id_portfolios"), type_="foreignkey")
        batch_op.drop_column("portfolio_id")


def upgrade() -> None:
    """Create `portfolios`, give every account a `Main`, and file every row in it."""
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_portfolios_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolios")),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_portfolios_user_name")),
    )
    op.execute(
        "INSERT INTO portfolios (user_id, name, created_at, updated_at) "
        "SELECT id, 'Main', created_at, created_at FROM users ORDER BY id"
    )
    _file_in_main("wallets", _wallets(with_portfolio=False))
    _file_in_main("exchange_operations", _operations(with_portfolio=False))


def downgrade() -> None:
    """Drop the two columns, then the table. Every wallet and operation stays."""
    _unfile("exchange_operations", _operations(with_portfolio=True))
    _unfile("wallets", _wallets(with_portfolio=True))
    op.drop_table("portfolios")
