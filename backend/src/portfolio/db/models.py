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

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from portfolio.db.base import Base
from portfolio.db.types import BaseUnits, NumericText, UtcDateTime

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

# How a balance sync run was started, and how it ended. Both carry the same duplication
# hazard as the constants above -- the text is repeated verbatim in `0005_balances` and
# nothing mechanical compares the two -- and both are covered the same way, by a test that
# reflects the constraint off a migrated database.
#
# `running` and `interrupted` are the two statuses the issue did not ask for and they are
# what make the table honest. A row is written at `running` *before* the first provider
# call, so a run the process died in the middle of leaves evidence; the lifespan sweeps any
# surviving `running` row to `interrupted` at startup and at shutdown. Without them a
# crashed run and a live run are the same row.
_SYNC_RUN_TRIGGER_CHECK: Final = "trigger IN ('scheduled', 'manual', 'startup')"
_SYNC_RUN_STATUS_CHECK: Final = (
    "status IN ('running', 'success', 'partial', 'failed', 'interrupted')"
)

# A chain's own outcome within a run: it either produced balances or it did not. There is
# no `partial` here, because partial is a property of the *run* -- one chain succeeding
# while another fails -- and a chain that raised produced nothing at all. #54 owns the
# per-address case that would make a chain itself partial.
_SYNC_RUN_CHAIN_STATUS_CHECK: Final = "status IN ('success', 'failed')"

# Whose fault a chain's failure was, and three different parties can be. The first four are
# `providers/errors.py`'s vocabulary and mean the vendor failed; `address_rejected` means the
# owner configured an address this chain will not accept -- most plausibly one from another
# network, which registration does not check; `internal` means we did, and it exists so that
# a parser bug is never reported as an outage at the chain. Nullable, because a chain that
# succeeded has no error to name.
_SYNC_RUN_CHAIN_ERROR_KIND_CHECK: Final = (
    "error_kind IS NULL OR "
    "error_kind IN ('unavailable', 'rate_limited', 'response', 'unknown_chain', "
    "'address_rejected', 'internal')"
)

# A confirmed balance is a count of base units the chain has already accepted, so it cannot
# be negative; `align_balances` refuses one at the provider boundary and this refuses one at
# the column, for the reason `PriceRepository.upsert` gives about checking twice.
#
# **`pending` deliberately has no such constraint.** It is a signed net mempool delta, and
# an outgoing payment waiting to confirm spends a confirmed output and funds nothing, so it
# is legitimately negative. A "balances cannot be negative" guard applied to it would refuse
# the ordinary case.
_BALANCE_SNAPSHOT_CONFIRMED_CHECK: Final = "confirmed >= 0"

# The venues an exchange account may name, carrying the same duplication hazard as the
# constants above -- the text is repeated verbatim in `0006_exchanges` and nothing mechanical
# compares the two -- and covered the same way, by a reflection test. The values are the
# `domain.exchanges.ExchangeKey` members, so adding a venue is a migration, as adding a chain
# is.
_EXCHANGE_ACCOUNT_EXCHANGE_KEY_CHECK: Final = "exchange_key IN ('bingx', 'bitget')"

# **The constraint that makes `uq_exchange_fills_account_trade` mean anything.** Two fills
# with an empty trade id collide under the unique constraint, and under #15's
# `ON CONFLICT DO NOTHING` the second is dropped without a word. `NormalizedFill` refuses an
# empty or blank id; this refuses one again for a writer that bypasses it. Whitespace is not
# refused here: `trim()` in a `CHECK` is a function call SQLite evaluates per row, and the
# empty string is the value a missing field actually defaults to.
_EXCHANGE_FILL_EXTERNAL_TRADE_ID_CHECK: Final = "external_trade_id <> ''"

# The `domain.exchanges.FillSide` members, with the same duplication and the same test.
_EXCHANGE_FILL_SIDE_CHECK: Final = "side IN ('buy', 'sell')"

# `Boolean` on SQLite is an `INTEGER`, and since SQLAlchemy 1.4 it no longer emits a `CHECK`
# of its own (`create_constraint` defaults to `False`), so without this the column would
# accept `2` from any writer that is not the ORM. Named, like every other constraint here.
_EXCHANGE_FILL_QUOTE_QUANTITY_DERIVED_CHECK: Final = "quote_quantity_derived IN (0, 1)"

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

FILL_SCALE: Final = 18
"""Decimal places every amount column of `exchange_fills` rounds to and stores.

**Eighteen covers every token denominated in wei**, the finest unit any venue this product
could plausibly import quotes in, and leaves `MONEY_PRECISION - 18` = 20 digits in front of
the point -- more than any quantity, price, quote amount or fee a spot fill will carry.

**It is also a refusal boundary, not only a rounding one.** `NumericText` rounds an amount
finer than its scale, which is right for a price and wrong for a fill whose `quote_quantity`
is stored "as reported": a value the column would change is not the value the venue
reported. So `providers.exchanges.base.NormalizedFill` refuses any amount with more than
eighteen fractional digits before it reaches the column, and a venue that reports one fails
its page loudly. That is a guess about fee precision recorded as one; if a venue ever does
it, the scale moves with the evidence.
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


class SyncRun(Base):
    """One attempt to read every active wallet's balance, whatever became of it.

    **The row is inserted before any provider is called, at `status='running'`**, and
    updated when the run ends. A row written only at the end is not written by a run the
    process died in the middle of, and "every run writes a row" is a criterion rather than
    an aspiration.

    **Timing is recorded twice and the two are not redundant.** `started_at` and
    `finished_at` are wall clock and answer *when*; `duration_ms` is an integer from
    `providers.http.monotonic_ms` and answers *how long*. The difference of two wall-clock
    reads is wrong by however much the clock was stepped between them, and a Raspberry Pi
    that syncs its clock mid-run would otherwise record a negative duration. It is an
    `INTEGER` of milliseconds rather than a fraction of seconds because `float` is banned in
    `services/`, which is where the subtraction happens.

    `finished_at` and `duration_ms` are both `NULL` while a run is in flight **and stay
    `NULL` for an interrupted one**: a run the process did not live to finish has no honest
    end time, and inventing the sweep's own clock reading would record a duration that is
    mostly the time the process spent dead.

    Counts are plain integers and are counts of *wallets*, not of addresses: a wallet is
    what the owner registered and what the dashboard renders.
    """

    __tablename__ = "sync_runs"
    __table_args__ = (
        # Named, because a batch rebuild cannot re-create an anonymous CHECK.
        CheckConstraint(_SYNC_RUN_TRIGGER_CHECK, name="trigger"),
        CheckConstraint(_SYNC_RUN_STATUS_CHECK, name="status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # `trigger` is a SQLite keyword; SQLAlchemy quotes it on the way out, and the CHECK
    # above parses with it unquoted, which was measured rather than assumed.
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Indexed because an operator reading the run history reads it by time. The index is
    # `ix_sync_runs_started_at` by the metadata naming convention.
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wallets_total: Mapped[int] = mapped_column(Integer, nullable=False)
    wallets_succeeded: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    wallets_failed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )


class SyncRunChain(Base):
    """What one chain did during one run: success, or a failure with whose fault it was.

    **One row per chain per run is what makes failure isolation observable.** The run's own
    `status` says `partial`; this table says which half was which and why, which is the
    difference between "the sync half worked" and something an operator can act on.

    `detail` is the provider error's message. Those providers are written never to quote a
    response body, a URL or an address into one -- `request_target` logs a label rather than
    a path for the same reason -- and this column inherits that discipline, because it is
    rendered by an endpoint and read in an operations view.

    `UNIQUE (sync_run_id, chain_key)` because a chain is attempted once per run: the
    addresses of one chain are one group and one coroutine, and a second row for the same
    pair would mean the grouping had broken.
    """

    __tablename__ = "sync_run_chains"
    __table_args__ = (
        UniqueConstraint("sync_run_id", "chain_key", name="uq_sync_run_chains_run_chain"),
        # The same text as `wallets.chain_key`'s constraint, and deliberately the same
        # constant rather than a second copy of it: two spellings of "which chains exist"
        # is how one of them comes to admit a chain the other does not.
        CheckConstraint(_WALLET_CHAIN_KEY_CHECK, name="chain_key"),
        CheckConstraint(_SYNC_RUN_CHAIN_STATUS_CHECK, name="status"),
        CheckConstraint(_SYNC_RUN_CHAIN_ERROR_KIND_CHECK, name="error_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # No index of its own: `uq_sync_run_chains_run_chain` leads with this column, so the
    # only query that filters on it -- `list_runs` fetching the chains of a page of runs --
    # is already served. A second index would be a second thing to keep.
    sync_run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sync_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    chain_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    wallets_read: Mapped[int] = mapped_column(Integer, nullable=False)
    error_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class BalanceSnapshot(Base):
    """What one wallet held the last time one run managed to read it.

    Append-only: a run adds a row, nothing updates one. That is what makes `MAX(id)` a
    correct answer to "the latest snapshot per wallet" -- identity order is insertion order
    -- and it needs no argument about how a `TEXT` datetime collates.

    **This table is the per-address cache #7 deferred.** A previous reading, with the
    instant it was taken, durable across restarts and visible to an operator. A second
    in-memory cache in front of it would be a copy of this table with a different lifetime
    and no way to look at it, and it would answer a repeated manual refresh by handing back
    a stale number that looks exactly like a fresh one. What the deferral was protecting
    against -- a refresh button that hammers a public index -- is answered by the sync
    coordinator instead: a second caller joins the run in flight rather than starting one.

    **`confirmed` and `pending` are integer base units, not `Decimal`.** A satoshi and a
    sompi cannot be subdivided and every chain API reports them as whole numbers, so there
    is nothing to round; `BaseUnits` refuses anything that is not an integer on the way in
    *and* on the way out, because SQLite has no column type enforcement and a row written
    by hand on the Pi would otherwise come back as a `float`.

    **`pending` keeps #7's tri-state exactly.** `NULL` means this chain cannot answer the
    question -- the Kaspa REST balance endpoint exposes no mempool figures at all -- and a
    value is a signed net delta that is legitimately negative. Zero is neither of those; it
    means the mempool holds nothing for this address, which only a chain that answers the
    question can say.

    **`decimals` is stored here rather than read from `assets` at query time**, so that
    editing an asset row cannot reinterpret history that was already recorded. The same
    reason `AddressBalance` carries it beside the provider's capabilities.

    `UNIQUE (wallet_id, sync_run_id)`: one run reads a wallet once. A second row for the
    pair would mean the same address was counted twice in one total.
    """

    __tablename__ = "balance_snapshots"
    __table_args__ = (
        UniqueConstraint("wallet_id", "sync_run_id", name="uq_balance_snapshots_wallet_run"),
        # Named, because a batch rebuild cannot re-create an anonymous CHECK.
        CheckConstraint(_BALANCE_SNAPSHOT_CONFIRMED_CHECK, name="confirmed"),
        # Named explicitly rather than left to the convention, which would render
        # `ix_balance_snapshots_wallet_id_observed_at`. This is the index the history
        # endpoint reads: one wallet, filtered and ordered by time.
        Index("ix_balance_snapshots_wallet_observed", "wallet_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("wallets.id", ondelete="CASCADE"),
        nullable=False,
    )
    # No index of its own, and unlike `sync_run_chains` no unique constraint leads with
    # it either. Nothing queries snapshots by run: the reads are "the latest per wallet" and
    # "one wallet's history", both of which the primary key and the index below serve. The
    # cascade on this foreign key would scan, and nothing deletes a run.
    sync_run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sync_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    confirmed: Mapped[int] = mapped_column(BaseUnits, nullable=False)
    pending: Mapped[int | None] = mapped_column(BaseUnits, nullable=True)
    decimals: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ExchangeAccount(Base):
    """One venue the owner imports spot fills from.

    **No column holds a credential, and none ever will.** The API key, its secret and the
    passphrase come from environment variables into `SecretStr` and are never persisted
    (rule 3); a database file that leaks must not hand the reader a working key. What this
    row records is that the owner *has* an account at a venue, so that the fills imported
    from it have something to belong to.

    `UNIQUE (user_id, exchange_key)`: credentials are one set per venue, read from the
    environment, so one account per venue is the only configuration that can exist.
    Relaxing it -- two sub-accounts at one venue -- needs a credential story first, and a
    migration then.

    Sync state -- status, `auth_failed`, checkpoints, the requested and effective start of
    the history -- is #15's and arrives with the loop that writes it.
    """

    __tablename__ = "exchange_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "exchange_key", name="uq_exchange_accounts_user_exchange"),
        # Named, because a batch rebuild cannot re-create an anonymous CHECK.
        CheckConstraint(_EXCHANGE_ACCOUNT_EXCHANGE_KEY_CHECK, name="exchange_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # No index of its own: `uq_exchange_accounts_user_exchange` leads with it, which serves
    # both the only lookup there is and the cascade.
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    exchange_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ExchangeFill(Base):
    """One spot trade execution, as the venue reported it. An immutable event log.

    **`UNIQUE (exchange_account_id, external_trade_id)` is criterion 7**, and it is what
    #15's `ON CONFLICT DO NOTHING` will stand on: the sync re-reads overlapping windows on
    purpose, and the database, not the loop, is what makes a fill counted once. Two
    consequences are written down here because nothing in the schema can say them:

    * **The trade id must be unique per account across every symbol.** A venue whose ids
      are unique only within a symbol must namespace them -- `BTC-USDT:12345` -- or this
      constraint turns two different fills into one and silently drops the second. #14
      must check its venue.
    * **An empty id would collide with every other empty id**, which is why
      `ck_exchange_fills_external_trade_id` exists beside the unique constraint.

    **Amounts are `NumericText(FILL_SCALE)` and carry no `CHECK`, deliberately.**
    `quantity > 0` on a `TEXT` column is a comparison SQLite performs by numeric affinity --
    the float coercion rule 2 forbids, applied inside the database. Signs, bounds and scale
    are enforced by `NormalizedFill`, in Python, where they are exact. `fee_amount` is
    signed: positive is a fee paid, negative a rebate.

    **`quote_quantity` is stored as reported**, never recomputed as `quantity * price`: a
    one-unit disagreement with the venue's own rounding would haunt every reconciliation
    after it. When a venue omits it, the provider derives it and sets
    `quote_quantity_derived`, which is what the flag is for.

    **`raw_payload` is the venue's own fill object**, as canonical JSON, kept for forensics
    -- never the envelope or the request, which is where a key or a signature could be.

    **`ON DELETE RESTRICT` on the account.** Fills are the history a cost basis is computed
    from; deleting an account must not take that history with it, so the database refuses
    the delete until someone has decided what happens to the fills.

    Two clocks, and they are not redundant: `executed_at` is the venue's, when the trade
    happened; `ingested_at` is ours, when this row was written.

    No index beyond the unique constraint, which leads with `exchange_account_id`. The
    reader that needs one arrives with #15 or later and adds it then, as
    `balance_snapshots` did.
    """

    __tablename__ = "exchange_fills"
    __table_args__ = (
        UniqueConstraint(
            "exchange_account_id",
            "external_trade_id",
            name="uq_exchange_fills_account_trade",
        ),
        # Named, because a batch rebuild cannot re-create an anonymous CHECK.
        CheckConstraint(_EXCHANGE_FILL_EXTERNAL_TRADE_ID_CHECK, name="external_trade_id"),
        CheckConstraint(_EXCHANGE_FILL_SIDE_CHECK, name="side"),
        CheckConstraint(
            _EXCHANGE_FILL_QUOTE_QUANTITY_DERIVED_CHECK,
            name="quote_quantity_derived",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange_account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("exchange_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_trade_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The venue's spelling, e.g. `BTCUSDT`; the assets are split out beside it rather than
    # parsed back out of it, because no venue promises a separator.
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    base_asset: Mapped[str] = mapped_column(Text, nullable=False)
    quote_asset: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(NumericText(FILL_SCALE), nullable=False)
    price: Mapped[Decimal] = mapped_column(NumericText(FILL_SCALE), nullable=False)
    quote_quantity: Mapped[Decimal] = mapped_column(NumericText(FILL_SCALE), nullable=False)
    quote_quantity_derived: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(NumericText(FILL_SCALE), nullable=False)
    # Null only when the fee is zero; `NormalizedFill` enforces the pairing.
    fee_asset: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# Re-exported so that anything needing the schema -- Alembic's `env.py`, the drift check --
# imports it from the module that defines the tables. Importing `Base.metadata` directly
# from `base` would hand back an empty MetaData unless this module happened to have been
# imported first.
metadata: Final[MetaData] = Base.metadata
