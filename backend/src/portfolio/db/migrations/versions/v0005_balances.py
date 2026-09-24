"""Balance sync runs, their per-chain outcomes, and the snapshot history.

Revision ID: 0005_balances
Revises: 0004_prices
Create Date: 2026-09-23

Three tables and no alteration of an existing one, which is what makes the downgrade
uncomplicated: it drops what this migration created, in dependency order, and there is
nothing to back-fill and nothing else to lose.

**Every `CHECK` text below is duplicated verbatim from `db/models.py` and nothing mechanical
compares the two.** Alembic's autogenerate has no check-constraint comparator at all, so
editing one copy without the other passes ruff, mypy, the layering contract and the drift
check, and then rejects inserts in production. What covers it is a test that reflects each
constraint off a migrated database and compares its `sqltext` against the model's constant --
the same treatment `ck_assets_kind`, `ck_wallets_chain_key` and `ck_prices_quote_currency`
already have.

**`confirmed` and `pending` are `BaseUnits`, not `sa.Numeric` and not `NumericText`.** An
on-chain quantity is an integer count of indivisible units -- satoshis, sompi -- so there is
nothing to round and no reason to store text; `BaseUnits` is `BigInteger` with a refusal on
either side of the wire for anything that is not a whole number. `decimals` beside them is
the exponent they are read with, copied onto the row rather than looked up in `assets`, so
that editing an asset cannot reinterpret a reading that was already taken.

**`trigger` is a SQLite keyword.** SQLAlchemy quotes the identifier in the DDL it emits, and
SQLite's parser accepts the bare word inside the `CHECK` expression -- measured, not assumed,
because a constraint that fails to parse would take the whole migration with it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from portfolio.db.types import BaseUnits, UtcDateTime

revision: str = "0005_balances"
down_revision: str | None = "0004_prices"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the run log, the per-chain outcomes, and the snapshot history."""
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", UtcDateTime(), nullable=False),
        # Null while a run is in flight, and null for a run that was interrupted: a run the
        # process did not live to finish has no honest end time, and the sweep's own clock
        # reading would mostly measure how long the process was dead.
        sa.Column("finished_at", UtcDateTime(), nullable=True),
        # Milliseconds from a monotonic clock, so a wall-clock step mid-run cannot produce a
        # negative duration. An integer, because `float` is banned in the layer that
        # computes it.
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("wallets_total", sa.Integer(), nullable=False),
        sa.Column("wallets_succeeded", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("wallets_failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'manual', 'startup')",
            name=op.f("ck_sync_runs_trigger"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed', 'interrupted')",
            name=op.f("ck_sync_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(op.f("ix_sync_runs_started_at"), "sync_runs", ["started_at"], unique=False)

    op.create_table(
        "sync_run_chains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=False),
        sa.Column("chain_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("wallets_read", sa.Integer(), nullable=False),
        sa.Column("error_kind", sa.Text(), nullable=True),
        # The provider's own message. Those providers never quote a body, a URL or an
        # address into one, and this column is rendered by an endpoint, so it inherits that
        # discipline rather than relying on it.
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "chain_key IN ('bitcoin', 'kaspa')",
            name=op.f("ck_sync_run_chains_chain_key"),
        ),
        sa.CheckConstraint(
            "status IN ('success', 'failed')",
            name=op.f("ck_sync_run_chains_status"),
        ),
        sa.CheckConstraint(
            "error_kind IS NULL OR "
            "error_kind IN ('unavailable', 'rate_limited', 'response', 'unknown_chain', "
            "'internal')",
            name=op.f("ck_sync_run_chains_error_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_runs.id"],
            name=op.f("fk_sync_run_chains_sync_run_id_sync_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_run_chains")),
        sa.UniqueConstraint(
            "sync_run_id",
            "chain_key",
            name=op.f("uq_sync_run_chains_run_chain"),
        ),
    )
    op.create_index(
        op.f("ix_sync_run_chains_sync_run_id"),
        "sync_run_chains",
        ["sync_run_id"],
        unique=False,
    )

    op.create_table(
        "balance_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wallet_id", sa.Integer(), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), nullable=False),
        sa.Column("confirmed", BaseUnits(), nullable=False),
        # Signed, and null means "this chain cannot answer the question" rather than zero.
        # No non-negative CHECK, deliberately: an outgoing payment in the mempool is a
        # negative delta and is the ordinary case.
        sa.Column("pending", BaseUnits(), nullable=True),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("observed_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint("confirmed >= 0", name=op.f("ck_balance_snapshots_confirmed")),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_runs.id"],
            name=op.f("fk_balance_snapshots_sync_run_id_sync_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallets.id"],
            name=op.f("fk_balance_snapshots_wallet_id_wallets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balance_snapshots")),
        sa.UniqueConstraint(
            "wallet_id",
            "sync_run_id",
            name=op.f("uq_balance_snapshots_wallet_run"),
        ),
    )
    op.create_index(
        op.f("ix_balance_snapshots_sync_run_id"),
        "balance_snapshots",
        ["sync_run_id"],
        unique=False,
    )
    # Named rather than left to the convention, which would render
    # `ix_balance_snapshots_wallet_id_observed_at`. This is what the history endpoint reads:
    # one wallet, filtered and ordered by time.
    op.create_index(
        "ix_balance_snapshots_wallet_observed",
        "balance_snapshots",
        ["wallet_id", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the three tables, children first.

    `balance_snapshots` references both of the others and `sync_run_chains` references
    `sync_runs`, so the order is forced. Reversible without a data decision only in the
    sense that it loses exactly what it created: the snapshot history is **not**
    re-fetchable the way `prices` was -- a balance as it stood last Tuesday is gone once
    this runs. Said out loud here because `0004_prices` could honestly claim the opposite
    and a reader moving between the two should not carry that reassurance across.
    """
    op.drop_index("ix_balance_snapshots_wallet_observed", table_name="balance_snapshots")
    op.drop_index(op.f("ix_balance_snapshots_sync_run_id"), table_name="balance_snapshots")
    op.drop_table("balance_snapshots")
    op.drop_index(op.f("ix_sync_run_chains_sync_run_id"), table_name="sync_run_chains")
    op.drop_table("sync_run_chains")
    op.drop_index(op.f("ix_sync_runs_started_at"), table_name="sync_runs")
    op.drop_table("sync_runs")
