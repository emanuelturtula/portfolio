"""The wire representation of money: a JSON string, never a JSON number.

JSON has one numeric type and every mainstream parser reads it into an IEEE-754 double,
so `{"amount": 0.1}` is already inexact before any application code runs. A string
crosses the wire intact, and the frontend turns it into a `decimal.js` value -- which is
what the `no-restricted-globals` rule in `frontend/eslint.config.js` assumes when it bans
`parseFloat`, `parseInt` and `Number()` on money.

`WithJsonSchema` is what carries that contract across the boundary: it pins `type:
"string"` in the OpenAPI document, so the generated TypeScript declares a `string` and a
frontend that treats money as a number fails to compile rather than at runtime.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Final

from pydantic import AfterValidator, BeforeValidator, PlainSerializer, WithJsonSchema

from portfolio.domain.money import MONEY_PRECISION

MAX_WIRE_EXPONENT: Final = MONEY_PRECISION
"""How far from the decimal point an amount may sit, in either direction.

Derived from `MONEY_PRECISION` rather than picked, so that anything a money column can
store is something the wire can carry. A separate number here would eventually differ
from that one, and the difference would show up as a value that persists fine and then
fails to serialize.
"""


def _refuse_a_lossy_number(value: object) -> object:
    """Refuse an input that floating point has already damaged.

    Accepting a JSON number would launder the error instead of reporting it: by the time
    this validator runs, `0.1` is `0.1000000000000000055511151231257827021181583404541`
    and no amount of care afterwards recovers the digits the client meant.

    Everything else is handed to Pydantic's own `Decimal` validation, which accepts a
    string or an integer, and rejects a `bool` and a malformed string with the ordinary
    422 rather than an exception from here.
    """
    if isinstance(value, float | bool):
        message = (
            f"a monetary value must arrive as a JSON string, not as "
            f"{type(value).__name__}: a JSON number is parsed into an IEEE-754 double "
            f"and is already inexact by the time it is validated"
        )
        raise ValueError(message)
    return value


def _refuse_an_unrenderable_magnitude(value: Decimal) -> Decimal:
    """Refuse an amount whose exponent makes rendering it the denial of service.

    `format(value, "f")` writes out every position between the digits and the point, so
    its cost is linear in the exponent while the input stays tiny. Measured:
    `"1E+1000000"` is 10 bytes in and renders a 1,000,014-character string in about 2 ms,
    so `"1E+1000000000"` -- 13 bytes -- renders on the order of a gigabyte. On a
    Raspberry Pi 5 that is an out-of-memory kill from one request body.

    The renderer is the amplifier, not the parser: `Decimal("1E+1000000000")` itself is
    cheap and small. Anyone tempted to relax this bound should note that the cost is
    proportional to the *output*, which the client chooses and does not pay for.

    The database side is already safe, because `quantize` raises before `format` is
    reached. This is the same guarantee for the path that has no column behind it.
    """
    exponent = value.as_tuple().exponent
    # `exponent` is an `int` on every finite Decimal, and non-finite values were rejected
    # by Pydantic's own `Decimal` validation before this serializer can run.
    if abs(int(exponent)) > MAX_WIRE_EXPONENT or abs(value.adjusted()) > MAX_WIRE_EXPONENT:
        message = (
            f"a monetary value must have an exponent within "
            f"{MAX_WIRE_EXPONENT} places of the decimal point"
        )
        raise ValueError(message)
    return value


def _as_fixed_point(value: Decimal) -> str:
    """Render an amount for the wire, always in positional notation.

    `str(Decimal("1E+2"))` is `"1E+2"`, which is a correct decimal string and a hostile
    thing to hand a client.

    Negative zero is normalised here for the same reason `NumericText` normalises it on
    the way into the database: a computed loss of four tenths of a cent quantized to two
    places is `Decimal("-0.00")`, and `-0.00` is not something to show a user. Without
    this the two layers disagree -- the column stores `0.00` and the wire says `-0.00`
    for the same amount.
    """
    if value.is_zero():
        value = abs(value)
    return format(value, "f")


MoneyStr = Annotated[
    Decimal,
    BeforeValidator(_refuse_a_lossy_number),
    AfterValidator(_refuse_an_unrenderable_magnitude),
    PlainSerializer(_as_fixed_point, return_type=str),
    WithJsonSchema({"type": "string", "examples": ["1234.56789012"]}),
]
"""A `Decimal` field that validates from a JSON string and serializes back to one."""
