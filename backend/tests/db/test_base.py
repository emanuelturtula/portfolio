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
}


def test_the_base_carries_the_convention() -> None:
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert metadata is Base.metadata


def test_the_convention_covers_every_constraint_kind() -> None:
    """A missing key means that kind of constraint is silently anonymous again."""
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}


def test_constraints_are_named_by_the_convention() -> None:
    """Every constraint and index on every mapped table has its conventional name."""
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
