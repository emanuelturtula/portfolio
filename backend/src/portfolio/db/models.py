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

# The chains a wallet may name, carrying exactly the same duplication hazard as
# `_ASSET_KIND_CHECK` above and covered by the same kind of reflection test. The values are
# the `ChainKey` members: `domain.chains` decides what a chain key is, and this constraint
# is the database refusing to hold anything else. Adding a chain is therefore a migration,
# not an enum edit -- which is the point.
_WALLET_CHAIN_KEY_CHECK: Final = "chain_key IN ('bitcoin', 'kaspa')"


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


class Wallet(Base):
    """One on-chain address the owner wants balances read from.

    **Two columns hold the address, and that is not redundancy.** `address_canonical` is
    what uniqueness is decided on and what a chain provider will eventually be asked
    about; `address_display` is the string the owner actually typed. For bech32 the two
    differ whenever a wallet renders an address in uppercase -- the encoding is
    case-insensitive, so both spellings are the same address. For Base58Check they are
    always identical, because that encoding is case *sensitive* and normalising it would
    produce a different address. One column plus a `lower()` at query time would therefore
    be correct for one of the two forms and silently wrong for the other.

    **Archiving is a timestamp, not a delete.** Balance snapshots will reference
    `wallet_id`, and a portfolio that forgets its own history the moment an address is
    retired is not a portfolio tracker. An archived row keeps its slot in the unique
    constraint, so re-adding the same address is a conflict rather than a resurrection --
    which is deliberate, since a silent resurrection would come back with the old label
    and the old history while looking to the owner like a new wallet.
    """

    __tablename__ = "wallets"
    __table_args__ = (
        # Named explicitly rather than left to the convention, which would render
        # `uq_wallets_user_id_chain_key_address_canonical` -- accurate, and long enough
        # that no error message quoting it is readable.
        UniqueConstraint(
            "user_id",
            "chain_key",
            "address_canonical",
            name="uq_wallets_user_chain_address",
        ),
        # Named, because a batch rebuild cannot re-create an anonymous CHECK.
        CheckConstraint(_WALLET_CHAIN_KEY_CHECK, name="chain_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chain_key: Mapped[str] = mapped_column(Text, nullable=False)
    address_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    address_display: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null means active. A timestamp rather than a boolean because "when was this
    # retired" is a question the history will be asked and a flag cannot answer.
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# Re-exported so that anything needing the schema -- Alembic's `env.py`, the drift check --
# imports it from the module that defines the tables. Importing `Base.metadata` directly
# from `base` would hand back an empty MetaData unless this module happened to have been
# imported first.
metadata: Final[MetaData] = Base.metadata
