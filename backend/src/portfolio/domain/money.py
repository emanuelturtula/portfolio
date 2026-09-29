"""The money rules, in one place: precision, rounding, and base-unit conversion.

A monetary value is a `decimal.Decimal` from the moment it is parsed to the moment it is
rendered. `float` is banned in this package, and in `services/` and `providers/`, because
IEEE-754 cannot represent `0.1`: a portfolio that adds a few thousand fills in binary
floating point reports a total that is wrong in the last places and never says so. An AST
test in `backend/tests/security/` enforces the ban.

Two things here are deliberate exceptions to "`domain` is pure". The module mutates the
decimal context at import, because the alternative -- threading a `decimal.Context`
through every call -- fails open the first time a caller forgets. And `portfolio.db`
imports this module so that `NumericText` rounds by the rule defined here; the layering
contract places `db` above `domain` for exactly that reason. `domain` itself still
imports nothing from the application.
"""

from __future__ import annotations

import decimal
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

MONEY_PRECISION: Final[int] = 38
"""Significant digits available to a monetary calculation.

Wide enough for the product's extremes at once: 18 decimals of an EVM-style token, a
total in the billions, and room left for intermediate products. The default of 28 is not.
"""

MONEY_ROUNDING: Final[str] = ROUND_HALF_EVEN
"""Banker's rounding, the only mode here that does not accumulate a bias.

`ROUND_HALF_UP` pushes every tie away from zero, so a long series of roundings drifts
upward -- which, over thousands of fills, is a portfolio that reports more than it holds.
"""


def configure_decimal_context() -> None:
    """Raise the decimal precision to `MONEY_PRECISION`, for this thread and new ones.

    `getcontext()` returns the *calling thread's* context, so setting it alone configures
    whichever thread called this and nothing else. That is not a theoretical concern
    here: `main.py` runs the Alembic migrations through `anyio.to_thread.run_sync`, and
    Starlette runs every non-async route handler in a worker thread too. A thread builds
    its context by copying `DefaultContext` the first time it calls `getcontext()`, so
    setting only one of the two leaves half the process quantizing at 28 digits with no
    error to show for it. Measured: with `getcontext()` alone a worker thread reports 28;
    with `DefaultContext` also set it reports 38.

    Two consequences worth stating rather than discovering. A thread that materialised
    its own context *before* this ran keeps the old precision -- which is why this is
    called at import below, while the application is still being built and no worker
    thread exists yet. And `DefaultContext` is the `decimal` module's own global, so this
    changes the default precision for everything in the process, not only for this
    package. That is the point -- a guarantee that only covers the code that remembered
    to ask for it is not a guarantee -- but it is a real side effect of importing a
    library module, and the honest place to record it is here.
    """
    decimal.getcontext().prec = MONEY_PRECISION
    decimal.DefaultContext.prec = MONEY_PRECISION


# Called at import, deliberately. The alternative -- threading a `decimal.Context`
# through every call, or requiring an explicit setup call -- fails open the first time a
# caller forgets, and a rounding that is silently wrong is the failure mode this whole
# module exists to prevent. Naming the operation rather than running two bare assignments
# here means the entry point can also call it explicitly, and that moving to
# explicit-only configuration would be a one-line change rather than a redesign.
configure_decimal_context()

# The context every rounding in this module is evaluated against, passed explicitly.
#
# `Decimal.quantize` otherwise uses the *calling thread's* ambient context, which makes
# the answer depend on state a caller can change: inside a `decimal.localcontext()` with
# `prec = 9`, `quantize(Decimal("1234567890.12345"), 2)` raises where the same call
# outside it succeeds. Depending on the import side effect above for correctness would
# also mean depending on nothing else having lowered the precision since. Passing this
# makes `configure_decimal_context()` a convenience for other code rather than something
# this module's own answers rest on.
_MONEY_CONTEXT: Final = decimal.Context(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING)


def require_amount(value: object, *, subject: str) -> Decimal:
    """Return `value` as a finite `Decimal`, or refuse it.

    Refusing happens *before* any conversion, which is the whole point: `Decimal(0.1)`
    succeeds and yields `0.1000000000000000055511151231257827021181583404541015625`, and
    by then the damage is committed and invisible. A `bool` is refused with everything
    else that is not a `Decimal`.

    `subject` names the caller in the message, so a rejected write says which column or
    conversion refused it.

    Raises:
        TypeError: `value` is not a `Decimal`.
        ValueError: `value` is a NaN or an infinity, neither of which is an amount.
    """
    if not isinstance(value, Decimal):
        message = f"{subject} requires a Decimal, got {type(value).__name__}"
        raise TypeError(message)
    if not value.is_finite():
        message = f"{subject} cannot represent {value}"
        raise ValueError(message)
    return value


def quantize(value: Decimal, scale: int) -> Decimal:
    """Round `value` to exactly `scale` decimal places, half to even.

    This is the single definition of how money rounds. `NumericText` calls it on the way
    into the database rather than carrying its own copy, so a change to `MONEY_ROUNDING`
    cannot apply to half the system.

    A value needing more than `MONEY_PRECISION` significant digits after rounding raises
    `decimal.InvalidOperation`, which is the correct outcome: it is a number this
    application has decided it does not represent. At `scale` decimal places that leaves
    `MONEY_PRECISION - scale` digits for the integer part.

    The bound is `MONEY_PRECISION` exactly, and not whatever the calling thread's context
    happens to say, because the context is passed explicitly. A caller inside a
    `decimal.localcontext()` gets the same answer as one outside it.
    """
    # No `rounding=` argument here. An explicit one takes precedence over the context's,
    # which left the mode spelled twice one line apart with the context's copy never
    # evaluated -- so a later refactor dropping the argument "because the context already
    # says so" would have been trusting a value no test had ever read. `_MONEY_CONTEXT` is
    # now the single source for both precision and rounding, and changing `MONEY_ROUNDING`
    # fails thirteen tests rather than none.
    return value.quantize(_exponent(scale), context=_MONEY_CONTEXT)


def multiply(left: Decimal, right: Decimal) -> Decimal:
    """The exact product of two amounts. Never rounded, whatever the ambient context says.

    **Exact, and unbounded, on purpose.** The result carries every digit of the product --
    as many significant digits as the two coefficients have between them -- so that
    `quantize` stays the one place money is rounded. A caller that needs the product at a
    scale quantizes it, and `quantize` is where the `MONEY_PRECISION` ceiling is enforced:
    a product with more integer digits than the scale leaves room for raises
    `decimal.InvalidOperation` there, which is the too-many-integer-digits refusal of
    whatever the product is about to become.

    Two alternatives were rejected, and each is a real off-by-one rather than a style
    preference:

    * **`left * right`** evaluates in the calling thread's context. At the interpreter's
      default precision of 28 -- or inside anyone's `decimal.localcontext()` -- a product
      of two 18-place amounts is rounded before it ever reaches `quantize`, silently.
    * **Multiplying under a 38-digit context** fixes the thread dependence and still rounds
      any product longer than 38 significant digits. Quantizing that result to a scale is a
      *second* half-even rounding, and two half-even roundings in a row are not one: a
      product whose digits past the scale read `4999...95` is below the halfway point and
      should round down, but the first step rounds it up to an exact tie, and the second
      then rounds that tie to even -- up, one unit off, whenever the last kept digit is
      odd.

    Built from the coefficients as integers, the way `from_base_units` assembles its
    result, so neither the context's precision nor its rounding mode can come between the
    operands and the answer. The sign follows the usual rule, including for a zero: `-0`
    times a positive amount is `-0`, which `quantize` and `NumericText` normalise.

    **No step converts between `int` and `str`**, and that is what makes this safe on any
    operand rather than on the ones a caller happened to bound. CPython refuses to convert
    an integer of more than 4300 digits to or from a string (`sys.get_int_max_str_digits`),
    with a bare `ValueError`, and the first version of this function did both -- so an
    amount a venue sent with five thousand digits escaped as an untyped error from the
    middle of a derivation. `int(Decimal)` and `Decimal(int)` convert the binary
    representations directly and are not subject to that limit; measured, a product of two
    5001-digit coefficients round-trips through both.

    Raises:
        TypeError: either operand is not a `Decimal` (a `bool` or a `float` included).
        ValueError: either operand is a NaN or an infinity.
        decimal.InvalidOperation: the product's exponent lies outside the range `Decimal`
            can represent at all -- the same type `quantize` raises for a result it cannot
            hold, so a caller that quantizes the product catches one type for both.
    """
    require_amount(left, subject="multiply")
    require_amount(right, subject="multiply")
    left_sign, left_digits, left_exponent = left.as_tuple()
    right_sign, right_digits, right_exponent = right.as_tuple()
    product = _coefficient(left_digits) * _coefficient(right_digits)
    # Both exponents are `int` on a finite Decimal; the string forms belong to NaN and
    # infinity, which `require_amount` has already refused. `Decimal(product)` is exact
    # whatever the context says: construction from an `int` never rounds.
    return Decimal(
        (
            left_sign ^ right_sign,
            Decimal(product).as_tuple().digits,
            int(left_exponent) + int(right_exponent),
        )
    )


def add(left: Decimal, right: Decimal) -> Decimal:
    """The exact sum of two amounts. Never rounded, whatever the ambient context says.

    `multiply`'s reasoning, applied to a sum. **`left + right`** evaluates in the calling
    thread's context: at the interpreter's default of 28 digits, or inside anyone's
    `decimal.localcontext()`, a sum of a large basis and an 18-place fee is rounded before
    anybody asked for a rounding, silently. **Adding under a 38-digit context** fixes the
    thread dependence and still rounds any sum longer than 38 digits, and whatever rounds
    it next is then a second rounding. So the operands are aligned to the smaller exponent
    and summed as integers, and the result carries every digit.

    **The shape of the answer is `Decimal`'s own**, so this is a drop-in for an exact `+`:
    the exponent is the smaller of the two, and a zero result is negative only when both
    operands are (`-0 + -0` is `-0`; `1 + -1` is `0`), which is the rule `Decimal` applies
    under every rounding mode but `ROUND_FLOOR`.

    **Built from integers like `multiply`, and with the same care**: `int(Decimal)` and
    `Decimal(int)` rather than any `str` round trip, which the interpreter refuses beyond
    4300 digits, and the alignment spelled as a `Decimal` exponent rather than as `10**k`,
    which typeshed types as `Any`. **The work grows with the gap between the exponents**,
    because an exact sum of `1E+100000` and `1E-100000` has two hundred thousand digits and
    converting them is quadratic -- measured at about a fifth of a second for a gap of a
    hundred thousand places. Every amount the accounting engine adds has exactly eighteen
    places, so its gaps are zero; a caller summing amounts from anywhere else should bound
    their exponents first, as `NormalizedFill` does.

    Raises:
        TypeError: either operand is not a `Decimal` (a `bool` or a `float` included).
        ValueError: either operand is a NaN or an infinity.
    """
    require_amount(left, subject="add")
    require_amount(right, subject="add")
    return _exact_sum(left, right)


def subtract(left: Decimal, right: Decimal) -> Decimal:
    """The exact difference `left - right`. Never rounded, whatever the ambient context says.

    `add` of `right` negated, and negated with `copy_negate`, which only flips the sign bit:
    unary `-right` is an arithmetic operation in `decimal`, and it rounds `right` to the
    calling thread's precision on the way. Everything `add` says about exactness, the shape
    of the result and the cost of a wide exponent gap holds here too, and so does the sign of
    a zero: `x - x` is `0`, and `-0 - 0` is `-0`.

    Raises:
        TypeError: either operand is not a `Decimal` (a `bool` or a `float` included).
        ValueError: either operand is a NaN or an infinity.
    """
    require_amount(left, subject="subtract")
    require_amount(right, subject="subtract")
    return _exact_sum(left, right.copy_negate())


def divide(dividend: Decimal, divisor: Decimal, scale: int) -> Decimal:
    """`dividend / divisor`, rounded **once**, by `MONEY_ROUNDING`, to exactly `scale` places.

    The one arithmetic operation here that has to round, since a quotient such as `1 / 3`
    has no finite expansion. Two things make it round exactly once.

    **The quotient is never approximated before the rounding.** Integer division yields the
    quotient truncated one place past `scale`, and a remainder. Where the remainder is not
    zero, a trailing `1` -- a sticky digit -- is appended two places past `scale`. That
    stand-in sits strictly between the truncation and the next value at that place, and so
    does the true quotient, and no rounding boundary or midpoint at `scale` places lies in
    that open interval: every rounding mode treats the two identically. A stand-in with no
    sticky digit is the quotient itself. The alternative -- `dividend / divisor` in some
    context, then `quantize` -- is two roundings, and `multiply` explains how two half-even
    roundings in a row put the last digit one unit off.

    **The rounding itself is `quantize`'s**, applied to that stand-in, so this function
    carries no copy of the rounding rule to drift from the one the rest of the system uses:
    change `MONEY_ROUNDING` and this changes with it. That is also where the ceiling comes
    from. A quotient needing more than `MONEY_PRECISION` significant digits at `scale`
    places raises `decimal.InvalidOperation`, the type `quantize` raises, and so does a
    `scale` finer than the context's exponent range allows. The ambient context plays no
    part: `quantize` passes `_MONEY_CONTEXT` explicitly.

    **The integers are bounded by the operands, not by their exponents.** An operand is a
    `Decimal`, so `1E+999999` is a legal one, and aligning it naively would build a
    million-digit integer and convert it -- quadratic, and measured at seconds for a few
    hundred thousand digits. So the quotient's order of magnitude is read off the adjusted
    exponents first. A quotient certain to exceed the ceiling is refused without being
    computed, and one certain to be under a tenth of a unit at `scale` places is replaced by
    a sticky digit alone -- which rounds as it would, so the rounding mode is still
    `quantize`'s to apply. What remains needs integers of no more digits than the two
    coefficients hold between them, plus the forty or so the ceiling allows.

    The sign follows the usual rule, including for a zero quotient: `-1 / 3` at a scale too
    coarse to show it is `-0`, which `NumericText` normalises.

    Raises:
        TypeError: `dividend` or `divisor` is not a `Decimal` (a `bool` or a `float`
            included), or `scale` is not an `int` (a `bool` included).
        ValueError: `dividend` or `divisor` is a NaN or an infinity, or `scale` is negative.
        decimal.DivisionByZero: `divisor` is zero. The type `decimal` itself raises for a
            division by zero, and a subclass of `ZeroDivisionError`, so a caller catching
            either catches this.
        decimal.InvalidOperation: the rounded quotient needs more than `MONEY_PRECISION`
            significant digits.
    """
    require_amount(dividend, subject="divide")
    require_amount(divisor, subject="divide")
    _require_decimals(scale, subject="divide")
    if divisor.is_zero():
        message = "divide refuses a zero divisor"
        raise decimal.DivisionByZero(message)
    dividend_sign, dividend_digits, dividend_exponent = dividend.as_tuple()
    divisor_sign, divisor_digits, divisor_exponent = divisor.as_tuple()
    sign = dividend_sign ^ divisor_sign
    if dividend.is_zero():
        return quantize(Decimal((sign, (0,), 0)), scale)
    # 10**m <= |dividend / divisor| * 10**(1 - (adjusted gap)) ... in plain words: with
    # `magnitude = dividend.adjusted() - divisor.adjusted()`, the quotient lies strictly
    # between 10**(magnitude - 1) and 10**(magnitude + 1). `adjusted()` reads the exponent
    # and the digit count; it involves no context and no arithmetic on the value.
    magnitude = dividend.adjusted() - divisor.adjusted()
    if magnitude + scale - 1 >= MONEY_PRECISION:
        # At `scale` places the quotient is above 10**MONEY_PRECISION units, so its rounded
        # coefficient has more digits than the context holds, under any rounding mode.
        message = (
            f"divide cannot represent a quotient of more than {MONEY_PRECISION} "
            f"significant digits at {scale} decimal places"
        )
        raise decimal.InvalidOperation(message)
    if magnitude + scale + 2 <= 0:
        # Below a tenth of a unit at `scale` places. A lone sticky digit two places past
        # `scale` stands in for it: truncated one place past `scale` it is zero with a
        # non-zero remainder, exactly as the quotient is.
        return quantize(Decimal((sign, (1,), -(scale + 2))), scale)
    # One place past `scale`: the guard digit the sticky digit is appended after.
    shift = int(dividend_exponent) - int(divisor_exponent) + scale + 1
    numerator = _scaled_coefficient(dividend_digits, max(shift, 0))
    denominator = _scaled_coefficient(divisor_digits, max(-shift, 0))
    truncated, remainder = divmod(numerator, denominator)
    digits = Decimal(truncated).as_tuple().digits
    if remainder:
        return quantize(Decimal((sign, (*digits, 1), -(scale + 2))), scale)
    return quantize(Decimal((sign, digits, -(scale + 1))), scale)


def _exact_sum(left: Decimal, right: Decimal) -> Decimal:
    """The exact sum of two finite amounts, aligned to the smaller exponent. See `add`."""
    left_sign, left_digits, left_exponent = left.as_tuple()
    right_sign, right_digits, right_exponent = right.as_tuple()
    # Both exponents are `int` on a finite Decimal; `require_amount` has refused the rest.
    exponent = min(int(left_exponent), int(right_exponent))
    left_value = _scaled_coefficient(left_digits, int(left_exponent) - exponent)
    right_value = _scaled_coefficient(right_digits, int(right_exponent) - exponent)
    total = (-left_value if left_sign else left_value) + (
        -right_value if right_sign else right_value
    )
    if total == 0:
        return Decimal((left_sign & right_sign, (0,), exponent))
    return Decimal((int(total < 0), Decimal(abs(total)).as_tuple().digits, exponent))


def _coefficient(digits: tuple[int, ...]) -> int:
    """A Decimal's coefficient digits as the integer they spell. Exact by construction.

    Through `int(Decimal)` rather than `int("".join(...))`: the string route is subject to
    the interpreter's 4300-digit conversion limit, and this one is not.
    """
    return int(Decimal((0, digits, 0)))


def _scaled_coefficient(digits: tuple[int, ...], shift: int) -> int:
    """The integer `digits` spell, times ten to the `shift`. `shift` is not negative.

    `_coefficient` with the power of ten carried as the exponent of the `Decimal` being
    converted, which `int()` resolves exactly -- a `Decimal` with a non-negative exponent is
    already an integer, and converting one involves no rounding and no context. It avoids
    `10**shift`, which typeshed types as `Any`, for the reason `to_base_units` gives.
    """
    return int(Decimal((0, digits, shift)))


def to_base_units(amount: Decimal, decimals: int) -> int:
    """Convert a decimal amount into the integer base units a chain counts in.

    `decimals` is the exponent recorded on `assets.decimals`: 8 means one BTC is
    100_000_000 satoshis. An amount carrying more precision than `decimals` can hold is
    refused rather than rounded -- a balance read from a chain is exact, and rounding one
    here would mean reporting a holding the chain does not agree with.

    A negative zero converts to `0`: integers have no signed zero, so the conversion is
    exact in value but does not preserve the sign of a zero amount.

    Raises:
        TypeError: `amount` is not a `Decimal`, or `decimals` is not an `int`.
        ValueError: `amount` is not finite, `decimals` is negative, or `amount` is finer
            than `decimals` can express.
    """
    require_amount(amount, subject="to_base_units")
    _require_decimals(decimals, subject="to_base_units")
    sign, digits, exponent = amount.as_tuple()
    # `exponent` is an `int` on every finite Decimal; its string forms belong to NaN and
    # infinity, which `require_amount` has already rejected.
    shift = int(exponent) + decimals
    if shift < 0:
        message = (
            f"{amount} carries more than {decimals} decimal places "
            f"and cannot be expressed in base units"
        )
        raise ValueError(message)
    # The coefficient's digits with `shift` zeros appended *is* the base-unit integer.
    # Written this way rather than as `coefficient * 10**shift` because typeshed types
    # `int.__pow__` as returning `Any` -- a negative exponent produces a float -- and an
    # `Any` in the middle of the one conversion this module exists for is exactly the
    # hole that lets a float back in.
    units = int("".join(str(digit) for digit in digits) + "0" * shift)
    return -units if sign else units


def from_base_units(units: int, decimals: int) -> Decimal:
    """Convert integer base units back into a decimal amount.

    The result is assembled from its parts rather than divided into existence, so neither
    rounding nor the context precision can come between the stored integer and the value
    returned.

    Guarded on the way in for the same reason `to_base_units` is: this is the boundary
    where an integer read from a chain API or a raw row becomes an amount, and an
    unguarded `bool` here is not a type error but a wrong balance -- `True` would convert
    to one base unit and be reported as a holding.

    Raises:
        TypeError: `units` or `decimals` is not an `int`, or either is a `bool`.
        ValueError: `decimals` is negative.
    """
    if isinstance(units, bool) or not isinstance(units, int):
        message = f"from_base_units requires an int, got {type(units).__name__}"
        raise TypeError(message)
    _require_decimals(decimals, subject="from_base_units")
    digits = tuple(int(character) for character in str(abs(units)))
    return Decimal((1 if units < 0 else 0, digits, -decimals))


def _require_decimals(decimals: object, *, subject: str) -> None:
    """Refuse an exponent that is not a whole, non-negative number of places.

    A negative `decimals` is its own rejection rather than a strange-sounding
    over-precision message: `to_base_units(Decimal("1"), -2)` used to complain that `1`
    "carries more than -2 decimal places", which is not a sentence and sends the reader
    looking at the amount instead of at the exponent they got wrong.
    """
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        message = f"{subject} requires an int number of decimals, got {type(decimals).__name__}"
        raise TypeError(message)
    if decimals < 0:
        message = f"{subject} requires a non-negative number of decimals, got {decimals}"
        raise ValueError(message)


def _exponent(scale: int) -> Decimal:
    """`Decimal("1E-scale")`, the unit `Decimal.quantize` rounds against.

    Built from its parts: `Decimal(1).scaleb(-scale)` would run through the decimal
    context, and the point of this module is that nothing about money depends on ambient
    state that a caller can change.
    """
    return Decimal((0, (1,), -scale))
