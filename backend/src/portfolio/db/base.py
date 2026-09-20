"""The declarative base and the metadata naming convention every table inherits.

The naming convention is load-bearing rather than cosmetic. SQLite cannot `ALTER COLUMN`,
so Alembic changes a column by rebuilding the table in batch mode: it copies the old table
into a new one and re-creates every constraint and index on the way. A constraint that has
no name cannot be referred to, dropped or re-created, which makes the rebuild impossible.
Fixing the names here, once, means every future migration has something to hold on to.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# `column_0_N_name` covers multi-column indexes and unique constraints; the single-column
# case renders identically to `column_0_name`, so one entry serves both.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The declarative base for every mapped class in the application."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
