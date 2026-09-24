"""Criterion 1 of #9: a price is persisted with its source and its time, and loses no digit.

Every database here is a real file under `tmp_path`, built by running the **migrations**,
never by `metadata.create_all` -- the distinction `tests/db/test_wallets_repository.py`
explains at length: the migration is the only description of the schema production ever
executes.

## What makes this table different from every other one

It is the first that holds money. That puts three separate things under test, and only the
first of them is about the repository:

* **The round trip keeps the vendor's digits.** A price arrives as a `Decimal` built from
  the characters a vendor sent -- `decode_json` guarantees that much -- and everything
  after it has to be a pass-through. The assertions therefore compare a `Decimal` against a
  literal written out by hand, and separately compare the **stored `TEXT`** against the
  fixed-point string `NumericText` is supposed to have written. Comparing only the
  round-tripped value would pass for a column that stored `0.0` and read it back through a
  cache; comparing only the text would pass for a column nothing can read.
* **The scale that ships is 12, and it is pinned as the shipped value.** One column holds
  a KAS price near `0.042` and a BTC price near `86000` at the same time. A test that
  built its own `NumericText(12)` would be asserting against a number it chose; these read
  `PRICE_SCALE` and the column off the mapped class.
* **`UNIQUE (asset_id, quote_currency)` is what makes this the *current* price.** Without
  it a refresh appends and the table becomes an unindexed history whose "current" row is
  whichever one a query happened to return. Proven by raw SQL, for the reason the wallet
  suite gives: a repository that merely looked first would satisfy every assertion made
  through the repository and still lose the race between two writers.

**Nothing in this module sums, orders or compares money in SQL.** Rule 2's third clause is
not about `Decimal` -- it is about SQLite applying numeric affinity to a `TEXT` column the
moment it is asked to aggregate it. `list_for_currency` orders by `asset_id`, an INTEGER,
and this file asserts that the ordering is stable without ever asking the database about an
amount.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from portfolio.db.engine import create_session_factory
from portfolio.db.models import _PRICE_QUOTE_CURRENCY_CHECK, PRICE_SCALE, Asset, AssetPrice
from portfolio.db.types import NumericText
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.prices import PriceRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

AS_OF: Final = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
LATER: Final = AS_OF + timedelta(hours=1)

USD: Final = "USD"
EUR: Final = "EUR"

BTC: Final = "BTC"
KAS: Final = "KAS"

#: The two measured prices from the spec, as the **characters the vendors sent**.
#:
#: `KAS_USD` is the Kaspa node's `{"price": 0.04228645}` -- the JSON number that is this
#: issue's whole subject. `BTC_USD` carries trailing zeros on purpose: a value that went
#: through a `float` renders as `86000.1`, which is the same number and a different string,
#: and the string is what a `TEXT` column stores and what a diff of the database shows.
KAS_USD_DIGITS: Final = "0.04228645"
BTC_USD_DIGITS: Final = "86000.10000"

#: What `NumericText(12)` must write for each: the same digits, padded to exactly the
#: declared scale, in fixed point and never in scientific notation. Written out rather
#: than computed, because a computed expectation is a second copy of the code under test.
KAS_USD_STORED: Final = "0.042286450000"
BTC_USD_STORED: Final = "86000.100000000000"

#: A price finer than the column's scale that **survives** it: the thirteenth decimal place
#: is rounded away and something is left. This is what "rounded to the declared scale"
#: means, and it is the only over-precise case the column accepts.
OVER_PRECISE_DIGITS: Final = "0.0422864500004"
OVER_PRECISE_STORED: Final = "0.042286450000"

#: A price finer than the column's scale that **does not survive** it: rounding to twelve
#: places leaves zero.
#:
#: This constant used to sit above with `"0.000000000000"` beside it, asserted as a
#: rounding property -- which was this module asserting the one outcome it exists to
#: refuse. Measured end to end before the guard landed: a positive price stored as a zero,
#: and `value_portfolio` then reporting `total=0E-12 complete=True unpriced=()`. A renderer
#: is told the total is whole; there is no absent row for a valuation to notice.
VANISHING_DIGITS: Final = "0.0000000000005"

INSERT_PRICE: Final = text(
    "INSERT INTO prices (asset_id, quote_currency, amount, source, as_of, fetched_at) "
    "VALUES (:asset_id, :quote_currency, :amount, :source, "
    " '2026-09-23 12:00:00', '2026-09-23 12:00:00')"
)

KRAKEN: Final = "kraken"
COINBASE: Final = "coinbase"


@pytest.fixture
async def session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session from the application's own factory, over the migrated file.

    No user and no wallet is inserted: `prices` hangs off `assets`, which the seed
    migration has already filled with BTC, KAS and USDT. That is deliberate -- a price is
    a fact about an asset and not about an account, and a `user_id` on this table would be
    the first place a second owner's prices could diverge from the first's.
    """
    factory = create_session_factory(migrated_engine)
    async with factory() as opened:
        yield opened


@pytest.fixture
def repository(session: AsyncSession) -> PriceRepository:
    return PriceRepository(session)


@pytest.fixture
def assets(session: AsyncSession) -> AssetRepository:
    return AssetRepository(session)


async def asset_id(session: AsyncSession, symbol: str) -> int:
    """The seeded asset's primary key, read back rather than assumed to be 1, 2, 3."""
    found = (await session.scalars(select(Asset).where(Asset.symbol == symbol))).one()
    assert found.id is not None
    return found.id


async def stored_text(session: AsyncSession, *, symbol: str, currency: str) -> str:
    """The raw characters in the `amount` column, read around the type decorator.

    Through `text()` and not through the mapped attribute, because `NumericText` converts
    on the way out. What is being checked here is what is *on disk*, which is the only
    thing a future migration, a manual `sqlite3` session on the Pi, or a backup diff will
    ever see.
    """
    raw = await session.execute(
        text(
            "SELECT p.amount FROM prices p JOIN assets a ON a.id = p.asset_id "
            "WHERE a.symbol = :symbol AND p.quote_currency = :currency"
        ),
        {"symbol": symbol, "currency": currency},
    )
    value = raw.scalar_one()
    assert isinstance(value, str), f"the amount column read back as {type(value).__name__}"
    return value


# --------------------------------------------------------------------------------------
# Criterion 1: the source and the time are stored, and so is the amount, exactly
# --------------------------------------------------------------------------------------


async def test_a_price_round_trips_with_its_source_and_time(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """Criterion 1: every field survives a write, a commit and a read through a new identity.

    `expunge_all` before the read is what makes this a round trip rather than an identity
    map lookup. Without it the object compared is the object written, and a column that
    never reached SQLite at all would satisfy every assertion.

    The two timestamps are asserted separately and are deliberately different values.
    `as_of` is when the price was observed and `fetched_at` is when the row was written;
    a schema that collapsed them into one column, or a repository that passed the same
    argument twice, is invisible to an assertion that only checks one of them.
    """
    btc = await asset_id(session, BTC)

    await repository.upsert(
        asset_id=btc,
        quote_currency=USD,
        amount=Decimal(BTC_USD_DIGITS),
        source=KRAKEN,
        as_of=AS_OF,
        fetched_at=LATER,
    )
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(AssetPrice))).one()

    assert stored.asset_id == btc
    assert stored.quote_currency == USD
    assert stored.amount == Decimal(BTC_USD_DIGITS)
    assert stored.source == KRAKEN
    assert stored.as_of == AS_OF
    assert stored.fetched_at == LATER
    assert stored.as_of != stored.fetched_at


async def test_the_stored_timestamps_come_back_aware_and_in_utc(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """SQLite stores no offset, so `UtcDateTime` is the only thing that puts one back.

    Criterion 4 computes `now - as_of > STALE_AFTER`. Subtracting a naive datetime from an
    aware one raises `TypeError`, which would at least be loud -- but a *naive* pair
    compares fine and silently measures staleness against whatever timezone the Raspberry
    Pi happens to be in. This is the assertion that says the read path cannot produce one.
    """
    kas = await asset_id(session, KAS)
    await repository.upsert(
        asset_id=kas,
        quote_currency=USD,
        amount=Decimal(KAS_USD_DIGITS),
        source=KRAKEN,
        as_of=AS_OF,
        fetched_at=AS_OF,
    )
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(AssetPrice))).one()

    assert stored.as_of.tzinfo is not None
    assert stored.as_of.utcoffset() == timedelta(0)
    assert stored.fetched_at.utcoffset() == timedelta(0)


async def test_a_sub_cent_price_round_trips_without_losing_a_digit(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """The digits the vendor sent, through the column, back out, and on disk.

    This is the test the whole issue is about. `0.04228645` is what the Kaspa node sends as
    a JSON number; anything that touched a binary `double` on the way here produces
    `0.0422864500000000032020608387028914876282215118408203125`, and the assertion on the
    stored text is what sees it -- the value comparison alone would not, because a
    `Decimal` built from that float still *prints* as `0.04228645` at eight places.

    The expectations are literals. Neither is derived from `PRICE_SCALE`, from
    `quantize`, or from the value that was written, because an expectation computed the way
    the code computes it is not an expectation.
    """
    kas = await asset_id(session, KAS)

    await repository.upsert(
        asset_id=kas,
        quote_currency=USD,
        amount=Decimal(KAS_USD_DIGITS),
        source=KRAKEN,
        as_of=AS_OF,
        fetched_at=AS_OF,
    )
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(AssetPrice))).one()

    assert stored.amount == Decimal(KAS_USD_DIGITS)
    assert await stored_text(session, symbol=KAS, currency=USD) == KAS_USD_STORED
    # Every significant digit the vendor sent is still in the stored string, in order.
    assert KAS_USD_STORED.startswith(KAS_USD_DIGITS)


async def test_a_five_figure_price_and_a_sub_cent_one_share_the_column(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """The reason the scale is 12 rather than 2 or 20: both extremes, in one table.

    BTC near 86,000 and KAS near 0.042 were both measured on the same day. A scale that
    served one would ruin the other -- two decimal places truncates the KAS price to
    `0.04`, and a scale so large that 86,000 no longer fits in the digits before the point
    refuses the BTC row outright. Asserted together, in one commit, so a change to the
    scale cannot be checked against only the convenient half.
    """
    btc = await asset_id(session, BTC)
    kas = await asset_id(session, KAS)

    for identifier, digits in ((btc, BTC_USD_DIGITS), (kas, KAS_USD_DIGITS)):
        await repository.upsert(
            asset_id=identifier,
            quote_currency=USD,
            amount=Decimal(digits),
            source=KRAKEN,
            as_of=AS_OF,
            fetched_at=AS_OF,
        )
    await session.commit()
    session.expunge_all()

    by_symbol = {
        (await session.get_one(Asset, row.asset_id)).symbol: row
        for row in (await session.scalars(select(AssetPrice))).all()
    }

    assert by_symbol[BTC].amount == Decimal(BTC_USD_DIGITS)
    assert by_symbol[KAS].amount == Decimal(KAS_USD_DIGITS)
    assert await stored_text(session, symbol=BTC, currency=USD) == BTC_USD_STORED
    assert await stored_text(session, symbol=KAS, currency=USD) == KAS_USD_STORED


async def test_the_shipped_scale_is_twelve_and_the_column_is_the_one_that_uses_it() -> None:
    """The shipped value, pinned -- and pinned to the column rather than beside it.

    Two halves, and only together do they mean anything. `PRICE_SCALE == 12` alone is a
    test of a constant; a column built with a literal `12` beside a constant that says 12
    is two facts that can disagree. So the mapped column's own type is read and its
    `scale` compared to the constant, which is the thing production actually rounds by.

    #6's lesson, applied to a number rather than to a policy: a test that injects a value
    can no longer observe the value production uses.
    """
    assert PRICE_SCALE == 12

    column_type = AssetPrice.__table__.c.amount.type

    assert isinstance(column_type, NumericText)
    assert column_type.scale == PRICE_SCALE
    assert AssetPrice.__tablename__ == "prices"


async def test_a_price_finer_than_the_scale_is_rounded_to_it(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """The thirteenth decimal place, rounded away, with something left behind.

    Not a hazard for either asset this release prices -- KAS quotes at eight places -- but
    the column's behaviour at its own boundary should be a decision rather than a surprise.
    This is the accepted half of that boundary: `0.0422864500004` loses a four and stays a
    price.

    The refused half is the test below. Earlier this module asserted **that** case as a
    rounding property too, which was a mistake worth naming: it wrote down the column
    destroying an amount as the column's declared behaviour, and the assertion would have
    gone on passing through every mutation of the guard that now prevents it.
    """
    kas = await asset_id(session, KAS)

    await repository.upsert(
        asset_id=kas,
        quote_currency=USD,
        amount=Decimal(OVER_PRECISE_DIGITS),
        source=KRAKEN,
        as_of=AS_OF,
        fetched_at=AS_OF,
    )
    await session.commit()

    assert await stored_text(session, symbol=KAS, currency=USD) == OVER_PRECISE_STORED


async def test_a_price_that_would_round_away_to_zero_is_refused(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """A positive price the scale cannot represent is refused, and no row is written.

    This is criterion 3 arriving through the column instead of through an absent row, and
    it is the more dangerous of the two routes: a missing row produces `NEVER_FETCHED` and
    an incomplete total, which a renderer has to handle. A stored zero produces a
    **complete** total that is silently short, and nothing anywhere says so.

    The empty table is the half that matters. A refusal that still left the zero behind
    would be the original defect with a traceback attached.
    """
    kas = await asset_id(session, KAS)

    with pytest.raises(ValueError, match=r"finer than its scale") as caught:
        await repository.upsert(
            asset_id=kas,
            quote_currency=USD,
            amount=Decimal(VANISHING_DIGITS),
            source=KRAKEN,
            as_of=AS_OF,
            fetched_at=AS_OF,
        )

    # **A `ValueError`, not the `StatementError` SQLAlchemy wraps it in.** The column raises
    # at bind time, inside `flush()`, and the driver's wrapper would carry the `INSERT`
    # statement out through a repository into a service -- a `sqlalchemy` exception in a
    # layer `import-linter` forbids from importing `sqlalchemy`, which is the same breach
    # as a raw `httpx` error reaching a service, in the other direction.
    assert not isinstance(caught.value, StatementError)
    assert type(caught.value).__module__ == "builtins"
    # The wrapper is kept as the cause, so the traceback still shows where it happened.
    assert isinstance(caught.value.__cause__, StatementError)

    await session.rollback()
    assert (await session.scalars(select(AssetPrice))).all() == []


async def test_the_refusal_carries_no_price_and_no_row(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """The message names the scale and not the amount, through the statement wrapper too.

    SQLAlchemy renders bound parameters into a `StatementError` by default; `hide_parameters`
    is what stops it, and `tests/db/test_wallets_repository.py` proves that for an address.
    Here the value is a price, which is public -- but the same column will hold a quantity,
    and a quantity is the owner's holdings. Asserting it now is what keeps the habit rather
    than discovering the exception later.
    """
    kas = await asset_id(session, KAS)

    with pytest.raises(ValueError, match=r"finer than its scale") as caught:
        await repository.upsert(
            asset_id=kas,
            quote_currency=USD,
            amount=Decimal(VANISHING_DIGITS),
            source=KRAKEN,
            as_of=AS_OF,
            fetched_at=AS_OF,
        )

    rendered = f"{caught.value}{caught.value!r}{caught.value.__cause__}"

    assert VANISHING_DIGITS not in rendered
    assert str(PRICE_SCALE) in str(caught.value)
    await session.rollback()


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        pytest.param(Decimal(VANISHING_DIGITS), ValueError, id="an amount that rounds away"),
        pytest.param(Decimal("1E+300"), ValueError, id="an amount too large for the scale"),
        pytest.param(0.04228645, TypeError, id="a float"),
        pytest.param(True, TypeError, id="a bool"),
    ],
)
async def test_a_column_types_refusal_is_never_delivered_as_a_driver_exception(
    repository: PriceRepository,
    session: AsyncSession,
    amount: object,
    expected: type[Exception],
) -> None:
    """Every value the column refuses reaches a caller as the column's own exception.

    This is a layering assertion rather than a value one, and it is the generalisation of
    the two tests above. `NumericText` refuses at **bind** time, which is inside
    `flush()`, and SQLAlchemy wraps whatever the type raised in a `StatementError` that
    carries the `INSERT` with it. Letting that travel puts a `sqlalchemy` exception in
    `services/`, which the layering contract forbids from importing `sqlalchemy` at all --
    a caller cannot even name it in an `except` clause.

    `1E+300` is the row this test was written for: it took down a whole refresh as an
    uncaught `StatementError`, leaving zero rows written for three pairs that had answered
    perfectly well.

    The type is asserted rather than only the absence of the wrapper, because `TypeError`
    and `ValueError` mean different things to a caller -- one is a programming mistake and
    the other is a value this column cannot hold -- and collapsing them would be the same
    loss of information in a smaller package.

    **The rule is narrower than "no `sqlalchemy` type escapes", deliberately**, and the
    name of this test says the narrow form. A genuine database failure -- a dropped
    connection, a `CHECK` violation -- has no better account available at this layer, and
    inventing one would be the wrong kind of helpful.
    `test_a_real_database_failure_is_left_as_itself` is the other half, and without it a
    clause that unwrapped *everything* would satisfy this test while delivering a lost
    connection under the column's vocabulary.
    """
    kas = await asset_id(session, KAS)

    with pytest.raises(expected) as caught:
        await repository.upsert(
            asset_id=kas,
            quote_currency=USD,
            amount=amount,  # type: ignore[arg-type]
            source=KRAKEN,
            as_of=AS_OF,
            fetched_at=AS_OF,
        )

    assert not isinstance(caught.value, StatementError)
    assert type(caught.value).__module__ == "builtins", (
        f"{type(caught.value).__module__}.{type(caught.value).__name__} escaped the repository"
    )
    await session.rollback()


async def test_a_real_database_failure_is_left_as_itself(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """The discriminator for the unwrap: a `CHECK` violation is still an `IntegrityError`.

    Without this, a `_flush` that unwrapped **every** `StatementError` would satisfy the
    test above -- and a dropped connection, or a constraint refusing a row, would arrive
    at a caller as whatever the driver happened to have underneath, dressed in the
    vocabulary the column type uses for a bad value. The pair together says the actual
    rule: *a column type's refusal of a value is translated; a database failure is not.*

    `GBP` is the cause because it is the one refusal this schema can produce on demand --
    `ck_prices_quote_currency` admits two currencies -- and because it is genuinely a
    database's verdict rather than a type's. An operator seeing `IntegrityError` here is
    being told something true: the row was refused by the database, and the fix is a
    migration.
    """
    btc = await asset_id(session, BTC)

    with pytest.raises(IntegrityError) as caught:
        await repository.upsert(
            asset_id=btc,
            quote_currency="GBP",
            amount=Decimal("1"),
            source=KRAKEN,
            as_of=AS_OF,
            fetched_at=AS_OF,
        )

    assert "ck_prices_quote_currency" in str(caught.value)
    assert isinstance(caught.value, StatementError), (
        "an IntegrityError is a StatementError; it must reach the caller as the driver's"
    )
    await session.rollback()


async def test_a_float_amount_never_reaches_the_column(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """The backstop the float ban cannot provide, at the boundary that can.

    `tests/security/test_no_float.py` reads source and stops a float being *written* in
    `providers/`, `services/` or `domain/`. It cannot see a float produced at run time from
    names, and a vendor's JSON number is exactly that. `NumericText` refusing one on the way
    in is the second line, and this is where it is exercised for the price column
    specifically -- with a value that is entirely plausible, which is what makes it worth an
    assertion.
    """
    kas = await asset_id(session, KAS)

    # One statement inside the block, deliberately. `NumericText` refuses the value when it
    # binds the parameter, so a `flush()` in here would be unreachable -- and a `raises`
    # block holding two statements cannot say which of them raised.
    with pytest.raises(TypeError, match=r"(?i)decimal"):
        await repository.upsert(
            asset_id=kas,
            quote_currency=USD,
            amount=0.04228645,  # type: ignore[arg-type]
            source=KRAKEN,
            as_of=AS_OF,
            fetched_at=AS_OF,
        )


# --------------------------------------------------------------------------------------
# Criterion 1: the upsert replaces, so the table is a current price and not a history
# --------------------------------------------------------------------------------------


async def test_refreshing_a_pair_replaces_its_row_rather_than_adding_one(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """The second refresh of a pair overwrites the first. One row, new values.

    A repository that inserted would leave the table growing by four rows an hour, with
    `get` returning whichever row SQLite felt like -- most likely the oldest, since nothing
    orders by time. The failure mode is a dashboard showing last Tuesday's price with no
    indication that anything is wrong, which is criterion 3's complaint in a different
    costume.

    Every field is asserted after the replacement, not only the amount: a source that stayed
    behind would credit Kraken with a number Coinbase supplied.
    """
    btc = await asset_id(session, BTC)
    await repository.upsert(
        asset_id=btc,
        quote_currency=USD,
        amount=Decimal(BTC_USD_DIGITS),
        source=KRAKEN,
        as_of=AS_OF,
        fetched_at=AS_OF,
    )
    await session.commit()

    await repository.upsert(
        asset_id=btc,
        quote_currency=USD,
        amount=Decimal("85999.90000"),
        source=COINBASE,
        as_of=LATER,
        fetched_at=LATER,
    )
    await session.commit()
    session.expunge_all()

    rows = (await session.scalars(select(AssetPrice))).all()

    assert len(rows) == 1
    assert rows[0].amount == Decimal("85999.90000")
    assert rows[0].source == COINBASE
    assert rows[0].as_of == LATER
    assert rows[0].fetched_at == LATER


async def test_the_two_currencies_of_one_asset_are_two_rows(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """Criterion 8's shape in the schema: USD and EUR coexist and never overwrite.

    The unique constraint spans two columns and this is the test that says the second one
    is load-bearing. A constraint on `asset_id` alone would make every EUR refresh silently
    replace the USD price, and a portfolio valued in dollars would start reporting euros.
    """
    btc = await asset_id(session, BTC)
    for currency, digits in ((USD, BTC_USD_DIGITS), (EUR, "79000.34000")):
        await repository.upsert(
            asset_id=btc,
            quote_currency=currency,
            amount=Decimal(digits),
            source=KRAKEN,
            as_of=AS_OF,
            fetched_at=AS_OF,
        )
    await session.commit()
    session.expunge_all()

    rows = {row.quote_currency: row.amount for row in (await session.scalars(select(AssetPrice)))}

    assert rows == {USD: Decimal(BTC_USD_DIGITS), EUR: Decimal("79000.34000")}


async def test_get_returns_the_pairs_row_and_none_for_a_pair_never_fetched(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """`None` rather than a zero row, which is criterion 3 starting at the bottom layer.

    A repository that answered a missing pair with a zero-amount row would make
    `NEVER_FETCHED` unreachable and hand the service a price it could add to a total.
    """
    btc = await asset_id(session, BTC)
    kas = await asset_id(session, KAS)
    await repository.upsert(
        asset_id=btc,
        quote_currency=USD,
        amount=Decimal(BTC_USD_DIGITS),
        source=KRAKEN,
        as_of=AS_OF,
        fetched_at=AS_OF,
    )
    await session.commit()

    found = await repository.get(asset_id=btc, quote_currency=USD)
    missing_currency = await repository.get(asset_id=btc, quote_currency=EUR)
    missing_asset = await repository.get(asset_id=kas, quote_currency=USD)

    assert found is not None
    assert found.amount == Decimal(BTC_USD_DIGITS)
    assert missing_currency is None
    assert missing_asset is None


async def test_listing_a_currency_returns_only_that_currency_in_asset_id_order(
    repository: PriceRepository,
    session: AsyncSession,
) -> None:
    """The read the valuation service makes -- and it is ordered by an INTEGER.

    Rule 2's third clause: `ORDER BY` on a `TEXT` money column applies SQLite's numeric
    affinity and sorts by a `double`. `asset_id` is an `INTEGER` primary key, so ordering by
    it costs nothing and touches no money. The assertion is on the id order rather than on
    the amounts, which is what keeps it honest if the seed order ever changes.
    """
    btc = await asset_id(session, BTC)
    kas = await asset_id(session, KAS)
    for identifier, currency, digits in (
        (kas, USD, KAS_USD_DIGITS),
        (btc, USD, BTC_USD_DIGITS),
        (btc, EUR, "79000.34000"),
    ):
        await repository.upsert(
            asset_id=identifier,
            quote_currency=currency,
            amount=Decimal(digits),
            source=KRAKEN,
            as_of=AS_OF,
            fetched_at=AS_OF,
        )
    await session.commit()
    session.expunge_all()

    in_usd = await repository.list_for_currency(USD)
    everything = await repository.list_all()

    assert [row.quote_currency for row in in_usd] == [USD, USD]
    assert [row.asset_id for row in in_usd] == sorted([btc, kas])
    assert len(everything) == 3


# --------------------------------------------------------------------------------------
# The assets repository, which is where `asset_id` comes from
# --------------------------------------------------------------------------------------


async def test_the_asset_lookup_answers_by_symbol_and_says_nothing_for_an_unknown_one(
    assets: AssetRepository,
    session: AsyncSession,
) -> None:
    """A symbol nobody seeded is `None`, not a `KeyError` and not an invented row.

    The refresh service turns `("BTC", "USD")` into an `asset_id`, and an unknown symbol
    has to reach it as an absence it can report rather than as an exception that ends the
    whole refresh -- one unpriceable asset must not cost the other three their prices.
    """
    del session

    by_symbol = await assets.by_symbol()
    btc = await assets.get_by_symbol(BTC)
    unknown = await assets.get_by_symbol("NOTACOIN")

    assert set(by_symbol) == {"BTC", "KAS", "USDT"}
    assert btc is not None
    assert by_symbol[BTC].id == btc.id
    assert unknown is None


# --------------------------------------------------------------------------------------
# What the migration actually wrote, read back off disk
# --------------------------------------------------------------------------------------


async def test_the_unique_constraint_refuses_a_second_row_for_one_pair(
    migrated_engine: AsyncEngine,
) -> None:
    """Proven by SQLite and by nothing else: two raw inserts, no ORM in between.

    Whatever the repository does or stops doing, the second one has to fail here. If
    `UNIQUE` is missing from the migration, this is the test that goes red while
    `upsert` still appears to work -- because it would be doing its own `SELECT` first,
    which loses the race that two concurrent refreshes would create.
    """
    async with migrated_engine.begin() as connection:
        identifier = await connection.scalar(text("SELECT id FROM assets WHERE symbol = 'BTC'"))
        row = {
            "asset_id": identifier,
            "quote_currency": USD,
            "amount": BTC_USD_STORED,
            "source": KRAKEN,
        }
        await connection.execute(INSERT_PRICE, row)

    with pytest.raises(IntegrityError) as caught:
        async with migrated_engine.begin() as connection:
            # A different amount and a different source: the constraint is on the two
            # columns it names, not on the whole row.
            await connection.execute(
                INSERT_PRICE, {**row, "amount": "1.000000000000", "source": COINBASE}
            )

    assert "UNIQUE" in str(caught.value).upper()

    async with migrated_engine.connect() as connection:
        remaining = await connection.scalar(text("SELECT COUNT(*) FROM prices"))
    assert remaining == 1, "the refused insert must not have landed"


async def test_the_quote_currency_check_admits_usd_and_eur_and_nothing_else(
    migrated_engine: AsyncEngine,
) -> None:
    """Criterion 8 in the database: two currencies, both fetched, neither derived.

    Asserted by *inserting* each value rather than by reading the constraint text, so it is
    SQLite's own opinion that is recorded. `GBP` is the rejection: adding a currency is a
    migration, which is the honest cost and the reason the constraint exists at all.
    """
    async with migrated_engine.begin() as connection:
        btc = await connection.scalar(text("SELECT id FROM assets WHERE symbol = 'BTC'"))
        for currency in (USD, EUR):
            await connection.execute(
                INSERT_PRICE,
                {
                    "asset_id": btc,
                    "quote_currency": currency,
                    "amount": "1.000000000000",
                    "source": KRAKEN,
                },
            )
        accepted = await connection.scalar(text("SELECT COUNT(*) FROM prices"))

    assert accepted == 2

    async with migrated_engine.connect() as connection:
        kas = await connection.scalar(text("SELECT id FROM assets WHERE symbol = 'KAS'"))

    # The id is read outside the block, so the only statement inside it is the insert --
    # which is the one that has to raise. With the lookup in there, a `KAS` row that had
    # somehow gone missing would raise for an unrelated reason and this would still pass.
    with pytest.raises(IntegrityError) as caught:
        async with migrated_engine.begin() as connection:
            await connection.execute(
                INSERT_PRICE,
                {
                    "asset_id": kas,
                    "quote_currency": "GBP",
                    "amount": "1.000000000000",
                    "source": KRAKEN,
                },
            )

    assert "ck_prices_quote_currency" in str(caught.value)


def test_the_quote_currency_check_constraint_matches_the_model(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """Alembic has no check-constraint comparator, so this is the only thing looking.

    The same hazard `_ASSET_KIND_CHECK` and `_WALLET_CHAIN_KEY_CHECK` already carry, and
    the same cure. Editing `_PRICE_QUOTE_CURRENCY_CHECK` without writing the matching
    migration passes ruff, mypy, the layering contract, the drift check and every other
    test in the repository, and then rejects inserts in production.
    """
    del migrated_database_url  # Ordering only: the fixture migrates the file.

    reflected = {
        str(constraint["name"]): str(constraint["sqltext"])
        for constraint in inspect(sync_engine).get_check_constraints("prices")
    }

    assert set(reflected) == {"ck_prices_quote_currency"}
    assert " ".join(reflected["ck_prices_quote_currency"].split()) == " ".join(
        _PRICE_QUOTE_CURRENCY_CHECK.split()
    )


def test_the_amount_column_is_text_on_disk(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """Rule 2's storage row, reflected through a second, unconfigured connection.

    `NumericText` is a `TypeDecorator` over `Text`, so what reaches SQLite is a `TEXT`
    column. If it were ever swapped for `Numeric`, `DECIMAL`, `Float` or `REAL`, the
    reflected type here would change and every value in the column would start round
    tripping through a C double -- silently, and with the ORM still returning `Decimal`
    objects that look right.

    `tests/security/test_no_float.py` bans those four names in source. This is the same
    fact asserted about the file the Raspberry Pi opens.
    """
    del migrated_database_url

    columns = {
        str(column["name"]): str(column["type"])
        for column in inspect(sync_engine).get_columns("prices")
    }

    assert columns["amount"] == "TEXT"
    assert columns["quote_currency"] == "TEXT"
    assert columns["source"] == "TEXT"
    assert set(columns) == {
        "id",
        "asset_id",
        "quote_currency",
        "amount",
        "source",
        "as_of",
        "fetched_at",
    }


def test_the_unique_constraint_is_in_the_migrated_schema(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """Reflected off disk, by name and by column list, in that order.

    The raw-insert test proves SQLite refuses a duplicate. This proves *which* constraint
    refused it, which is what keeps the failure legible if a column is ever added to the
    list or dropped from it.
    """
    del migrated_database_url

    unique = {
        str(constraint["name"]): list(constraint["column_names"])
        for constraint in inspect(sync_engine).get_unique_constraints("prices")
    }

    assert unique == {"uq_prices_asset_currency": ["asset_id", "quote_currency"]}


def test_the_foreign_key_points_at_assets(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """A price is about an asset, and a price for an asset that does not exist is nothing.

    Asserted as the whole foreign key rather than as its presence: a key pointing at
    `wallets` would still be one foreign key.
    """
    del migrated_database_url

    foreign_keys = inspect(sync_engine).get_foreign_keys("prices")

    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "assets"
    assert foreign_keys[0]["constrained_columns"] == ["asset_id"]


def test_the_table_carries_no_index_beyond_its_unique_constraint(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """Four rows today, so an index would be cost with no benefit -- and it is a decision.

    Pinned because the decision is invisible otherwise. An index nobody argued for is one
    the next person adds "to be safe", and on a money column it would be an index SQLite
    maintains by coercing `TEXT` to a `double` on every write.
    """
    del migrated_database_url

    indexes = {index["name"] for index in inspect(sync_engine).get_indexes("prices")}

    assert not any(
        "amount" in (index["column_names"] or [])
        for index in inspect(sync_engine).get_indexes("prices")
    ), "an index over the money column coerces it to a float on every write"
    assert indexes <= {"uq_prices_asset_currency", "sqlite_autoindex_prices_1"}
