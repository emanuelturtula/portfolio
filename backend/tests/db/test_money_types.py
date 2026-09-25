"""`NumericText` and `BaseUnits`: criteria 1, 2 and 3.

The bind and result processors are exercised directly, because that is where the rejections
live and a `TypeError` wrapped in a `StatementError` says less than the exception it
wrapped. They are then exercised through a **real column on a real file**, because a type
that is correct in isolation and not actually attached to anything proves nothing -- and
because only a real column can answer the question SQLite alone can answer: whether the
bytes on disk are `text` or a `real`.

The table here is deliberately built on its own `MetaData`. Attaching it to
`portfolio.db.base.Base` would put it in `portfolio.db.models.metadata`, and the drift
check from #1 would then report a table the migrations do not create -- which would read as
a schema bug rather than as the test-isolation bug it is. `test_the_money_table_is_not_in_
the_application_metadata` pins that mechanically rather than by comment.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Text, select, text
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.exc import StatementError
from sqlalchemy.types import TypeDecorator

from portfolio.db import models
from portfolio.db.types import BaseUnits, NumericText
from portfolio.domain.money import MONEY_PRECISION

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

DIALECT: Final = sqlite_dialect()

# The value criterion 1 names, and a column whose scale matches its 18 decimal places. The
# scale is part of a money column's meaning, exactly as it is in `DECIMAL(p, s)`, so an
# exact `str()` round trip is a claim about a matched pair -- not about the type alone.
HIGH_PRECISION = Decimal("0.000000012345678901")
HIGH_PRECISION_PLACES: Final = 18

SCALE_18: Final = NumericText(HIGH_PRECISION_PLACES)
SCALE_8: Final = NumericText(8)
SCALE_2: Final = NumericText(2)
SCALE_0: Final = NumericText(0)
UNITS: Final = BaseUnits()

INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1

# Both comfortably inside a signed 64-bit integer, which is the claim the docstring on
# `BaseUnits` makes and this module checks rather than repeats.
BITCOIN_SUPPLY_IN_SATOSHIS: Final = 2_100_000_000_000_000
KASPA_SUPPLY_IN_SOMPI: Final = 2_870_000_000_000_000_000

MONEY_METADATA = MetaData()
"""A metadata of its own. See the module docstring: this must never be `Base.metadata`."""

amounts = Table(
    "amounts",
    MONEY_METADATA,
    Column("id", Integer, primary_key=True),
    Column("wide", NumericText(HIGH_PRECISION_PLACES)),
    Column("fiat", NumericText(2)),
    Column("units", BaseUnits),
)


@pytest.fixture
async def money_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """The application's engine over a real file, with the throwaway table created.

    `engine` comes from `tests/db/conftest.py`: a file under `tmp_path`, never `:memory:`,
    for the reasons that fixture's docstring gives.
    """
    async with engine.begin() as connection:
        await connection.run_sync(MONEY_METADATA.create_all)
    yield engine
    async with engine.begin() as connection:
        await connection.run_sync(MONEY_METADATA.drop_all)


# --------------------------------------------------------------------------------------
# Criterion 1: the value survives, exactly.
# --------------------------------------------------------------------------------------


def test_numeric_text_round_trips_a_high_precision_decimal() -> None:
    """Both `==` and `str()`, because `==` alone would pass `0.10` against `0.1`."""
    stored = SCALE_18.process_bind_param(HIGH_PRECISION, DIALECT)
    restored = SCALE_18.process_result_value(stored, DIALECT)

    assert stored == "0.000000012345678901"
    assert restored == HIGH_PRECISION
    assert str(restored) == str(HIGH_PRECISION)
    assert restored is not None
    assert restored.as_tuple() == HIGH_PRECISION.as_tuple()


async def test_numeric_text_round_trips_through_a_real_column(
    money_engine: AsyncEngine,
) -> None:
    """The same value, written to a file by SQLite and read back out of it."""
    async with money_engine.begin() as connection:
        await connection.execute(amounts.insert().values(id=1, wide=HIGH_PRECISION))

    async with money_engine.connect() as connection:
        restored = await connection.scalar(select(amounts.c.wide).where(amounts.c.id == 1))
        raw = await connection.scalar(text("SELECT wide FROM amounts WHERE id = 1"))
        affinity = await connection.scalar(text("SELECT typeof(wide) FROM amounts WHERE id = 1"))

    assert restored == HIGH_PRECISION
    assert str(restored) == str(HIGH_PRECISION)
    # The bytes on disk are the canonical fixed-point string, not a double and not
    # scientific notation. This is the assertion `sqlalchemy.Numeric` would fail.
    assert raw == "0.000000012345678901"
    assert affinity == "text"


async def test_the_declared_column_type_is_text(money_engine: AsyncEngine) -> None:
    """`TEXT` in the schema itself, so nothing later applies numeric affinity to it."""
    async with money_engine.connect() as connection:
        rows = (await connection.execute(text("PRAGMA table_info(amounts)"))).all()

    declared = {row[1]: row[2] for row in rows}

    assert declared["wide"] == "TEXT"
    assert declared["fiat"] == "TEXT"
    assert declared["units"] == "BIGINT"


def test_the_stored_form_is_never_scientific() -> None:
    """`str(Decimal("1E+2"))` is `"1E+2"`; a money column must not hold that."""
    assert SCALE_2.process_bind_param(Decimal("1E+2"), DIALECT) == "100.00"
    assert SCALE_18.process_bind_param(Decimal("1E-18"), DIALECT) == "0.000000000000000001"


def test_numeric_text_pads_to_the_declared_scale() -> None:
    """One shape per column, which is what makes an equality lookup on it mean anything."""
    assert SCALE_8.process_bind_param(Decimal("1.5"), DIALECT) == "1.50000000"
    assert SCALE_0.process_bind_param(Decimal("1.5"), DIALECT) == "2"


def test_a_value_shorter_than_the_column_comes_back_padded() -> None:
    """Deliberate, and adjudicated in the spec: the scale belongs to the column.

    Through a column two places wider than the value, the amount is equal and spelled
    differently. That is what `DECIMAL(p, s)` does everywhere else, and it is the reason
    the exact-`str()` test above uses a column whose scale matches the value.
    """
    wider = NumericText(20)

    stored = wider.process_bind_param(HIGH_PRECISION, DIALECT)
    restored = wider.process_result_value(stored, DIALECT)

    assert restored == HIGH_PRECISION
    assert str(restored) == "1.234567890100E-8"
    assert str(restored) != str(HIGH_PRECISION)


# --------------------------------------------------------------------------------------
# Criterion 2: a float, a NaN or an infinity is refused, not coerced.
# --------------------------------------------------------------------------------------


def test_numeric_text_rejects_a_float() -> None:
    """Before any conversion: `Decimal(0.1)` succeeds, and by then the damage is done."""
    with pytest.raises(TypeError, match="NumericText requires a Decimal, got float"):
        SCALE_2.process_bind_param(0.1, DIALECT)


async def test_a_float_is_rejected_on_insert(money_engine: AsyncEngine) -> None:
    """The type is attached to the column, not merely defined next to it."""
    async with money_engine.begin() as connection:
        with pytest.raises(StatementError, match="NumericText requires a Decimal, got float"):
            await connection.execute(amounts.insert().values(id=1, fiat=0.1))


@pytest.mark.parametrize("value", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
def test_numeric_text_rejects_nan_and_infinity(value: str) -> None:
    """Neither is an amount, and both would store as a perfectly plausible string."""
    with pytest.raises(ValueError, match="NumericText cannot represent"):
        SCALE_2.process_bind_param(Decimal(value), DIALECT)


def test_numeric_text_rejects_a_bool() -> None:
    """`isinstance(True, int)` is `True`; without the guard it stores as `1.00`."""
    with pytest.raises(TypeError, match="NumericText requires a Decimal, got bool"):
        SCALE_2.process_bind_param(True, DIALECT)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("1.00", id="str"),
        pytest.param([Decimal("1")], id="list"),
        pytest.param(object(), id="object"),
    ],
)
def test_numeric_text_rejects_anything_else(value: object) -> None:
    with pytest.raises(TypeError, match="NumericText requires a Decimal"):
        SCALE_2.process_bind_param(value, DIALECT)


def test_numeric_text_accepts_an_int() -> None:
    """The one non-`Decimal` input with nothing after the point to lose."""
    assert SCALE_2.process_bind_param(7, DIALECT) == "7.00"


def test_numeric_text_binds_none() -> None:
    assert SCALE_2.process_bind_param(None, DIALECT) is None


def test_numeric_text_reads_a_null_as_none() -> None:
    """The NULL branch of the result processor, which no insert path reaches."""
    assert SCALE_2.process_result_value(None, DIALECT) is None


async def test_a_null_money_column_reads_back_as_none(money_engine: AsyncEngine) -> None:
    """A nullable money column really is nullable, end to end."""
    async with money_engine.begin() as connection:
        await connection.execute(amounts.insert().values(id=1))

    async with money_engine.connect() as connection:
        row = (
            await connection.execute(
                select(amounts.c.wide, amounts.c.fiat, amounts.c.units).where(amounts.c.id == 1)
            )
        ).one()

    assert row.wide is None
    assert row.fiat is None
    assert row.units is None


# --------------------------------------------------------------------------------------
# Criterion 3: half to even, at the column's declared scale.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        # The ties, at scale 0. Half-up would give "2", "3" and "4".
        #
        # `0.5` used to be the first row here and is now in
        # `test_a_non_zero_amount_that_rounds_away_is_refused`: it is the one tie at this
        # scale whose banker's-rounded result is **zero**, and a non-zero amount becoming a
        # zero is what the column now refuses. The rounding rule is unchanged; what changed
        # is that one of its outcomes is no longer storable.
        ("1.5", 0, "2"),
        ("2.5", 0, "2"),
        ("3.5", 0, "4"),
        ("-1.5", 0, "-2"),
        ("-2.5", 0, "-2"),
        # The same rule where money actually rounds.
        ("0.125", 2, "0.12"),
        ("0.135", 2, "0.14"),
        ("1.005", 2, "1.00"),
        ("1.015", 2, "1.02"),
        # Not a tie, so ordinary rounding applies.
        ("0.124", 2, "0.12"),
        ("0.126", 2, "0.13"),
    ],
)
def test_numeric_text_quantizes_half_to_even(value: str, scale: int, expected: str) -> None:
    assert NumericText(scale).process_bind_param(Decimal(value), DIALECT) == expected


async def test_the_column_rounds_on_the_way_in(money_engine: AsyncEngine) -> None:
    """The tie is resolved by the database write, not only by a unit call."""
    async with money_engine.begin() as connection:
        await connection.execute(amounts.insert().values(id=1, fiat=Decimal("1.005")))
        await connection.execute(amounts.insert().values(id=2, fiat=Decimal("1.015")))

    async with money_engine.connect() as connection:
        stored = (await connection.execute(select(amounts.c.fiat).order_by(amounts.c.id))).scalars()
        values = list(stored)

    assert values == [Decimal("1.00"), Decimal("1.02")]


def test_numeric_text_normalises_negative_zero() -> None:
    """`-0.00` and `0.00` are equal as Decimals and different as the text SQLite compares.

    Both spellings here are **already** zero, which is the whole of what this test is about:
    one stored form for one amount. `-0.001` used to be a third case on this list and has
    moved to `test_a_non_zero_amount_that_rounds_away_is_refused` -- it is not a spelling of
    zero, it is a value the scale would destroy, and the two are different questions that
    happened to produce the same string.
    """
    assert SCALE_2.process_bind_param(Decimal("-0.00"), DIALECT) == "0.00"
    assert SCALE_2.process_bind_param(Decimal("0.00"), DIALECT) == "0.00"
    assert SCALE_2.process_bind_param(Decimal("-0"), DIALECT) == "0.00"


async def test_negative_zero_is_stored_as_one_spelling(money_engine: AsyncEngine) -> None:
    """Two rows written from the two spellings hold the identical bytes."""
    async with money_engine.begin() as connection:
        await connection.execute(amounts.insert().values(id=1, fiat=Decimal("-0.00")))
        await connection.execute(amounts.insert().values(id=2, fiat=Decimal("0.00")))

    async with money_engine.connect() as connection:
        raw = (await connection.execute(text("SELECT fiat FROM amounts ORDER BY id"))).scalars()
        values = list(raw)

    assert values == ["0.00", "0.00"]


def test_numeric_text_requires_a_scale() -> None:
    """No default. A money column without a declared scale has no defined rounding."""
    with pytest.raises(TypeError, match="scale"):
        NumericText()  # type: ignore[call-arg]


# --------------------------------------------------------------------------------------
# The declared scale is validated at construction, not at bind time.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scale",
    [
        pytest.param(2.0, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(Decimal("2"), id="decimal"),
        pytest.param("2", id="str"),
        pytest.param(None, id="none"),
    ],
)
def test_numeric_text_rejects_a_scale_that_is_not_an_int(scale: object) -> None:
    """A float scale, in the type whose reason for existing is banning floats.

    It used to construct fine and die at bind time with `exponent must be an integer`,
    which names no column and sends the reader to the value rather than to the schema.
    """
    with pytest.raises(TypeError, match="NumericText requires an int scale"):
        NumericText(scale)  # type: ignore[arg-type]


@pytest.mark.parametrize("scale", [-1, -2, 39, 100])
def test_numeric_text_rejects_a_scale_outside_the_representable_range(scale: int) -> None:
    """`NumericText(-2)`, a plausible typo for `NumericText(2)`, was silent money loss.

    It bound `Decimal("12345.67")` to the string `"12300"` -- a legal-looking amount,
    quietly missing 45.67 and rounded to the nearest hundred. Nothing raised, nothing
    logged, and the column looked fine in a diff.
    """
    with pytest.raises(ValueError, match="requires a scale between 0 and 38"):
        NumericText(scale)


@pytest.mark.parametrize("scale", [0, 1, 2, 8, 18, 37, 38])
def test_numeric_text_accepts_every_scale_in_range(scale: int) -> None:
    """Both endpoints included: 0 is an integer column, 38 is the whole precision."""
    assert NumericText(scale).scale == scale


def test_a_bad_scale_fails_at_construction_not_at_insert() -> None:
    """Which makes it an import-time error, before any row exists to be wrong.

    A column definition is evaluated when its module is imported, so a typo'd scale stops
    the application from starting rather than corrupting the first write.
    """
    with pytest.raises(ValueError, match="requires a scale between 0 and 38"):
        Table(
            "never_created",
            MetaData(),
            Column("id", Integer, primary_key=True),
            Column("amount", NumericText(-2)),
        )


# --------------------------------------------------------------------------------------
# An amount too large for the digits the scale leaves in front of the point.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("scale", [0, 2, 8, 18, 38])
def test_the_usable_integer_range_is_precision_minus_scale(scale: int) -> None:
    """The boundary, walked from both sides, at every scale that matters.

    A column's integer part gets `MONEY_PRECISION - scale` digits. One more digit than
    that is the first value it cannot hold, and it has to say so rather than raise a bare
    `InvalidOperation`.
    """
    integer_digits = MONEY_PRECISION - scale
    column = NumericText(scale)
    widest = Decimal("9" * integer_digits) if integer_digits else Decimal(0)
    one_too_wide = Decimal("9" * (integer_digits + 1))

    assert column.process_bind_param(widest, DIALECT) is not None
    with pytest.raises(ValueError, match="cannot store"):
        column.process_bind_param(one_too_wide, DIALECT)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Decimal(10**36), id="one past the ceiling"),
        pytest.param(Decimal("4242424242424242424242424242424242424.2"), id="distinctive digits"),
    ],
)
def test_an_over_magnitude_value_names_the_scale_and_the_ceiling_but_not_the_amount(
    value: Decimal,
) -> None:
    """The scale and the ceiling are what an operator can act on; the amount is the owner's.

    The old failure was `decimal.InvalidOperation: [<class 'decimal.InvalidOperation'>]`,
    which names no column and no number. The message that replaced it used to quote the
    amount as well, and #12 -- the first issue to put a *quantity* in a `NumericText`
    column -- is where that stopped: a fill quantity is the owner's holdings, and this
    message reaches a log through any `logger.exception`.
    """
    with pytest.raises(ValueError, match="NumericText cannot store") as caught:
        NumericText(2).process_bind_param(value, DIALECT)

    message = str(caught.value)
    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    # The amount, in every spelling a formatter could have chosen.
    for spelling in (str(value), format(value, "f"), value.to_eng_string(), "4242", "1E+36"):
        assert spelling not in rendered, spelling
    # The positive companion: the message still says everything else it used to.
    assert "a scale of 2" in message
    assert "36 digits before the decimal point" in message
    assert str(MONEY_PRECISION) in message


def test_a_scale_of_38_leaves_no_room_in_front_of_the_point() -> None:
    """Conflating the precision with the scale builds a column that cannot hold `1.5`."""
    column = NumericText(MONEY_PRECISION)

    assert column.process_bind_param(Decimal("0.5"), DIALECT) == "0." + "5" + "0" * 37
    with pytest.raises(ValueError, match="leaves 0 digits before the decimal point"):
        column.process_bind_param(Decimal("1.5"), DIALECT)


async def test_an_over_magnitude_insert_reports_the_column_not_a_bare_decimal_error(
    money_engine: AsyncEngine,
) -> None:
    """Through a real INSERT, which is where this failure is actually met."""
    async with money_engine.begin() as connection:
        with pytest.raises(StatementError, match="NumericText cannot store"):
            await connection.execute(amounts.insert().values(id=1, fiat=Decimal(10**36)))


def test_two_scales_do_not_share_a_cache_key() -> None:
    """A shared cache entry would round a value to another column's scale, silently.

    `cache_ok = True` tells SQLAlchemy the type's state is safe to fold into a statement's
    cache key; it builds that key from the names of `__init__`'s parameters, read off the
    instance. So the key contains the scale only because the attribute is called `scale`.
    """
    key_2 = NumericText(2)._static_cache_key
    key_8 = NumericText(8)._static_cache_key

    # A type SQLAlchemy refuses to cache returns the `NO_CACHE` sentinel rather than a
    # tuple, so this is the first thing to rule out.
    assert isinstance(key_2, tuple)
    assert isinstance(key_8, tuple)
    assert key_2 != key_8
    assert ("scale", 2) in key_2
    assert ("scale", 8) in key_8


def test_a_type_that_hid_its_scale_would_share_one_cache_key() -> None:
    """The control that makes the test above mean something.

    Storing the constructor argument under any other attribute name collapses every scale
    onto a single cache key, with no warning and no error -- so the assertion above is
    pinning real behaviour rather than restating a tautology.
    """

    class Mislabelled(TypeDecorator[Decimal]):
        impl = Text
        cache_ok = True

        def __init__(self, scale: int) -> None:
            super().__init__()
            self.places = scale

    mislabelled_key = Mislabelled(2)._static_cache_key

    assert isinstance(mislabelled_key, tuple)
    assert mislabelled_key == Mislabelled(8)._static_cache_key
    assert ("scale", 2) not in mislabelled_key


async def test_two_scales_round_to_their_own_scale_through_one_engine(
    money_engine: AsyncEngine,
) -> None:
    """The consequence, observed rather than reasoned about.

    Two differently-scaled columns written through the same engine -- and therefore through
    the same compiled-statement cache -- each keep their own rounding.
    """
    async with money_engine.begin() as connection:
        await connection.execute(
            amounts.insert().values(id=1, wide=Decimal("1.005"), fiat=Decimal("1.005"))
        )

    async with money_engine.connect() as connection:
        row = (await connection.execute(text("SELECT wide, fiat FROM amounts WHERE id = 1"))).one()

    assert row.wide == "1.005000000000000000"
    assert row.fiat == "1.00"


def test_the_type_is_cacheable() -> None:
    """Without `cache_ok` SQLAlchemy warns and refuses to cache every statement using it."""
    assert NumericText.cache_ok is True
    assert BaseUnits.cache_ok is True


# --------------------------------------------------------------------------------------
# `BaseUnits`: an integer count of indivisible units, and nothing else.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1.0, id="float-that-is-whole"),
        pytest.param(0.1, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(Decimal("1"), id="decimal"),
        pytest.param("1", id="str"),
    ],
)
def test_base_units_rejects_a_non_integer(value: object) -> None:
    """`1.0` is the dangerous one: it is a float that looks exactly like a quantity."""
    with pytest.raises(TypeError, match="BaseUnits requires an int"):
        UNITS.process_bind_param(value, DIALECT)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(INT64_MAX + 1, id="above-max"),
        pytest.param(INT64_MIN - 1, id="below-min"),
        pytest.param(10**30, id="far-above"),
    ],
)
def test_base_units_rejects_a_value_sqlite_cannot_hold(value: int) -> None:
    """Rejected at the boundary, so the limit is met as an error, not as truncation."""
    with pytest.raises(ValueError, match="outside the signed 64-bit range"):
        UNITS.process_bind_param(value, DIALECT)


@pytest.mark.parametrize(
    "value",
    [0, 1, -1, INT64_MIN, INT64_MAX, BITCOIN_SUPPLY_IN_SATOSHIS, KASPA_SUPPLY_IN_SOMPI],
)
def test_base_units_accepts_every_quantity_v1_can_produce(value: int) -> None:
    """Including the two chain supplies the docstring claims are comfortable."""
    assert UNITS.process_bind_param(value, DIALECT) == value


def test_base_units_binds_none() -> None:
    assert UNITS.process_bind_param(None, DIALECT) is None


async def test_base_units_round_trips_through_a_real_column(money_engine: AsyncEngine) -> None:
    """A whole chain supply, out of a real file, as an `int` and not a float."""
    async with money_engine.begin() as connection:
        await connection.execute(amounts.insert().values(id=1, units=KASPA_SUPPLY_IN_SOMPI))

    async with money_engine.connect() as connection:
        restored = await connection.scalar(select(amounts.c.units).where(amounts.c.id == 1))
        affinity = await connection.scalar(text("SELECT typeof(units) FROM amounts WHERE id = 1"))

    assert restored == KASPA_SUPPLY_IN_SOMPI
    assert isinstance(restored, int)
    assert affinity == "integer"


# --------------------------------------------------------------------------------------
# `BaseUnits` guards the read path too, because SQLite does not enforce a column type.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(1.5, id="real"),
        pytest.param(1.0, id="whole-real"),
        pytest.param(True, id="bool"),
        pytest.param("7", id="text"),
    ],
)
def test_base_units_refuses_a_stored_value_that_is_not_an_integer(stored: object) -> None:
    """Guarding the bind side alone assumes every row arrived through the ORM."""
    with pytest.raises(TypeError, match="which is not an integer quantity"):
        UNITS.process_result_value(stored, DIALECT)


@pytest.mark.parametrize("stored", [0, 1, -1, INT64_MAX, None])
def test_base_units_passes_through_a_stored_integer(stored: int | None) -> None:
    assert UNITS.process_result_value(stored, DIALECT) == stored


async def test_a_raw_real_row_is_refused_rather_than_read_back_as_a_float(
    money_engine: AsyncEngine,
) -> None:
    """The path that defeats the AST ban entirely, because no Python code writes it.

    An Alembic `op.execute` backfill or `sqlite3` on the Pi can write `1.5` into a column
    declared `BIGINT`: SQLite has no type enforcement, the row is stored with `typeof` =
    `real`, and before this guard it came back as a Python `float` from an attribute
    annotated `Mapped[int]`. Nothing downstream would have noticed.
    """
    async with money_engine.begin() as connection:
        await connection.execute(text("INSERT INTO amounts (id, units) VALUES (1, 1.5)"))

    async with money_engine.connect() as connection:
        # The premise: SQLite really did store a real in a BIGINT column.
        assert await connection.scalar(text("SELECT typeof(units) FROM amounts WHERE id = 1")) == (
            "real"
        )
        with pytest.raises(TypeError, match="which is not an integer quantity"):
            await connection.scalar(select(amounts.c.units).where(amounts.c.id == 1))


async def test_the_refusal_names_the_value_and_says_where_it_came_from(
    money_engine: AsyncEngine,
) -> None:
    """A read-side failure has no INSERT to blame, so the message has to carry the cause."""
    async with money_engine.begin() as connection:
        await connection.execute(text("INSERT INTO amounts (id, units) VALUES (1, 2.5)"))

    async with money_engine.connect() as connection:
        with pytest.raises(TypeError) as caught:
            await connection.scalar(select(amounts.c.units).where(amounts.c.id == 1))

    message = str(caught.value)
    assert "2.5" in message
    assert "float" in message
    assert "not written through this type" in message


# --------------------------------------------------------------------------------------
# Test isolation: this module's table must stay out of the application's schema.
# --------------------------------------------------------------------------------------


def test_the_money_table_is_not_in_the_application_metadata() -> None:
    """Otherwise the migration drift check from #1 reports a table nothing creates.

    That failure would point at the schema and the migrations, neither of which changed in
    this issue, and the hours would go there before they went here.
    """
    assert "amounts" not in models.metadata.tables
    assert MONEY_METADATA is not models.metadata
    assert set(MONEY_METADATA.tables) == {"amounts"}


# --------------------------------------------------------------------------------------
# An amount the scale would destroy: refused, not rounded to nothing
# --------------------------------------------------------------------------------------
#
# The column's two over-precision rules used to be "too many digits after the point is
# rounded away, too many in front is refused". The first of those had an outcome nobody had
# looked at: a value small enough that rounding leaves **zero**.
#
# That is not a rounding. It is the amount being destroyed, and it is the one outcome a
# money column must never produce quietly, because a zero is a number every consumer
# believes. Measured end to end before the guard existed, through the real service and a
# real migrated database:
#
#     vendor sends 0.0000000000005  ->  stored TEXT "0.000000000000"
#     value_portfolio(1_000_000 KAS) -> total=0E-12  complete=True  unpriced=()
#
# A positive price became a zero and `complete=True` told every renderer the total was
# whole. There is no missing row for a valuation to notice and no reason for it to report:
# it is `services/prices.py`'s whole subject arriving through the column instead of through
# an absent row.
#
# The guard lives here rather than in the prices repository because it is a property of the
# **column**. A fee, a fill or a cost basis added later meets the same boundary, and a guard
# beside one caller protects one caller.


@pytest.mark.parametrize(
    ("value", "scale"),
    [
        pytest.param("0.0000000000005", 12, id="the measured price, at the price scale"),
        pytest.param("0.5", 0, id="the one banker's tie at scale 0 that rounds to zero"),
        pytest.param("-0.5", 0, id="and its negative"),
        pytest.param("0.001", 2, id="a tenth of a cent"),
        pytest.param("0.005", 2, id="a half cent, which rounds to zero half-to-even"),
        pytest.param("-0.001", 2, id="a negative that used to be a spelling of zero"),
        pytest.param("1E-30", 2, id="far below the scale"),
        pytest.param("0.04", 1, id="rounds down to zero at one place"),
        pytest.param("0.05", 1, id="and the half-even tie there, which also lands on zero"),
    ],
)
def test_a_non_zero_amount_that_rounds_away_is_refused(value: str, scale: int) -> None:
    """A `ValueError`, in the same vocabulary as the too-many-digits-in-front refusal.

    The two over-precision rules are now one rule with two ends: the column refuses an
    amount it cannot represent, whichever end of the number the lost digits are at. A
    reader meeting them side by side should not have to learn that one of them silently
    succeeds.

    `0.5` at scale 0 is the row worth reading twice. It is a genuine banker's-rounding tie
    and its correct rounded value *is* zero -- the rounding rule is not wrong, the storage
    is impossible. `1.5` at the same scale is in
    `test_numeric_text_quantizes_half_to_even` and still stores `2`, which is what makes
    the pair a boundary rather than a blanket refusal of small numbers.
    """
    with pytest.raises(ValueError, match=r"(?i)finer than its scale") as caught:
        NumericText(scale).process_bind_param(Decimal(value), DIALECT)

    assert not isinstance(caught.value, TypeError), (
        "a value the column cannot represent is not a caller passing the wrong type"
    )


def test_the_refusal_names_the_scale_and_never_the_amount() -> None:
    """The scale is what an operator can act on; the amount is the owner's data.

    `prices.amount` holds public market data, so quoting a price would be harmless today.
    This type is the one every future money column is built from, and since #12 one holds a
    **quantity** -- which is the owner's holdings. The sibling refusal for an over-large
    amount stopped quoting its value in #12 as well; both are asserted.
    """
    with pytest.raises(ValueError, match=r"finer than its scale") as caught:
        NumericText(12).process_bind_param(Decimal("0.0000000000005"), DIALECT)

    message = str(caught.value)

    assert "12" in message
    assert "0.0000000000005" not in message
    assert "5E-13" not in message


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        pytest.param("0", 12, "0.000000000000", id="a plain zero"),
        pytest.param("0.00", 2, "0.00", id="a zero that already carries a scale"),
        pytest.param("-0.00", 2, "0.00", id="a negative zero"),
        pytest.param("0E-30", 12, "0.000000000000", id="a zero with an exponent"),
    ],
)
def test_a_true_zero_still_binds(value: str, scale: int, expected: str) -> None:
    """The boundary the guard must not cross, and the half a naive check would get wrong.

    "The quantized result is zero" is the obvious way to write this refusal and it is
    wrong: it refuses zero itself. A price of zero is already refused upstream by
    `require_price`, but a money column holds more than prices -- a realised gain of
    exactly nothing, a fee that was waived -- and a type that could not store zero would be
    useless for them.

    The guard is "the result is zero **and the input was not**", and this is the test that
    says so.
    """
    assert NumericText(scale).process_bind_param(Decimal(value), DIALECT) == expected


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        pytest.param("0.01", 2, "0.01", id="the smallest representable amount at scale 2"),
        pytest.param("0.006", 2, "0.01", id="rounds up to it rather than away"),
        pytest.param("0.000000000001", 12, "0.000000000001", id="one unit at the price scale"),
        pytest.param("0.0000000000006", 12, "0.000000000001", id="rounds up to one unit"),
    ],
)
def test_the_smallest_representable_amount_still_binds(
    value: str,
    scale: int,
    expected: str,
) -> None:
    """The other side of the boundary: refusing what vanishes must not refuse what survives.

    Each pair here is one digit away from a row in the refusal table above. `0.006` at scale
    2 rounds *up* to `0.01` and is stored; `0.005` rounds to zero and is refused. A guard
    written against the magnitude of the input rather than against the rounded result would
    fail these, and would quietly refuse every legitimate sub-cent amount a fee column will
    ever hold.
    """
    assert NumericText(scale).process_bind_param(Decimal(value), DIALECT) == expected


async def test_the_database_write_is_what_refuses_an_amount_that_rounds_away(
    money_engine: AsyncEngine,
) -> None:
    """Through a real column and a real INSERT, not only through a unit call on the type.

    A `TypeDecorator` that was never attached to the column, or a column built with a
    different scale than the test believed, would satisfy every assertion above. This is
    the one that says the refusal reaches the statement -- and that **nothing is written**,
    which is the outcome that matters: a refusal that still left a zero row behind would be
    the original defect with a traceback attached.
    """
    with pytest.raises(StatementError) as caught:
        async with money_engine.begin() as connection:
            await connection.execute(amounts.insert().values(id=1, fiat=Decimal("0.001")))

    assert isinstance(caught.value.orig, ValueError)
    assert "finer than its scale" in str(caught.value.orig)

    async with money_engine.connect() as connection:
        remaining = await connection.scalar(text("SELECT COUNT(*) FROM amounts"))

    assert remaining == 0, "a refused amount must not have left a zero row behind"
