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
from fractions import Fraction
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.money import (
    MONEY_PRECISION,
    MONEY_ROUNDING,
    add,
    divide,
    from_base_units,
    multiply,
    quantize,
    require_amount,
    subtract,
    to_base_units,
)
from tests.domain.accounting.oracle import round_half_even

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


# --------------------------------------------------------------------------------------
# `multiply` (#12): the exact product, with no rounding at all.
# --------------------------------------------------------------------------------------
#
# `derive_quote_quantity` is `quantize(multiply(quantity, price), FILL_SCALE)`, and
# `quantize` must be the only rounding in it. `Decimal.__mul__` rounds to the calling
# thread's precision -- 28 by default, lower inside any `localcontext` a caller opened -- and
# even a multiply under the 38-digit money context rounds a product past 38 digits, which
# `quantize` would then round a second time. Two half-even roundings in a row can land one
# unit away from one rounding; `tests/providers/exchanges/test_base.py` has the fill that
# shows it.

#: 29 significant digits, one past the `decimal` default of 28. Doubled by hand: each
#: ten-digit group `1234567890` doubles to `2469135780`, and `12345678.9` doubles to
#: `24691357.8`.
TWENTY_NINE_DIGITS: Final = Decimal("1234567890123456789012345678.9")
TWENTY_NINE_DIGITS_DOUBLED: Final = "2469135780246913578024691357.8"

#: `10**20 + 1`, squared by hand: `10**40 + 2 * 10**20 + 1`. 41 significant digits, three
#: past `MONEY_PRECISION`, so any multiply that rounds -- under any context -- loses the
#: final `1`.
TEN_TO_THE_TWENTY_PLUS_ONE: Final = Decimal("100000000000000000001")
ITS_SQUARE: Final = "1" + "0" * 19 + "2" + "0" * 19 + "1"

#: A fill-shaped product, 39 significant digits: `124.499999999999999995` times
#: `1.000000000000000001` is the price plus the price shifted 18 places right, which adds
#: `124` into the eighteenth place and appends the price's own fractional digits after it.
FILL_PRICE: Final = Decimal("124.499999999999999995")
FILL_QUANTITY: Final = Decimal("1.000000000000000001")
FILL_PRODUCT: Final = "124.500000000000000119499999999999999995"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        pytest.param(TWENTY_NINE_DIGITS, Decimal(2), TWENTY_NINE_DIGITS_DOUBLED, id="29 digits"),
        pytest.param(
            TEN_TO_THE_TWENTY_PLUS_ONE, TEN_TO_THE_TWENTY_PLUS_ONE, ITS_SQUARE, id="41 digits"
        ),
        pytest.param(FILL_QUANTITY, FILL_PRICE, FILL_PRODUCT, id="39 digits, fill-shaped"),
    ],
)
def test_multiply_is_exact(left: Decimal, right: Decimal, expected: str) -> None:
    """Exact past 38 significant digits, in this thread and inside a narrowed context alike.

    The expected strings are written by hand (see the constants). The control computes the
    same product with `*` inside the narrowed context and shows it is *not* exact there, so
    the assertion on `multiply` is about how it multiplies and not about numbers too small
    to round.
    """
    assert len(ITS_SQUARE) == 41
    assert str(multiply(left, right)) == expected
    assert str(multiply(right, left)) == expected

    with decimal.localcontext() as context:
        context.prec = 10
        inside = multiply(left, right)
        ambient = left * right

    assert str(inside) == expected
    assert str(ambient) != expected, "the control did not round; the test proves nothing"


def test_multiply_is_exact_past_the_money_precision_where_a_money_context_would_round() -> None:
    """The departure from a multiply under the 38-digit context, pinned on its own.

    `Decimal.multiply` under a 38-digit context is the obvious implementation and it rounds
    the 41-digit square to `1.0000000000000000000200000000000000000E+40`, losing the final
    unit. Computed here as the control, so the exact answer is seen to differ from it.
    """
    rounded_at_38 = decimal.Context(prec=MONEY_PRECISION).multiply(
        TEN_TO_THE_TWENTY_PLUS_ONE, TEN_TO_THE_TWENTY_PLUS_ONE
    )

    exact = multiply(TEN_TO_THE_TWENTY_PLUS_ONE, TEN_TO_THE_TWENTY_PLUS_ONE)

    assert exact == Decimal(ITS_SQUARE)
    assert exact != rounded_at_38
    assert exact - rounded_at_38 == 1


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        pytest.param("-1.5", "2", "-3.0", id="negative left"),
        pytest.param("1.5", "-2", "-3.0", id="negative right"),
        pytest.param("-1.5", "-2", "3.0", id="both negative"),
        pytest.param("0.1", "0.1", "0.01", id="a product smaller than either factor"),
        pytest.param("0", "-7.25", "0", id="zero"),
        pytest.param("2.50", "4", "10.00", id="trailing zeros are kept as places"),
    ],
)
def test_multiply_gets_the_sign_and_the_places_right(left: str, right: str, expected: str) -> None:
    """An exact product assembled from coefficients has two easy bugs: the sign and the exponent.

    Compared by value, and by places through `as_tuple().exponent` where it is not zero: the
    exact product of an `m`-place and an `n`-place number has `m + n` places.
    """
    product = multiply(Decimal(left), Decimal(right))

    assert product == Decimal(expected)
    if not product.is_zero():
        assert product.as_tuple().exponent == Decimal(expected).as_tuple().exponent


def test_multiply_is_exact_in_a_worker_thread_at_the_decimal_default() -> None:
    """A thread whose context is the library default of 28 still gets the exact product.

    `DefaultContext` is raised to 38 at import, so a fresh thread would not show the defect.
    This thread lowers its own context to the library default first, which is the state any
    thread that materialised its context before the import would be in.
    """
    results: list[Decimal] = []

    def compute() -> None:
        decimal.getcontext().prec = DEFAULT_DECIMAL_PRECISION
        results.append(multiply(TWENTY_NINE_DIGITS, Decimal(2)))

    thread = threading.Thread(target=compute)
    thread.start()
    thread.join()

    assert [str(result) for result in results] == [TWENTY_NINE_DIGITS_DOUBLED]


@pytest.mark.parametrize(
    ("left", "right", "raised"),
    [
        pytest.param(1.5, Decimal(2), TypeError, id="float left"),
        pytest.param(Decimal(2), True, TypeError, id="bool right"),
        pytest.param(Decimal("NaN"), Decimal(2), ValueError, id="nan"),
        pytest.param(Decimal(2), Decimal("Infinity"), ValueError, id="infinity"),
    ],
)
def test_multiply_refuses_what_is_not_a_finite_decimal(
    left: object, right: object, raised: type[Exception]
) -> None:
    """The `require_amount` guard, on both operands."""
    with pytest.raises(raised):
        multiply(left, right)  # type: ignore[arg-type]


#: Past CPython's int/str conversion limit of 4300 digits (`sys.get_int_max_str_digits`).
#: The first `multiply` built its product through `int(str)` and escaped with a bare
#: `ValueError` here; the contract since is "exact on any operand".
OVERLONG_ONES: Final = 5000


def test_multiply_is_exact_past_the_int_str_digit_limit() -> None:
    """1.111... (5000 ones) doubled is 2.222... (5000 twos): exact, and context-free.

    Asserted on the digit tuple and on the fixed-point rendering, both built by hand from
    the same counts; the squaring below checks a 10001-digit coefficient the same way.
    """
    long_amount = Decimal("1." + "1" * OVERLONG_ONES)

    doubled = multiply(long_amount, Decimal(2))
    with decimal.localcontext() as context:
        context.prec = 10
        doubled_inside = multiply(long_amount, Decimal(2))

    assert doubled.as_tuple() == (0, (2,) * (OVERLONG_ONES + 1), -OVERLONG_ONES)
    assert format(doubled, "f") == "2." + "2" * OVERLONG_ONES
    assert doubled_inside.as_tuple() == doubled.as_tuple()


def test_multiply_squares_a_coefficient_past_the_digit_limit() -> None:
    """`(10**5000 + 1) ** 2` is `10**10000 + 2 * 10**5000 + 1`: a 1, a 2 and a 1, zeros between."""
    operand = Decimal((0, (1,) + (0,) * 4999 + (1,), 0))
    expected_digits = (1,) + (0,) * 4999 + (2,) + (0,) * 4999 + (1,)

    squared = multiply(operand, operand)

    assert len(operand.as_tuple().digits) == 5001
    assert squared.as_tuple() == (0, expected_digits, 0)


def test_multiply_refuses_a_product_whose_exponent_decimal_cannot_hold() -> None:
    """The documented edge: an exponent past what `Decimal` represents at all."""
    enormous = Decimal("1E+999999999999999999")

    with pytest.raises(decimal.InvalidOperation):
        multiply(enormous, enormous)


# --------------------------------------------------------------------------------------
# `add`, `subtract` and `divide` (#17): the engine's only arithmetic.
# --------------------------------------------------------------------------------------
#
# Spec 019 forbids a bare `+ - * /` on a `Decimal` anywhere in the accounting package, so
# these three and `multiply` are the whole of its arithmetic. `add` and `subtract` must be
# exact however many digits they carry, and `divide` must round exactly once. Every
# expectation below is either written out by hand or computed in `fractions.Fraction` by
# the oracle's `round_half_even`, which rounds an exact rational and nothing else -- never
# by calling the function under test a second way.

#: 41 significant digits: `10**40` plus one unit in the 18th decimal place. Any sum under a
#: context of 38 digits or fewer loses the trailing `1`.
FORTY_DIGIT_ONE: Final = Decimal("1" + "0" * 40)
ONE_UNIT_AT_18: Final = Decimal("0.000000000000000001")


def test_add_is_exact_past_the_money_precision() -> None:
    """`10**40 + 1E-18` keeps both ends: 59 significant digits, none rounded."""
    total = add(FORTY_DIGIT_ONE, ONE_UNIT_AT_18)

    assert format(total, "f") == "1" + "0" * 40 + "." + "0" * 17 + "1"
    assert total.as_tuple().exponent == -18
    # The control: the operator, even under the 38-digit money context, drops the unit.
    money_context = decimal.Context(prec=MONEY_PRECISION)
    assert money_context.add(FORTY_DIGIT_ONE, ONE_UNIT_AT_18) == FORTY_DIGIT_ONE


def test_subtract_is_exact_past_the_money_precision() -> None:
    """`10**40 - 1E-18` is forty nines and eighteen, with no rounding back up to `10**40`."""
    difference = subtract(FORTY_DIGIT_ONE, ONE_UNIT_AT_18)

    assert format(difference, "f") == "9" * 40 + "." + "9" * 18
    money_context = decimal.Context(prec=MONEY_PRECISION)
    assert money_context.subtract(FORTY_DIGIT_ONE, ONE_UNIT_AT_18) == FORTY_DIGIT_ONE


def test_add_and_subtract_ignore_a_narrowed_ambient_context() -> None:
    """Inside `localcontext(prec=6, rounding=ROUND_UP)` the answers are the same exact ones."""
    left = Decimal("123456.789012345678901234")
    right = Decimal("0.000000000000000001")
    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_UP
        inside_sum = add(left, right)
        inside_difference = subtract(left, right)
        ambient = left + right

    assert inside_sum == Decimal("123456.789012345678901235")
    assert inside_difference == Decimal("123456.789012345678901233")
    assert ambient != inside_sum, "the control did not round; the test proves nothing"


@pytest.mark.parametrize(
    ("left", "right", "expected_sum", "expected_difference"),
    [
        pytest.param("0", "0", "0", "0", id="zero and zero"),
        pytest.param("-0", "-0", "-0", "0", id="negative zeros"),
        pytest.param("-0", "0", "0", "-0", id="negative zero and zero"),
        pytest.param("1", "-1", "0", "2", id="a sum that cancels"),
        pytest.param("1.5", "1.5", "3.0", "0.0", id="a difference that cancels"),
    ],
)
def test_the_sign_of_a_zero_follows_decimal(
    left: str, right: str, expected_sum: str, expected_difference: str
) -> None:
    """`Decimal`'s own rule under half-even: a zero sum is negative only if both are.

    Compared by `str`, because `-0 == 0` and a value comparison would pass whichever sign
    came back.
    """
    assert str(add(Decimal(left), Decimal(right))) == expected_sum
    assert str(subtract(Decimal(left), Decimal(right))) == expected_difference


@pytest.mark.parametrize("operation", [add, subtract], ids=["add", "subtract"])
@pytest.mark.parametrize(
    ("left", "right", "raised"),
    [
        pytest.param(1.5, Decimal(2), TypeError, id="float"),
        pytest.param(Decimal(2), True, TypeError, id="bool"),
        pytest.param(Decimal(2), 2, TypeError, id="int"),
        pytest.param(Decimal("NaN"), Decimal(2), ValueError, id="nan"),
        pytest.param(Decimal(2), Decimal("-Infinity"), ValueError, id="infinity"),
    ],
)
def test_add_and_subtract_refuse_what_is_not_a_finite_decimal(
    operation: Callable[[Decimal, Decimal], Decimal],
    left: object,
    right: object,
    raised: type[Exception],
) -> None:
    with pytest.raises(raised):
        operation(left, right)  # type: ignore[arg-type]


def test_add_and_subtract_are_exact_past_the_int_str_digit_limit() -> None:
    """Two 5000-digit coefficients, with no `int(str)` conversion that would refuse them."""
    ones = Decimal("0." + "1" * OVERLONG_ONES)
    twos = Decimal("0." + "2" * OVERLONG_ONES)

    assert format(add(ones, twos), "f") == "0." + "3" * OVERLONG_ONES
    assert format(subtract(twos, ones), "f") == "0." + "1" * OVERLONG_ONES


# `st.decimals` with `places` draws values on a fixed grid, which is exactly the engine's
# domain: 18 places, and a range wide enough to cross the 38-digit precision.
EIGHTEEN_PLACE_AMOUNTS: Final = st.decimals(
    min_value=Decimal("-1E+30"),
    max_value=Decimal("1E+30"),
    places=18,
    allow_nan=False,
    allow_infinity=False,
)


@given(left=EIGHTEEN_PLACE_AMOUNTS, right=EIGHTEEN_PLACE_AMOUNTS)
def test_add_and_subtract_agree_with_exact_rationals(left: Decimal, right: Decimal) -> None:
    """Against `Fraction`, which cannot round: every digit of every result is right."""
    assert Fraction(add(left, right)) == Fraction(left) + Fraction(right)
    assert Fraction(subtract(left, right)) == Fraction(left) - Fraction(right)


# ---- divide -------------------------------------------------------------------------

#: A quotient whose digits past the 18th place read `4999...9` for thirty places and then
#: stop: `1.000000000000000001` and a remainder just under half a unit. Rounded once it is
#: `...001`. Rounded first to 38 significant digits, the tail becomes an exact `5`, a tie,
#: and half-even then rounds the odd `1` up to `...002` -- the double rounding the spec's
#: *Arithmetic* section forbids.
BELOW_A_TIE: Final = Decimal("1.000000000000000001" + "4" + "9" * 29)


@pytest.mark.parametrize("divisor", ["1", "7", "0.003"], ids=["by 1", "by 7", "by 0.003"])
def test_divide_rounds_once_where_rounding_twice_would_differ(divisor: str) -> None:
    """The case that separates one rounding from two, through three different divisors.

    The dividend is `BELOW_A_TIE * divisor`, computed exactly by `multiply`, so the true
    quotient is `BELOW_A_TIE` itself. The control rounds it the two-step way and shows the
    answers differ, so the assertion is about the rounding and not about an easy number.
    """
    divisor_value = Decimal(divisor)
    dividend = multiply(BELOW_A_TIE, divisor_value)

    quotient = divide(dividend, divisor_value, 18)

    assert quotient == Decimal("1.000000000000000001")
    assert quotient.as_tuple().exponent == -18
    money_context = decimal.Context(prec=MONEY_PRECISION)
    rounded_twice = quantize(money_context.divide(dividend, divisor_value), 18)
    assert rounded_twice == Decimal("1.000000000000000002"), "the control did not double-round"


@pytest.mark.parametrize(
    ("dividend", "divisor", "scale", "expected"),
    [
        # Ties, which only half-even disambiguates: to the even neighbour, up and down.
        pytest.param("0.000000000000000001", "2", 18, "0", id="0.5 units to 0"),
        pytest.param("0.000000000000000003", "2", 18, "0.000000000000000002", id="1.5 to 2"),
        pytest.param("0.000000000000000005", "2", 18, "0.000000000000000002", id="2.5 to 2"),
        pytest.param("0.000000000000000007", "2", 18, "0.000000000000000004", id="3.5 to 4"),
        pytest.param("-0.000000000000000005", "2", 18, "-0.000000000000000002", id="-2.5 to -2"),
        pytest.param("-0.000000000000000007", "2", 18, "-0.000000000000000004", id="-3.5 to -4"),
        pytest.param("1", "4", 1, "0.2", id="0.25 at one place to 0.2"),
        pytest.param("3", "4", 1, "0.8", id="0.75 at one place to 0.8"),
        # Not ties: the nearest neighbour, whichever way it lies.
        pytest.param("1", "3", 18, "0.333333333333333333", id="1 by 3"),
        pytest.param("2", "3", 18, "0.666666666666666667", id="2 by 3"),
        pytest.param("-2", "3", 18, "-0.666666666666666667", id="-2 by 3"),
        pytest.param("2", "-3", 18, "-0.666666666666666667", id="2 by -3"),
        # docs/accounting.md example 6: 0.666666666666666667 / 2 is a tie on an odd digit.
        pytest.param("0.666666666666666667", "2", 18, "0.333333333333333334", id="example 6"),
        # An exact quotient comes back padded to the scale.
        pytest.param("70000", "2", 18, "35000", id="exact"),
    ],
)
def test_divide_rounds_half_to_even_at_the_scale(
    dividend: str, divisor: str, scale: int, expected: str
) -> None:
    quotient = divide(Decimal(dividend), Decimal(divisor), scale)

    assert quotient == Decimal(expected)
    assert quotient.as_tuple().exponent == -scale


def test_divide_keeps_the_sign_of_a_quotient_too_small_to_show() -> None:
    """`-1E-30 / 1` at 18 places is a negative zero, as the docstring promises."""
    assert str(divide(Decimal("-1E-30"), Decimal(1), 18)) == "-0E-18"
    assert str(divide(Decimal("1E-30"), Decimal(1), 18)) == "0E-18"
    assert str(divide(Decimal(0), Decimal(7), 18)) == "0E-18"


@pytest.mark.parametrize("zero", ["0", "-0", "0E-18"])
def test_divide_refuses_a_zero_divisor(zero: str) -> None:
    """Refused as a `ZeroDivisionError`, which `decimal.DivisionByZero` is."""
    with pytest.raises(ZeroDivisionError):
        divide(Decimal(1), Decimal(zero), 18)


@pytest.mark.parametrize(
    "dividend",
    [
        pytest.param("100000000000000000000", id="21 integer digits"),
        pytest.param("99999999999999999999.9999999999999999995", id="rounds up to 21 digits"),
        pytest.param("1E+100000", id="an exponent no quotient here can hold"),
    ],
)
def test_divide_refuses_a_quotient_beyond_the_money_precision(dividend: str) -> None:
    """`InvalidOperation`, the type `quantize` raises, for more than 38 digits at 18 places."""
    with pytest.raises(decimal.InvalidOperation):
        divide(Decimal(dividend), Decimal(1), 18)


def test_divide_accepts_the_widest_quotient_that_fits() -> None:
    """Twenty integer digits and eighteen places is 38 digits, and fits."""
    widest = Decimal("99999999999999999999.999999999999999999")

    assert divide(widest, Decimal(1), 18) == widest


def test_divide_ignores_a_narrowed_ambient_context() -> None:
    """The spec's own hostile context, `prec=6, rounding=ROUND_UP`, changes nothing."""
    outside = divide(BELOW_A_TIE, Decimal(3), 18)
    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_UP
        inside = divide(BELOW_A_TIE, Decimal(3), 18)
        ambient = BELOW_A_TIE / Decimal(3)

    assert str(inside) == str(outside)
    assert outside == Decimal("0.333333333333333334")
    assert ambient != outside, "the control did not round; the test proves nothing"


@pytest.mark.parametrize(
    ("dividend", "divisor"),
    [
        pytest.param(Decimal("0." + "1" * OVERLONG_ONES), Decimal(3), id="long dividend"),
        pytest.param(Decimal(1), Decimal("0." + "3" * OVERLONG_ONES), id="long divisor"),
        pytest.param(
            Decimal("1." + "1" * OVERLONG_ONES),
            Decimal("0." + "7" * OVERLONG_ONES),
            id="both long",
        ),
        pytest.param(Decimal("1E+100000"), Decimal("3E+100000"), id="far exponents"),
    ],
)
def test_divide_is_exact_past_the_int_str_digit_limit(dividend: Decimal, divisor: Decimal) -> None:
    """Operands of 5000 digits, or exponents of 100000, and still one correct rounding."""
    expected = round_half_even(Fraction(dividend) / Fraction(divisor), 18)

    assert Fraction(divide(dividend, divisor, 18)) == expected


@pytest.mark.parametrize(
    ("scale", "raised"),
    [
        pytest.param(-1, ValueError, id="negative"),
        pytest.param(True, TypeError, id="bool"),
        pytest.param(Decimal(18), TypeError, id="a Decimal"),
    ],
)
def test_divide_refuses_a_scale_that_is_not_a_whole_number_of_places(
    scale: object, raised: type[Exception]
) -> None:
    with pytest.raises(raised):
        divide(Decimal(1), Decimal(3), scale)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("dividend", "divisor", "raised"),
    [
        pytest.param(1, Decimal(3), TypeError, id="int dividend"),
        pytest.param(Decimal(1), 0.5, TypeError, id="float divisor"),
        pytest.param(Decimal("NaN"), Decimal(3), ValueError, id="nan"),
        pytest.param(Decimal(1), Decimal("Infinity"), ValueError, id="infinity"),
    ],
)
def test_divide_refuses_what_is_not_a_finite_decimal(
    dividend: object, divisor: object, raised: type[Exception]
) -> None:
    with pytest.raises(raised):
        divide(dividend, divisor, 18)  # type: ignore[arg-type]


NONZERO_AMOUNTS: Final = EIGHTEEN_PLACE_AMOUNTS.filter(lambda value: not value.is_zero())
SMALL_AMOUNTS: Final = st.decimals(
    min_value=Decimal("-1E+6"),
    max_value=Decimal("1E+6"),
    places=18,
    allow_nan=False,
    allow_infinity=False,
)


@settings(max_examples=300)
@given(
    dividend=st.one_of(SMALL_AMOUNTS, EIGHTEEN_PLACE_AMOUNTS),
    divisor=NONZERO_AMOUNTS,
    scale=st.integers(min_value=0, max_value=18),
)
def test_divide_agrees_with_one_exact_half_even_rounding(
    dividend: Decimal, divisor: Decimal, scale: int
) -> None:
    """Over generated operands: the rational quotient rounded once, or a refusal.

    300 examples rather than the default 100: this is the one function the engine rounds
    through, each example costs microseconds, and a tie or a near-tie is the input worth
    finding. It refuses exactly when the rounded quotient needs more than 38 significant
    digits at `scale` places.
    """
    expected = round_half_even(Fraction(dividend) / Fraction(divisor), scale)
    if abs(expected) >= Fraction(10) ** (MONEY_PRECISION - scale):
        with pytest.raises(decimal.InvalidOperation):
            divide(dividend, divisor, scale)
        return

    quotient = divide(dividend, divisor, scale)

    assert Fraction(quotient) == expected
    assert quotient.as_tuple().exponent == -scale


@pytest.mark.parametrize(
    ("dividend", "divisor"),
    [
        pytest.param("1E+20", "1.1", id="gap of 20, quotient 9.09E+19"),
        pytest.param("1E+20", "9.999999999999999999", id="gap of 20, just over 1E+19"),
        pytest.param("5E+19", "0.6", id="gap of 20 through a fractional divisor"),
        pytest.param("-1E+20", "1.000000000000000001", id="gap of 20, negative"),
    ],
)
def test_divide_computes_a_quotient_that_fits_even_when_the_exponents_are_20_apart(
    dividend: str, divisor: str
) -> None:
    """The refusal read off the exponents must not refuse what fits.

    With the adjusted exponents 20 apart, the quotient lies between 10**19 and 10**21, so
    only the digits can say whether it fits in 20 integer digits. Each of these does, and
    must be computed, not refused. The mutation sweep found a pre-check one digit too early
    surviving every other test, because random operands rarely land here.
    """
    expected = round_half_even(Fraction(Decimal(dividend)) / Fraction(Decimal(divisor)), 18)
    assert abs(expected) < Fraction(10) ** 20

    quotient = divide(Decimal(dividend), Decimal(divisor), 18)

    assert Fraction(quotient) == expected


@pytest.mark.parametrize(
    ("dividend", "divisor", "scale"),
    [
        pytest.param("1E+38", "1.1", 0, id="scale 0: 38 digits fit"),
        pytest.param("1E+30", "1.1", 8, id="scale 8: 30 digits fit"),
    ],
)
def test_divide_computes_the_widest_quotients_at_other_scales(
    dividend: str, divisor: str, scale: int
) -> None:
    """The same edge at other scales: `MONEY_PRECISION - scale` integer digits fit."""
    expected = round_half_even(Fraction(Decimal(dividend)) / Fraction(Decimal(divisor)), scale)
    assert abs(expected) < Fraction(10) ** (MONEY_PRECISION - scale)

    assert Fraction(divide(Decimal(dividend), Decimal(divisor), scale)) == expected
