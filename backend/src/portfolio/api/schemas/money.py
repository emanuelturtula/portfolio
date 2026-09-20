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
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema


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


def _as_fixed_point(value: Decimal) -> str:
    """Render an amount for the wire, always in positional notation.

    `str(Decimal("1E+2"))` is `"1E+2"`, which is a correct decimal string and a hostile
    thing to hand a client.
    """
    return format(value, "f")


MoneyStr = Annotated[
    Decimal,
    BeforeValidator(_refuse_a_lossy_number),
    PlainSerializer(_as_fixed_point, return_type=str),
    WithJsonSchema({"type": "string", "examples": ["1234.56789012"]}),
]
"""A `Decimal` field that validates from a JSON string and serializes back to one."""
