"""Extended keys: what a wallet's address columns hold, and the addresses derived from a key.

Revision ID: 0011_extended_keys
Revises: 0010_exchange_balances
Create Date: 2026-10-03

Two changes (spec 031, *Data model*):

* **`wallets` gains `kind`**, `address` or `extended_key`, with two named `CHECK`s: the kind
  is one of the two, and only a Bitcoin wallet may be an extended key. Every existing row
  becomes `address` through the server default. The column is added through
  `batch_alter_table`, for the reason `0007_exchange_sync` gives: a named `CHECK` is something
  SQLite's `ALTER TABLE` cannot add, so batch mode rebuilds the table.
* **`derived_addresses`**, new: one row per address derived from an extended-key wallet, so a
  rescan derives only what it has not derived before.

**The rebuild of `wallets` is safe for the reason `0010_exchange_balances` gives for
`exchange_accounts`.** `db/migration_guards.py` runs every migration with foreign key
enforcement off, so `DROP TABLE wallets` does not cascade into `balance_snapshots`, and
dangling references are checked for before the commit. `wallets` has no trigger to lose and
no `AUTOINCREMENT` keyword for the rebuild to drop.

**`ix_wallets_user_id` is in the `copy_from` table, and it has to be.** Dropping the old table
drops its index, and batch mode re-creates exactly the indexes the `copy_from` table carries.
Leaving it out would pass every test that only reads rows and lose the index the registry's
every lookup uses -- which the drift check would then report, correctly.

**The `CHECK` texts below are duplicated verbatim from `db/models.py`** --
`_WALLET_KIND_CHECK`, `_WALLET_KIND_CHAIN_CHECK` and the three `_DERIVED_ADDRESS_*_CHECK`
constants -- and nothing mechanical compares the two copies, for the reason `0003_wallets`
gives. The reflection tests are what hold them together.

**The downgrade refuses while any extended-key wallet exists** (R12), archived ones included.
The previous schema has no `kind`, so it would read the key as an address: the provider would
refuse it on every tick and fail the whole Bitcoin chain with it, every fifteen minutes,
until somebody noticed. With no such wallet the downgrade drops the table and the column, and
loses nothing the next sync after a re-upgrade cannot derive again. **Offline (`--sql`) the
downgrade refuses outright**: with no connection there is no way to know whether such a
wallet exists, and a script that might strand one is not one to hand an operator.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from alembic import context, op

from portfolio.db.migration_guards import MigrationIntegrityError
from portfolio.db.types import UtcDateTime

revision: str = "0011_extended_keys"
down_revision: str | None = "0010_exchange_balances"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_KIND_CHECK: Final = "kind IN ('address', 'extended_key')"
_KIND_CHAIN_CHECK: Final = "kind = 'address' OR chain_key = 'bitcoin'"
_BRANCH_CHECK: Final = "branch IN (0, 1)"
_CHILD_INDEX_CHECK: Final = "child_index >= 0 AND child_index < 2147483648"
_USED_CHECK: Final = "used IN (0, 1)"

DOWNGRADE_REFUSED_OFFLINE: Final = (
    "Refusing to render the downgrade below 0011_extended_keys as SQL: it must first check "
    "that no wallet holds an extended public key, and offline there is no database to ask. "
    "Run the downgrade against the database instead."
)
"""Why `alembic downgrade --sql` past this revision produces no script."""

DOWNGRADE_REFUSED: Final = (
    "Refusing to downgrade below 0011_extended_keys: {count} wallet(s), archived ones "
    "included, hold an extended public key. The previous schema would read each one as an "
    "address and fail the Bitcoin chain on every sync. Take a backup and delete those wallets "
    "first, as docs/operations.md, section 8, 'Deleting extended-key wallets before a "
    "downgrade', describes."
)
"""Why the downgrade stopped. A count and nothing else: never a key, never a wallet id."""


def _wallets_before() -> sa.Table:
    """`wallets` exactly as `0003_wallets` created it: the upgrade's `copy_from`.

    Written out rather than reflected, for the two reasons `0007_exchange_sync` gives: offline
    `--sql` mode has no connection to reflect through, and the rebuild then carries exactly
    the constraints and the index named here, with their text.
    """
    return sa.Table(
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
        sa.CheckConstraint("chain_key IN ('bitcoin', 'kaspa')", name="ck_wallets_chain_key"),
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


def _wallets_after() -> sa.Table:
    """`wallets` as this revision leaves it: the downgrade's `copy_from`."""
    table = _wallets_before()
    table.append_column(
        sa.Column("kind", sa.Text(), server_default=sa.text("'address'"), nullable=False)
    )
    table.append_constraint(sa.CheckConstraint(_KIND_CHECK, name="ck_wallets_kind"))
    table.append_constraint(sa.CheckConstraint(_KIND_CHAIN_CHECK, name="ck_wallets_kind_chain"))
    return table


def upgrade() -> None:
    """Add `kind` to every wallet, as `address`, and create the derived-address table."""
    with op.batch_alter_table("wallets", copy_from=_wallets_before()) as batch_op:
        # What the two address columns hold. Every existing row is an address.
        batch_op.add_column(
            sa.Column("kind", sa.Text(), server_default=sa.text("'address'"), nullable=False)
        )
        batch_op.create_check_constraint(op.f("ck_wallets_kind"), _KIND_CHECK)
        batch_op.create_check_constraint(op.f("ck_wallets_kind_chain"), _KIND_CHAIN_CHECK)

    op.create_table(
        "derived_addresses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wallet_id", sa.Integer(), nullable=False),
        # 0 is BIP44's receive branch, 1 its change branch.
        sa.Column("branch", sa.Integer(), nullable=False),
        sa.Column("child_index", sa.Integer(), nullable=False),
        # The canonical address, exactly as a registered wallet's would be stored.
        sa.Column("address_canonical", sa.Text(), nullable=False),
        # Whether the vendor ever reported a transaction for it. Never set back to false.
        sa.Column("used", sa.Boolean(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(_BRANCH_CHECK, name=op.f("ck_derived_addresses_branch")),
        sa.CheckConstraint(_CHILD_INDEX_CHECK, name=op.f("ck_derived_addresses_child_index")),
        sa.CheckConstraint(_USED_CHECK, name=op.f("ck_derived_addresses_used")),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallets.id"],
            name=op.f("fk_derived_addresses_wallet_id_wallets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_derived_addresses")),
        sa.UniqueConstraint(
            "wallet_id",
            "branch",
            "child_index",
            name=op.f("uq_derived_addresses_wallet_branch_index"),
        ),
    )


def downgrade() -> None:
    """Refuse while an extended-key wallet exists (R12); otherwise drop the table and `kind`.

    Raises:
        MigrationIntegrityError: offline, or while any wallet is an extended key. The
            message carries a count and never a key or an id.
    """
    if context.is_offline_mode():
        raise MigrationIntegrityError(DOWNGRADE_REFUSED_OFFLINE)
    # A count, which is not money: nothing is summed, ordered or compared as a number here.
    remaining: int = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM wallets WHERE kind = 'extended_key'"))
        .scalar_one()
    )
    if remaining:
        raise MigrationIntegrityError(DOWNGRADE_REFUSED.format(count=remaining))

    op.drop_table("derived_addresses")
    with op.batch_alter_table("wallets", copy_from=_wallets_after()) as batch_op:
        batch_op.drop_constraint(op.f("ck_wallets_kind_chain"), type_="check")
        batch_op.drop_constraint(op.f("ck_wallets_kind"), type_="check")
        batch_op.drop_column("kind")
