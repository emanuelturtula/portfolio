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


def _coefficient(digits: tuple[int, ...]) -> int:
    """A Decimal's coefficient digits as the integer they spell. Exact by construction.

    Through `int(Decimal)` rather than `int("".join(...))`: the string route is subject to
    the interpreter's 4300-digit conversion limit, and this one is not.
    """
    return int(Decimal((0, digits, 0)))


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
