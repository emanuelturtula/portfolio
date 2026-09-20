"""`domain/money.py`: criteria 3, 4 and 5.

Three things are being proved here, and each one has an obvious test that proves nothing.

* The context precision (criterion 4) is asserted from a **real worker thread**, not only
  from the thread that ran the import. `decimal.getcontext()` is thread-local, so a test
  that only reads it in the main thread would pass with `DefaultContext` left at 28 and the
  guarantee quietly covering one thread out of however many Starlette runs.
* The base-unit round trip (criterion 5) is asserted over generated inputs rather than
  three hand-picked ones, because the failing input for this kind of arithmetic is exactly
  the one nobody thinks to write down.
* The rounding mode (criterion 3) is asserted at the ties `ROUND_HALF_EVEN` exists to
  disambiguate, not by reading `MONEY_ROUNDING` back out of the module -- that would assert
  that a constant equals itself.

Everything here is pure: no fixtures, no I/O, no database.
"""

from __future__ import annotations

import decimal
import threading
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portfolio.domain.money import (
    MONEY_PRECISION,
    MONEY_ROUNDING,
    from_base_units,
    quantize,
    require_amount,
    to_base_units,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# The exponents `assets.decimals` actually carries: 0 for an indivisible unit, 2 for fiat,
# 8 for a satoshi or a sompi, 18 for an EVM-style token. The column is a plain `Integer`
# with no CHECK, so this range is the product's range rather than the schema's.
MIN_DECIMALS: Final = 0
MAX_DECIMALS: Final = 18

# Wide enough to cross every int size the CPython fast paths care about, narrow enough that
# a shrinking run stays fast.
MAX_UNITS: Final = 10**30

DEFAULT_DECIMAL_PRECISION: Final = 28
"""What `decimal` uses when nobody raises it -- the value criterion 4 exists to replace."""


# --------------------------------------------------------------------------------------
# Criterion 4: the decimal context is 38 digits, in this thread and in new ones.
# --------------------------------------------------------------------------------------


def test_the_decimal_context_precision_is_38() -> None:
    """Importing the module raised the calling thread's precision, as a side effect."""
    assert MONEY_PRECISION == 38
    assert decimal.getcontext().prec == MONEY_PRECISION


def test_the_default_context_precision_is_38() -> None:
    """The template every new thread copies, which is the half that is easy to forget."""
    assert decimal.DefaultContext.prec == MONEY_PRECISION


def test_a_worker_thread_inherits_the_precision() -> None:
    """The assertion that fails when only `getcontext()` is set.

    A thread builds its context by copying `DefaultContext` the first time it asks for
    one, so this thread -- which has imported nothing and called nothing -- reports 38 only
    because the module set both. With `getcontext()` alone it reports 28, and every
    quantization inside `anyio.to_thread.run_sync` would silently run at that precision.
    """
    observed: list[int] = []

    def record_precision() -> None:
        observed.append(decimal.getcontext().prec)

    thread = threading.Thread(target=record_precision)
    thread.start()
    thread.join()

    assert observed == [MONEY_PRECISION]
    assert observed != [DEFAULT_DECIMAL_PRECISION]


def test_a_worker_thread_computes_at_the_raised_precision() -> None:
    """Not just the number in the context: an actual division in a fresh thread.

    Reading `prec` back proves the attribute was set. This proves the attribute is the one
    arithmetic uses, which is the property criterion 4 is actually about.
    """
    results: list[Decimal] = []

    def divide() -> None:
        results.append(Decimal(1) / Decimal(3))

    thread = threading.Thread(target=divide)
    thread.start()
    thread.join()

    (quotient,) = results
    # 38 significant digits, so "0." plus 38 threes.
    assert str(quotient) == "0." + "3" * MONEY_PRECISION
    assert len(quotient.as_tuple().digits) == MONEY_PRECISION


# --------------------------------------------------------------------------------------
# Criterion 3: half to even, at the requested scale.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [
        # The ties. Half-up would give 1, 2 and 3; half-even gives the even neighbour.
        ("0.5", 0, "0"),
        ("1.5", 0, "2"),
        ("2.5", 0, "2"),
        ("3.5", 0, "4"),
        ("-0.5", 0, "-0"),
        ("-1.5", 0, "-2"),
        ("-2.5", 0, "-2"),
        # The same rule one place further in, which is where money actually rounds.
        ("0.125", 2, "0.12"),
        ("0.135", 2, "0.14"),
        ("1.005", 2, "1.00"),
        # Not a tie: ordinary rounding still rounds.
        ("0.126", 2, "0.13"),
        ("0.124", 2, "0.12"),
    ],
)
def test_quantize_rounds_half_to_even(value: str, scale: int, expected: str) -> None:
    result = quantize(Decimal(value), scale)

    assert str(result) == expected


def test_quantize_pads_to_the_declared_scale() -> None:
    """The scale is a shape, not a maximum: a short value is padded, not left short."""
    assert str(quantize(Decimal("1.5"), 8)) == "1.50000000"
    assert str(quantize(Decimal("2"), 2)) == "2.00"


def test_the_rounding_mode_is_the_unbiased_one() -> None:
    """`ROUND_HALF_UP` drifts upward over thousands of fills; this is the guard."""
    assert MONEY_ROUNDING == decimal.ROUND_HALF_EVEN


def test_quantize_refuses_a_value_wider_than_the_context() -> None:
    """A number this application has decided it does not represent, refused loudly."""
    too_wide = Decimal("1" * (MONEY_PRECISION + 1))

    with pytest.raises(decimal.InvalidOperation):
        quantize(too_wide, 0)


# --------------------------------------------------------------------------------------
# `require_amount`: the shared guard `NumericText` and `to_base_units` both delegate to.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0.1, id="float"),
        pytest.param(True, id="bool"),
        pytest.param("1.00", id="str"),
        pytest.param(1, id="int"),
        pytest.param(None, id="none"),
    ],
)
def test_require_amount_rejects_anything_that_is_not_a_decimal(value: object) -> None:
    """`Decimal(0.1)` succeeds and is already wrong, so the refusal comes first."""
    with pytest.raises(TypeError, match="requires a Decimal"):
        require_amount(value, subject="probe")


@pytest.mark.parametrize("value", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
def test_require_amount_rejects_a_value_that_is_not_finite(value: str) -> None:
    with pytest.raises(ValueError, match="cannot represent"):
        require_amount(Decimal(value), subject="probe")


def test_require_amount_names_the_caller_in_the_message() -> None:
    """A rejected write should say which column or conversion refused it."""
    with pytest.raises(TypeError, match=r"holdings\.quantity requires a Decimal, got float"):
        require_amount(1.5, subject="holdings.quantity")


def test_require_amount_returns_the_value_it_accepted() -> None:
    amount = Decimal("1.25")

    assert require_amount(amount, subject="probe") is amount


# --------------------------------------------------------------------------------------
# Criterion 5: base-unit conversion round-trips, for any valid input.
# --------------------------------------------------------------------------------------


@st.composite
def amounts_and_exponents(draw: st.DrawFn) -> tuple[Decimal, int]:
    """An amount and a `decimals` exponent wide enough to express it exactly."""
    decimals = draw(st.integers(min_value=MIN_DECIMALS, max_value=MAX_DECIMALS))
    places = draw(st.integers(min_value=MIN_DECIMALS, max_value=decimals))
    amount = draw(
        st.decimals(
            min_value=Decimal(-MAX_UNITS),
            max_value=Decimal(MAX_UNITS),
            allow_nan=False,
            allow_infinity=False,
            places=places,
        )
    )
    return amount, decimals


@given(case=amounts_and_exponents())
def test_base_unit_conversion_round_trips(case: tuple[Decimal, int]) -> None:
    """Decimal -> integer -> Decimal preserves the amount, for any representable one.

    Asserted by `==` rather than by `str()`, and deliberately: `Decimal("-0.00")` converts
    to the integer `0`, integers have no signed zero, and `0` converts back to
    `Decimal("0.00")`. That is equal in value and different in spelling, and the equality
    is the property that is actually true.
    """
    amount, decimals = case

    units = to_base_units(amount, decimals)
    restored = from_base_units(units, decimals)

    assert isinstance(units, int)
    assert not isinstance(units, bool)
    assert restored == amount
    # The returned value is canonical at the column's exponent, whatever shape it went in.
    assert restored.as_tuple().exponent == -decimals


@given(
    units=st.integers(min_value=-MAX_UNITS, max_value=MAX_UNITS),
    decimals=st.integers(min_value=MIN_DECIMALS, max_value=MAX_DECIMALS),
)
def test_base_unit_conversion_round_trips_from_the_integer_side(units: int, decimals: int) -> None:
    """integer -> Decimal -> integer is exact, with no equality escape hatch.

    The direction that matters for a chain balance: the integer is what the node reported,
    and it has to come back bit for bit.
    """
    assert to_base_units(from_base_units(units, decimals), decimals) == units


@pytest.mark.parametrize(
    ("amount", "decimals", "expected"),
    [
        ("1", 8, 100_000_000),
        ("0.00000001", 8, 1),
        ("-0.00000001", 8, -1),
        ("21000000", 8, 2_100_000_000_000_000),
        ("0", 8, 0),
        ("-0.00", 2, 0),
        ("1.5", 2, 150),
        ("1.50", 2, 150),
        ("123", 0, 123),
    ],
)
def test_to_base_units_worked_examples(amount: str, decimals: int, expected: int) -> None:
    """The arithmetic in numbers a person can check, alongside the generated property."""
    assert to_base_units(Decimal(amount), decimals) == expected


def test_to_base_units_normalises_negative_zero() -> None:
    """Stated as its own test because the property test cannot assert it by `str()`."""
    assert to_base_units(Decimal("-0.00"), 2) == 0
    assert str(from_base_units(to_base_units(Decimal("-0.00"), 2), 2)) == "0.00"


@pytest.mark.parametrize(
    ("amount", "decimals"),
    [
        ("0.000000001", 8),
        ("1.5", 0),
        ("0.01", 1),
        ("-0.000000001", 8),
    ],
)
def test_to_base_units_rejects_unrepresentable_precision(amount: str, decimals: int) -> None:
    """An amount finer than the chain counts is refused, never rounded.

    A balance read from a chain is exact. Rounding one here would mean reporting a holding
    the chain does not agree with, which is worse than failing.
    """
    with pytest.raises(ValueError, match="cannot be expressed in base units"):
        to_base_units(Decimal(amount), decimals)


@pytest.mark.parametrize("value", [0.1, True, "1", None])
def test_to_base_units_rejects_a_non_decimal(value: object) -> None:
    with pytest.raises(TypeError, match="to_base_units requires a Decimal"):
        to_base_units(value, 8)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_to_base_units_rejects_a_value_that_is_not_finite(value: str) -> None:
    with pytest.raises(ValueError, match="to_base_units cannot represent"):
        to_base_units(Decimal(value), 8)


@pytest.mark.parametrize(
    ("units", "decimals", "expected"),
    [
        (100_000_000, 8, "1.00000000"),
        (1, 8, "0.00000001"),
        (-1, 8, "-0.00000001"),
        (0, 8, "0.00000000"),
        (0, 0, "0"),
        (-150, 2, "-1.50"),
        (123, 0, "123"),
    ],
)
def test_from_base_units_renders_at_the_declared_exponent(
    units: int, decimals: int, expected: str
) -> None:
    """Compared through `format(value, "f")`, which is how money is ever rendered.

    `str(Decimal)` switches to scientific notation once the adjusted exponent drops below
    -6, so `str(from_base_units(1, 8))` is `"1E-8"` -- the right value in a shape no client
    should be handed. Both `NumericText` and `MoneyStr` therefore render with `"f"`, and so
    does this assertion.
    """
    result = from_base_units(units, decimals)

    assert format(result, "f") == expected
    assert result.as_tuple().exponent == -decimals


def test_from_base_units_does_not_divide() -> None:
    """Assembled from its parts, so the context precision cannot truncate the result.

    38 significant digits is the context limit; a division would clip this value to it.
    """
    units = int("9" * 45)

    restored = from_base_units(units, 18)

    assert len(restored.as_tuple().digits) == 45
    assert to_base_units(restored, 18) == units


@pytest.mark.parametrize(
    "conversion",
    [
        pytest.param(lambda: to_base_units(Decimal("1.5"), 8), id="to_base_units"),
        pytest.param(lambda: from_base_units(150_000_000, 8), id="from_base_units"),
        # This entry is the one that was missing. The parametrization covered both
        # conversions and not `quantize`, and `quantize` was the function that actually
        # read the ambient context -- so the omission is what let the bug through.
        pytest.param(lambda: quantize(Decimal("1234567890.12345"), 2), id="quantize"),
    ],
)
def test_conversion_does_not_depend_on_the_ambient_precision(
    conversion: Callable[[], object],
) -> None:
    """Lowering the context to 1 digit must not change any of the three answers.

    The conversions build their results from `Decimal` tuples rather than by arithmetic,
    and `quantize` passes an explicit `decimal.Context`, so a caller running inside a
    narrowed context still gets the exact value.
    """
    expected = conversion()
    with decimal.localcontext() as context:
        context.prec = 1

        assert conversion() == expected


def test_quantize_ignores_a_narrowed_ambient_context() -> None:
    """The regression, spelled out at the precision that produced it.

    `Decimal.quantize` uses the calling thread's context unless one is passed. Inside
    `localcontext(prec=9)` this value needs 12 significant digits, so the same call that
    succeeds outside used to raise `InvalidOperation` inside -- an answer that depended on
    ambient state a caller three frames up could change.
    """
    value = Decimal("1234567890.12345")

    with decimal.localcontext() as context:
        context.prec = 9
        inside = quantize(value, 2)

    assert inside == Decimal("1234567890.12")
    assert inside == quantize(value, 2)


def test_quantize_does_not_widen_past_money_precision_either() -> None:
    """The explicit context is a bound, not merely an escape from the ambient one.

    Raising the caller's context to 60 digits must not buy a value more room than this
    application represents, or the ceiling would be whatever the last caller set.
    """
    too_wide = Decimal("1" * (MONEY_PRECISION + 1))

    with decimal.localcontext() as context:
        context.prec = 60

        with pytest.raises(decimal.InvalidOperation):
            quantize(too_wide, 0)


# --------------------------------------------------------------------------------------
# Both conversions guard their arguments, on both sides.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "units",
    [
        pytest.param(True, id="bool"),
        pytest.param(1.5, id="float"),
        pytest.param(1.0, id="whole-float"),
        pytest.param(Decimal("1"), id="decimal"),
        pytest.param("1", id="str"),
        pytest.param(None, id="none"),
    ],
)
def test_from_base_units_rejects_units_that_are_not_an_int(units: object) -> None:
    """Two real failures, one of them silent.

    `from_base_units(True, 2)` returned `Decimal("0.01")` -- a boolean reported as a
    holding of one base unit, with nothing raised. `from_base_units(1.5, 2)` raised
    `ValueError: invalid literal for int() with base 10: '.'`, which is an error from deep
    inside a string comprehension and says nothing about base units.
    """
    with pytest.raises(TypeError, match="from_base_units requires an int"):
        from_base_units(units, 2)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "conversion",
    [
        pytest.param(to_base_units, id="to_base_units"),
        pytest.param(from_base_units, id="from_base_units"),
    ],
)
@pytest.mark.parametrize(
    "decimals",
    [
        pytest.param(True, id="bool"),
        pytest.param(2.0, id="float"),
        pytest.param(Decimal("2"), id="decimal"),
        pytest.param("2", id="str"),
        pytest.param(None, id="none"),
    ],
)
def test_both_conversions_reject_a_decimals_that_is_not_an_int(
    conversion: Callable[[object, object], object], decimals: object
) -> None:
    """The exponent is a count of places; a `bool` or a float is not one."""
    amount: object = Decimal("1") if conversion is to_base_units else 1

    with pytest.raises(TypeError, match="requires an int number of decimals"):
        conversion(amount, decimals)


@pytest.mark.parametrize(
    "conversion",
    [
        pytest.param(to_base_units, id="to_base_units"),
        pytest.param(from_base_units, id="from_base_units"),
    ],
)
@pytest.mark.parametrize("decimals", [-1, -2, -18])
def test_both_conversions_reject_a_negative_decimals(
    conversion: Callable[[object, object], object], decimals: int
) -> None:
    """Its own rejection, rather than a sentence that sends the reader to the wrong place.

    `to_base_units(Decimal("1"), -2)` used to complain that `1` "carries more than -2
    decimal places", which is not a sentence, and points at the amount rather than at the
    exponent the caller got wrong.
    """
    amount: object = Decimal("1") if conversion is to_base_units else 1

    with pytest.raises(ValueError, match="requires a non-negative number of decimals"):
        conversion(amount, decimals)


def test_the_negative_decimals_message_no_longer_blames_the_amount() -> None:
    """Pinned as text because the whole point of the fix was the wording."""
    with pytest.raises(ValueError, match="to_base_units") as caught:
        to_base_units(Decimal("1"), -2)

    message = str(caught.value)
    assert "non-negative number of decimals" in message
    assert "carries more than" not in message
