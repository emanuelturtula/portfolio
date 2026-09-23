"""The mapped tables.

`prices.amount` is the first monetary column in this module, and it is `NumericText` and
never `sqlalchemy.Numeric`: `Numeric` round-trips every value through a C double on SQLite,
which is the one obvious type and the one that silently destroys precision. `decimals` on
`assets` is the exponent the base-unit integer columns of a later change will be
interpreted with, not an amount.

Columns are declared as `Text` rather than `String` so SQLite's own type name is what the
schema actually says, which keeps the reflected schema and the models comparable.
"""

from __future__ import annotations

# Real imports, not `TYPE_CHECKING` ones: SQLAlchemy evaluates `Mapped[datetime]` and
# `Mapped[Decimal]` at class-creation time to build the mapper.
# `tool.ruff.lint.flake8-type-checking` is configured with this Base so TC003 leaves them
# alone.
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from portfolio.db.base import Base
from portfolio.db.types import NumericText, UtcDateTime

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

# The fiat currencies a price may be quoted in, carrying exactly the same duplication
# hazard as the two constants above and covered by the same kind of reflection test. The
# values are `providers.prices.base.USD` and `EUR`; `db` may not import `providers`, which
# is why this is a literal rather than a derivation, and why a test is what holds the two
# together. Adding a currency is therefore a migration -- which is the honest cost, since
# an existing row would have no price in the new one.
_PRICE_QUOTE_CURRENCY_CHECK: Final = "quote_currency IN ('EUR', 'USD')"

PRICE_SCALE: Final = 12
"""Decimal places `prices.amount` rounds to and stores. Public, because a test pins it.

**One column has to hold a sub-cent asset and a five-figure one at the same time.**
Measured on 2026-09-23: KAS quoted near `0.042` and BTC near `86,000`. Twelve places keep a
KAS price exact well past the eight digits the vendor actually sends, and leave
`MONEY_PRECISION - 12` = 26 digits in front of the point, which is more than any fiat price
of a crypto asset will ever need.

`NumericText` takes no default scale on purpose -- a money column without a declared scale
has no defined rounding -- so this is a decision with a number behind it rather than a
value that got omitted.
"""


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


class AssetPrice(Base):
    """The current price of one asset in one fiat currency. One row per pair, four today.

    **Named `AssetPrice` rather than `Price` because `services.prices.Price` is a different
    thing and they meet in the same file often.** This is a row: it has an `asset_id`, a
    `fetched_at`, and no opinion about whether it is stale. The service's `Price` has an
    `asset_symbol` and a `stale` flag computed against a clock. Giving both the same name
    would make every import site decide which one it meant.

    **`UNIQUE (asset_id, quote_currency)` is what makes this the current price rather than
    a history**: the refresh upserts into the slot instead of appending. A time series is
    what a value-over-time chart needs and nothing asks for one yet; this column set does
    not foreclose it -- a history table adds `as_of` to the key and an index, and every
    column here is one it would want.

    **`as_of` is our clock, not the vendor's, and it is the instant the refresh began.**
    The name is misleading in two directions and both are written down here because nothing
    else in the schema can say it.

    It is not a vendor quote time: measured on 2026-09-23, none of Kraken's ticker,
    Coinbase's spot endpoint or the Kaspa price endpoint returns one. We know when we
    asked; we do not know how old the answer was. A vendor that does supply one can
    populate this field more honestly later without a migration, which is the reason it is
    a separate column from `fetched_at` rather than one column doing both jobs.

    Nor is it the instant this particular price was observed. `PriceRefreshService` reads
    its clock once, before the first request, and stamps every row of that refresh from it,
    so a row can be dated a few seconds before its answer arrived -- bounded by how long a
    refresh takes, against a staleness threshold of an hour. That buys one instant for the
    whole refresh instead of rows that disagree about when their own call happened, and it
    errs *early*, which is the only direction that cannot make a stale price look fresh.

    `fetched_at` is when this row was written, and today the two are the same instant --
    one clock read per refresh, stamped on every row it writes. They diverge the moment a
    vendor supplies a quote time, and staleness is computed from `as_of` because that is
    the one that describes the *price* rather than the write.

    **Money is never aggregated in SQL.** `SUM`, `ORDER BY` and `<` on this `TEXT` column
    coerce it to a float in SQLite, which is the whole reason it is `TEXT`. The valuation
    service loads rows and sums them in Python; `repositories/prices.py` orders by
    `asset_id`, an `INTEGER`.
    """

    __tablename__ = "prices"
    __table_args__ = (
        UniqueConstraint("asset_id", "quote_currency", name="uq_prices_asset_currency"),
        # Named, because a batch rebuild cannot re-create an anonymous CHECK.
        CheckConstraint(_PRICE_QUOTE_CURRENCY_CHECK, name="quote_currency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # No `ondelete`: nothing deletes an asset, and a cascade here would quietly discard a
    # price rather than refusing a delete that should not be happening in the first place.
    asset_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("assets.id"),
        nullable=False,
    )
    quote_currency: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(NumericText(PRICE_SCALE), nullable=False)
    # Which source actually answered, which is not the one that was asked first whenever
    # failover did anything. A price whose source cannot be audited is a number nobody can
    # check.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# Re-exported so that anything needing the schema -- Alembic's `env.py`, the drift check --
# imports it from the module that defines the tables. Importing `Base.metadata` directly
# from `base` would hand back an empty MetaData unless this module happened to have been
# imported first.
metadata: Final[MetaData] = Base.metadata
