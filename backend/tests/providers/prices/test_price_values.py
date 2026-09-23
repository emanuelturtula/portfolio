"""`require_price`: the one boundary four vendors' prices go through.

Four parsers, one definition of what counts as a price. That is the whole point of the
function -- without it, Kraken's opinion about a zero and CoinGecko's opinion about a
string would be two different opinions, and the one that was wrong would be the one nobody
had read.

## The two shapes it accepts, and why the split is not symmetric

A **JSON string** arrives as a `str`: Kraken's `c[0]` and Coinbase's `data.amount` are both
strings, measured. A **JSON number** arrives as a `Decimal`, because `decode_json` passes
`parse_float=Decimal` -- built from the literal text the vendor sent rather than from the
nearest double. That conversion happens *before* this function runs, which is the reason
the hook lives in the shared decoder: a parser cannot repair a value a parser has already
damaged.

So `require_price` never has to decide what to do with a `float`. It refuses one anyway,
because "cannot arrive" is a property of today's call sites rather than of the function.

## What it refuses, and the one that matters

**Zero.** A stored price of zero values every holding of that asset at nothing, and the
total computed from it is complete, confident and wrong -- which is the sentence criterion
3 is quoted for. A refusal leaves the pair unanswered and turns it into a reason instead.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest

from portfolio.providers.base import decode_json
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.prices.base import require_price

VENDOR: Final = "A Vendor"


# --------------------------------------------------------------------------------------
# What it accepts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("0.04228645", id="a sub-cent price"),
        pytest.param("86000.10000", id="trailing zeros the vendor sent"),
        pytest.param("0.1", id="the value IEEE-754 cannot represent"),
        pytest.param("1e-8", id="exponent form"),
        pytest.param("0.000000000000000000001", id="finer than the column's own scale"),
    ],
)
def test_a_string_price_becomes_the_decimal_those_characters_spell(digits: str) -> None:
    """The shape Kraken and Coinbase send, carried through without a conversion in between.

    `str()` as well as `==`, because equality on a `Decimal` ignores a trailing zero and
    `86000.10000` versus `86000.1` is exactly the difference a `float` round trip makes --
    the same number, a different string, and the string is what the column stores.

    The last row is deliberately finer than `PRICE_SCALE`: rounding is `NumericText`'s job
    and a parser that rounded early would make the column's declared scale a fiction.
    """
    amount = require_price(digits, source=VENDOR)

    assert amount == Decimal(digits)
    assert str(amount) == str(Decimal(digits))


def test_a_decimal_from_a_json_number_passes_through_unchanged() -> None:
    """The shape the Kaspa endpoint and CoinGecko send, after `decode_json` has built it.

    Driven through the real decoder rather than by constructing a `Decimal` here, so the
    two halves of the float fix are exercised as the one path they actually form: the hook
    produces the `Decimal`, and this function accepts it without touching it.
    """
    decoded = decode_json('{"price": 0.04228645}')
    assert isinstance(decoded, dict)

    amount = require_price(decoded["price"], source=VENDOR)

    assert amount == Decimal("0.04228645")
    assert str(amount) == "0.04228645"


def test_a_whole_integer_is_accepted_the_way_the_money_column_accepts_one() -> None:
    """An `int` has nothing after the point to lose, so it is exact by construction.

    `NumericText` makes the same exception for the same reason, and the two agreeing is
    what stops a vendor that renders a round price as `86000` being refused at one boundary
    and stored at the other.
    """
    assert require_price(86000, source=VENDOR) == Decimal(86000)


# --------------------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("0", id="zero as a string"),
        pytest.param(Decimal(0), id="zero as a Decimal"),
        pytest.param(0, id="zero as an int"),
        pytest.param("0.00000000", id="zero with a scale"),
        pytest.param("-1.5", id="negative as a string"),
        pytest.param(Decimal("-0.00000001"), id="negative and tiny"),
    ],
)
def test_a_price_that_is_not_greater_than_zero_is_refused(value: object) -> None:
    """Criterion 3 at the parser: a zero is worse than an absence, because it is believed.

    Every spelling of zero the three input shapes can produce, because the guard is one
    comparison and a value that reached it as a string would otherwise be compared as text.
    The negative rows are here because a mis-signed price is a holding that subtracts from
    a portfolio total.
    """
    with pytest.raises(ProviderResponseError, match=r"(?i)greater than zero"):
        require_price(value, source=VENDOR)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("NaN", id="NaN"),
        pytest.param("Infinity", id="Infinity"),
        pytest.param("-Infinity", id="-Infinity"),
        pytest.param("nan", id="lower-case nan, which Decimal also accepts"),
    ],
)
def test_a_non_finite_value_sent_as_a_string_is_refused(token: str) -> None:
    """The arm a JSON **string** can still reach, now that the bare tokens are refused.

    `decode_json` turns `{"price": NaN}` into a refusal before this function sees it. But
    `{"price": "NaN"}` is perfectly valid JSON and arrives as an ordinary `str` -- and a
    string is the shape Kraken and Coinbase use for **every** price they send, so this is
    the realistic route rather than a contrived one.

    `Decimal("NaN")` constructs happily, which is what makes the check necessary: a NaN in
    a money column compares false against itself forever, including against the row it was
    read from, so a total built from one is a number no query can ever reconcile.
    """
    with pytest.raises(ProviderResponseError, match=r"(?i)finite"):
        require_price(token, source=VENDOR)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="null, which is what a missing field decodes to"),
        pytest.param(True, id="a bool, which is an int subclass"),
        pytest.param(False, id="a false bool, which is also a zero"),
        pytest.param([1], id="a list"),
        pytest.param({"amount": 1}, id="an object"),
        pytest.param(1.5, id="a float, which cannot arrive but is refused anyway"),
    ],
)
def test_a_value_that_is_not_a_number_at_all_is_refused_by_type(value: object) -> None:
    """The type guard, and two rows in it are the ones a plausible implementation misses.

    `True` is an `int` subclass, so `isinstance(value, int)` accepts it and it would be
    stored as a price of one. `False` is worse: it is an `int` *and* a zero, so a guard
    ordered the other way round would refuse it for the wrong reason and the type hole
    would stay open.

    The `float` row cannot arrive through `decode_json` today. It is refused because
    "cannot arrive" is a fact about the current call sites rather than about this function,
    and a parser that built one some other way is exactly the thing rule 2 exists to stop.
    """
    with pytest.raises(ProviderResponseError):
        require_price(value, source=VENDOR)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("1,234.5", id="a localised thousands separator"),
        pytest.param("", id="an empty string"),
        pytest.param("   ", id="whitespace"),
        pytest.param("86000.10 USD", id="a price with its currency attached"),
        pytest.param("$86000.10", id="a price with a symbol"),
    ],
)
def test_a_string_that_is_not_a_number_is_a_typed_refusal(value: str) -> None:
    """`Decimal(str)` raises `InvalidOperation`, which is the arm nobody writes down.

    It is not a `ValueError` subclass in the way a reader expects, so a parser catching
    `ValueError` around this would let it escape untyped -- into `fetch_prices`, which
    catches `ProviderError` and nothing else, ending a whole refresh rather than moving to
    the next source.
    """
    with pytest.raises(ProviderResponseError):
        require_price(value, source=VENDOR)


# --------------------------------------------------------------------------------------
# What the refusals say, and what they must not
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("0", id="zero"),
        pytest.param("NaN", id="not finite"),
        pytest.param(["a-distinctive-list-entry"], id="the wrong type"),
        pytest.param("1,234.5", id="an unparseable string"),
    ],
)
def test_every_refusal_names_the_source_and_never_the_value(value: object) -> None:
    """The vendor is what an operator can act on; the value is a response body.

    A price is public market data, so quoting one is not itself a disclosure -- but the
    same habit at the next boundary is what puts an address in a log, and this package's
    rule is that a parser error never shows the text it failed on. Asserted over all four
    arms, because each builds its own message.

    The wrong-type row is a list carrying a deliberately distinctive string rather than
    `None`: `str(None)` is `"None"`, which is a substring of the type name `NoneType` that
    the message correctly *does* carry, so the assertion would have failed for a message
    that is exactly right. Checking a short or common value against a sentence is how an
    assertion like this passes -- or fails -- by accident.
    """
    with pytest.raises(ProviderResponseError) as caught:
        require_price(value, source=VENDOR)

    message = str(caught.value)

    assert VENDOR in message
    assert str(value) not in message


def test_the_type_refusal_names_the_type_so_a_reader_knows_what_arrived() -> None:
    """Naming the type is the one detail about the value that is safe and useful.

    "The vendor sent something that is not a number" sends somebody to read a body by hand.
    "The vendor sent a NoneType" says the field was absent, which is a different fix.
    """
    with pytest.raises(ProviderResponseError) as caught:
        require_price(None, source=VENDOR)

    assert "NoneType" in str(caught.value)
