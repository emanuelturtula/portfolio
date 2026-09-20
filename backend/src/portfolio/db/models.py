"""The mapped tables.

Nothing in this module reads or writes a monetary value, so no `Decimal`, no base-unit
integer column and no `NumericText` appears here yet -- money persistence is a later
change that lands on top of this schema. `decimals` on `assets` is the exponent those
later base-unit columns will be interpreted with, not an amount.

Columns are declared as `Text` rather than `String` so SQLite's own type name is what the
schema actually says, which keeps the reflected schema and the models comparable.
"""

from __future__ import annotations

# A real import, not a `TYPE_CHECKING` one: SQLAlchemy evaluates `Mapped[datetime]` at
# class-creation time to build the mapper. `tool.ruff.lint.flake8-type-checking` is
# configured with this Base so TC003 leaves it alone.
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from portfolio.db.base import Base
from portfolio.db.types import UtcDateTime

if TYPE_CHECKING:
    from sqlalchemy import MetaData

# The full set of values `assets.kind` may take. `fiat` has no seeded row yet -- the
# display currency is a later decision -- but the constraint admits it so that adding one
# is an insert rather than a table rebuild.
#
# This text is duplicated verbatim in `0001_initial_schema`, which is the copy the
# database is actually built from, and the drift check does NOT cover the duplication:
# Alembic's autogenerate compares tables, columns, types, server defaults, indexes,
# unique constraints, foreign keys and comments, and has no check-constraint comparator
# at all. Editing this string without writing the matching migration passes every gate in
# the repository and then rejects the insert in production. What covers it is a test that
# reflects `ck_assets_kind` back off a migrated database and compares its `sqltext`
# against this constant.
_ASSET_KIND_CHECK: Final = "kind IN ('crypto', 'fiat')"


class User(Base):
    """The single account that owns the portfolio.

    The product is deliberately single user. The table exists because a password hash and
    a session have to hang off something, and because the wallet registry is already
    specified with a `user_id` in its uniqueness constraint.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    # The Argon2id encoded hash, written by the authentication change. Never the password.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class Session(Base):
    """A logged-in browser session, identified by the hash of its cookie token.

    Only the hash is stored: a database file that leaks must not hand the reader a set of
    working session cookies.
    """

    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("token_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    # The sliding idle expiry is derived from this; `expires_at` is the hard ceiling that
    # activity cannot push back.
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class Asset(Base):
    """A tradeable or holdable asset: a coin, a token, or eventually a fiat currency."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("symbol"),
        # Named, because a batch rebuild cannot re-create an anonymous CHECK.
        CheckConstraint(_ASSET_KIND_CHECK, name="kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # The base-unit exponent: 8 means one BTC is 100_000_000 satoshis.
    decimals: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# Re-exported so that anything needing the schema -- Alembic's `env.py`, the drift check --
# imports it from the module that defines the tables. Importing `Base.metadata` directly
# from `base` would hand back an empty MetaData unless this module happened to have been
# imported first.
metadata: Final[MetaData] = Base.metadata
