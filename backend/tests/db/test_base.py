"""The metadata naming convention.

This is load-bearing rather than cosmetic. SQLite has no `ALTER COLUMN`, so Alembic
changes one by rebuilding the table in batch mode, and a constraint with no name cannot
be dropped or re-created during the rebuild. A convention that is declared but not
actually carried by `Base.metadata` would leave every future migration stuck.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, Table, UniqueConstraint

from portfolio.db.base import NAMING_CONVENTION, Base
from portfolio.db.models import metadata

# Every name the three tables in this change must carry, grouped by table.
EXPECTED_NAMES = {
    "users": {"pk_users", "uq_users_username"},
    "assets": {"pk_assets", "uq_assets_symbol", "ck_assets_kind"},
    "sessions": {
        "pk_sessions",
        "uq_sessions_token_hash",
        "fk_sessions_user_id_users",
        "ix_sessions_user_id",
    },
    # `uq_wallets_user_chain_address` is named explicitly on the model rather than left to
    # the convention, which would render `uq_wallets_user_id_chain_key_address_canonical`
    # -- accurate, and too long for any error message quoting it to be readable.
    "wallets": {
        "pk_wallets",
        "uq_wallets_user_chain_address",
        "ck_wallets_chain_key",
        "fk_wallets_user_id_users",
        "ix_wallets_user_id",
    },
    # #9. `uq_prices_asset_currency` is named explicitly for the same reason
    # `uq_wallets_user_chain_address` is: the convention would render
    # `uq_prices_asset_id_quote_currency`, and this is the constraint whose name appears in
    # the `IntegrityError` a duplicate refresh produces.
    #
    # **No index**, deliberately, and its absence is part of the pin. The table holds one
    # row per pair -- four today -- so an index would be cost with no benefit, and an index
    # over the money column would have SQLite coerce a `TEXT` amount to a double on every
    # write. A name appearing here later is a decision somebody has to make on purpose.
    "prices": {
        "pk_prices",
        "uq_prices_asset_currency",
        "ck_prices_quote_currency",
        "fk_prices_asset_id_assets",
    },
    # #10. Exactly two indexes across the three tables, which is what the spec's DDL
    # enumerates and what the absences below are pinning. Neither `sync_run_id` is indexed:
    # `uq_sync_run_chains_run_chain` already leads with that column, and nothing queries
    # snapshots by run -- the two reads are the primary key and `ix_..._wallet_observed`.
    "sync_runs": {
        "pk_sync_runs",
        "ck_sync_runs_trigger",
        "ck_sync_runs_status",
        "ix_sync_runs_started_at",
    },
    "sync_run_chains": {
        "pk_sync_run_chains",
        "uq_sync_run_chains_run_chain",
        "ck_sync_run_chains_chain_key",
        "ck_sync_run_chains_status",
        "ck_sync_run_chains_error_kind",
        "fk_sync_run_chains_sync_run_id_sync_runs",
    },
    # `uq_balance_snapshots_wallet_run` is named explicitly for the reason
    # `uq_wallets_user_chain_address` is: the convention would render
    # `uq_balance_snapshots_wallet_id_sync_run_id`, and this is the name that appears in the
    # `IntegrityError` a run writing one wallet twice produces.
    "balance_snapshots": {
        "pk_balance_snapshots",
        "uq_balance_snapshots_wallet_run",
        "ck_balance_snapshots_confirmed",
        "fk_balance_snapshots_wallet_id_wallets",
        "fk_balance_snapshots_sync_run_id_sync_runs",
        "ix_balance_snapshots_wallet_observed",
    },
}


def test_the_base_carries_the_convention() -> None:
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert metadata is Base.metadata


def test_the_convention_covers_every_constraint_kind() -> None:
    """A missing key means that kind of constraint is silently anonymous again."""
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}


def test_constraints_are_named_by_the_convention() -> None:
    """Every constraint and index on every mapped table has its conventional name.

    The table set is compared **exactly**. Iterating `EXPECTED_NAMES` alone -- which is
    what this did until #5 -- means a newly mapped table that nobody added here is checked
    by nothing at all, while the test goes on reporting success over the tables it does
    know about.
    """
    assert set(EXPECTED_NAMES) == set(metadata.tables)

    for table_name, expected in EXPECTED_NAMES.items():
        table = metadata.tables[table_name]
        found = {constraint.name for constraint in table.constraints}
        found |= {index.name for index in table.indexes}
        assert found == expected, table_name


def test_no_constraint_is_anonymous() -> None:
    """A `None` name is the failure this convention exists to prevent."""
    anonymous = [
        (table.name, type(constraint).__name__)
        for table in metadata.tables.values()
        for constraint in table.constraints
        if constraint.name is None
    ]

    assert anonymous == []


def test_the_convention_names_a_constraint_declared_without_one() -> None:
    """The convention has to apply to future tables, not only to the three here."""
    scratch = MetaData(naming_convention=NAMING_CONVENTION)
    table = Table(
        "a_future_table",
        scratch,
        Column("id", Integer, primary_key=True),
        Column("symbol", Integer),
        UniqueConstraint("symbol"),
    )

    assert {constraint.name for constraint in table.constraints} == {
        "pk_a_future_table",
        "uq_a_future_table_symbol",
    }
