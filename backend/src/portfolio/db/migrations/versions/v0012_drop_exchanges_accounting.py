"""Drop the exchange, accounting and manual-adjustment tables (spec 036).

Revision ID: 0012_drop_exchanges_accounting
Revises: 0011_extended_keys
Create Date: 2026-10-08

The application no longer imports exchange fills, reads exchange balances, computes a cost
basis or records manual adjustments, so the eleven tables those features wrote, and the two
append-only triggers on `exchange_fills`, are dropped:

* from `0006_exchanges`: `exchange_accounts`, `exchange_fills`;
* from `0007_exchange_sync`: `exchange_sync_windows`, `exchange_sync_runs`,
  `exchange_sync_run_accounts`, and the triggers `exchange_fills_no_update` and
  `exchange_fills_no_delete`;
* from `0008_accounting`: `accounting_snapshots`, `accounting_positions`, `accounting_lots`,
  `accounting_warnings`;
* from `0009_manual_adjustments`: `manual_adjustments`;
* from `0010_exchange_balances`: `exchange_balances`.

No other table refers to any of them, so nothing that stays is rebuilt. Children go before
their parents and the triggers before their table, so the order is right even with foreign
key enforcement on; `db/migration_guards.py` runs every migration with it off regardless, and
checks for dangling references before the commit.

**The upgrade deletes data, and the downgrade does not bring it back.** The downgrade runs the
`upgrade()` of `0006` to `0010` in order, so the schema below this revision is exactly what
those revisions built -- the drift and reflection tests of the older revisions keep holding --
but every table comes back empty. The deploy script backs up the database before it migrates
(`docs/deployment.md`), so the rows live on in that copy: rolling back past this revision means
restoring that backup, not running the previous image over this database.

The older revisions are kept unchanged. `alembic_version` on the Pi names `0011_extended_keys`
today, and a history that no longer contained a stamped revision could not be upgraded from.
"""

from __future__ import annotations

from typing import Final

from alembic import op

from portfolio.db.migrations.versions import (
    v0006_exchanges,
    v0007_exchange_sync,
    v0008_accounting,
    v0009_manual_adjustments,
    v0010_exchange_balances,
)

revision: str = "0012_drop_exchanges_accounting"
down_revision: str | None = "0011_extended_keys"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

DROPPED_TRIGGERS: Final = ("exchange_fills_no_update", "exchange_fills_no_delete")
"""The append-only triggers `0007_exchange_sync` put on `exchange_fills`."""

DROPPED_TABLES: Final = (
    "exchange_sync_run_accounts",
    "exchange_sync_runs",
    "exchange_sync_windows",
    "exchange_balances",
    "exchange_fills",
    "accounting_warnings",
    "accounting_lots",
    "accounting_positions",
    "accounting_snapshots",
    "manual_adjustments",
    "exchange_accounts",
)
"""Every table this revision drops, children before parents."""


def upgrade() -> None:
    """Drop the two triggers, then the eleven tables. Their indexes go with them."""
    for name in DROPPED_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for table in DROPPED_TABLES:
        op.drop_table(table)


def downgrade() -> None:
    """Recreate the dropped schema, empty, by running the revisions that built it."""
    for previous in (
        v0006_exchanges,
        v0007_exchange_sync,
        v0008_accounting,
        v0009_manual_adjustments,
        v0010_exchange_balances,
    ):
        previous.upgrade()
