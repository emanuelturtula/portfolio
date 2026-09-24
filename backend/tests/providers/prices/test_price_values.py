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

from portfolio.db.models import PRICE_SCALE
from portfolio.domain.money import MONEY_PRECISION
from portfolio.providers.base import decode_json
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.prices.base import MAX_PRICE_INTEGER_DIGITS, require_price

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
        pytest.param("0.0422864500004", id="finer than the column's scale, and surviving it"),
    ],
)
def test_a_string_price_becomes_the_decimal_those_characters_spell(digits: str) -> None:
    """The shape Kraken and Coinbase send, carried through without a conversion in between.

    `str()` as well as `==`, because equality on a `Decimal` ignores a trailing zero and
    `86000.10000` versus `86000.1` is exactly the difference a `float` round trip makes --
    the same number, a different string, and the string is what the column stores.

    The last row is finer than `PRICE_SCALE` and **still leaves something** when rounded to
    it: this function does not round, because rounding is `NumericText`'s job and a parser
    that rounded early would make the column's declared scale a fiction. A value fine
    enough to round away to *nothing* is a different case and is refused; see
    `test_a_price_too_small_for_the_column_to_hold_is_refused`. That row used to live here,
    asserted as a pass-through, which was this module agreeing that the column may destroy
    an amount.
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


# --------------------------------------------------------------------------------------
# A price too large to store is a vendor error, not a database error
# --------------------------------------------------------------------------------------
#
# Kraken and Coinbase send prices as **strings**, so `"1e300"` is a well-formed response as
# far as every layer above this function is concerned. Without a bound here the quote is
# built, survives the service, and dies inside `flush()` -- where `NumericText` raises and
# SQLAlchemy wraps it in a `StatementError`.
#
# Measured before the bound existed: `Decimal("1E+300")` beside three good pairs raised
# `sqlalchemy.exc.StatementError` out of `refresh_prices` and left **zero** rows written.
# Three separate things were wrong and none of them was the bad price: a `sqlalchemy`
# exception escaped into a layer the contract forbids from importing it, an operator got a
# traceback from a CLI command, and every pair already fetched in that refresh was thrown
# away -- the one outcome `fetch_prices`' own docstring promises is impossible.
#
# Refused here it is one more thing a vendor can get wrong: the loop passes the source over,
# the other pairs are kept, and this one is answered by somebody else or becomes a reason.


def test_the_integer_bound_is_derived_from_the_column_and_not_invented() -> None:
    """26 digits, and it is `MONEY_PRECISION - PRICE_SCALE` rather than a round number.

    A price this application cannot *store* is not one it should *accept*, so the bound has
    to be the column's. A plausible-looking literal here would be a third number free to
    drift from the two it is supposed to agree with -- and it would drift in the direction
    that matters, accepting a value the database then refuses.

    The arithmetic is written out as well as the name, because "derived" is a claim about
    the source and 26 is what a reader needs to check against `PRICE_SCALE`.
    """
    assert MAX_PRICE_INTEGER_DIGITS == MONEY_PRECISION - PRICE_SCALE
    assert MAX_PRICE_INTEGER_DIGITS == 26


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("1E+300", id="the measured value, in exponent form"),
        pytest.param("1" + "0" * 26, id="one digit over the bound, written out"),
        pytest.param("9" * 27, id="twenty-seven nines"),
        pytest.param("1e400", id="beyond what a double could even hold"),
    ],
)
def test_a_price_with_more_integer_digits_than_the_column_holds_is_refused(digits: str) -> None:
    """A `ProviderResponseError`, so the failover loop already knows what to do with it.

    No new vocabulary and no new report line: the path for "this vendor sent something that
    cannot be trusted" exists, and an unstorable number is exactly that. The alternative --
    a bespoke reason, or a service-level catch -- would be a second way of saying the same
    thing, for a case every other malformed field already covers.
    """
    with pytest.raises(ProviderResponseError, match=r"(?i)digits before the decimal point"):
        require_price(digits, source=VENDOR)


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("9" * 26, id="exactly at the bound"),
        pytest.param("86000.10000", id="a real price, nowhere near it"),
        pytest.param("0.04228645", id="a sub-cent price, at the other end"),
    ],
)
def test_a_price_the_column_can_hold_is_accepted(digits: str) -> None:
    """The boundary from the allowed side, so the check is `>` on the digit count.

    Twenty-six nines is the largest price this application represents and it must bind.
    Without this row the bound could be off by one in the refusing direction and nothing
    would say so -- and an off-by-one there refuses a legitimate value forever, which is
    the harder failure to notice because it looks like a vendor problem.
    """
    assert require_price(digits, source=VENDOR) == Decimal(digits)


def test_the_bound_refuses_before_the_database_is_ever_reached() -> None:
    """The point of putting it here: the value never becomes a `PriceQuote` at all.

    Asserted as the absence of a `sqlalchemy` exception in the type raised. It is a thin
    assertion on its own -- this function has no database -- and it is the statement of
    intent the end-to-end test in `tests/providers/prices/test_failover.py` then proves:
    three pairs keep their prices when a fourth is impossible.
    """
    with pytest.raises(ProviderResponseError) as caught:
        require_price("1E+300", source=VENDOR)

    assert type(caught.value).__module__.startswith("portfolio.")
    assert "1E+300" not in str(caught.value)


# --------------------------------------------------------------------------------------
# And a price too small to store, which is the other end of the same bound
# --------------------------------------------------------------------------------------
#
# `NumericText` refuses a non-zero amount that rounds away to nothing, for every money
# column. That refusal happens at **bind** time, inside `flush()`, which is far too late:
# the quote has been built, the service has accepted it, and the other pairs in the same
# refresh are already in the transaction that is about to be rolled back.
#
# So the same value is refused here as well, where it is a vendor error like any other.
# The duplication is deliberate and the two are not redundant: the column's guard protects
# every money column from every caller, and this one keeps a vendor's number from costing a
# refresh the pairs that worked.


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("0.0000000000005", id="the measured vanishing price"),
        pytest.param("1E-30", id="far below the scale"),
        pytest.param("0.0000000000004", id="rounds down to zero"),
    ],
)
def test_a_price_too_small_for_the_column_to_hold_is_refused(digits: str) -> None:
    """Refused at the parser, so the pair fails over instead of the refresh failing.

    A price of `0.0000000000005` stored at twelve places is a **zero**, and a zero price
    values every holding of that asset at nothing inside a total flagged complete. That is
    criterion 3's failure arriving through the column, and the column now refuses it -- but
    refusing it only there means the refresh dies mid-transaction and the three pairs that
    answered are rolled back with it.
    """
    with pytest.raises(ProviderResponseError, match=r"(?i)round it away to zero"):
        require_price(digits, source=VENDOR)


def test_the_smallest_storable_price_is_still_a_price() -> None:
    """The boundary from the allowed side: one unit at the column's scale binds.

    `0.000000000001` is the smallest price this application can represent, and it has to be
    accepted -- a bound that refused it would refuse a legitimate value forever, which is
    the harder failure to notice because it looks like a vendor problem.
    """
    assert require_price("0.000000000001", source=VENDOR) == Decimal("0.000000000001")
    assert require_price("0.0000000000006", source=VENDOR) == Decimal("0.0000000000006")


def test_both_ends_of_the_bound_are_refused_for_the_same_reason_in_the_same_vocabulary() -> None:
    """Too large and too small are one rule with two ends, and both are a vendor error.

    Asserted together because the temptation is to treat them differently -- an enormous
    number looks like a broken vendor and a tiny one looks like a real price -- and they
    have the identical consequence: a value this application cannot store, reaching a
    `flush()` that will discard everything around it.
    """
    with pytest.raises(ProviderResponseError) as too_large:
        require_price("1E+300", source=VENDOR)
    with pytest.raises(ProviderResponseError) as too_small:
        require_price("1E-300", source=VENDOR)

    assert VENDOR in str(too_large.value)
    assert VENDOR in str(too_small.value)
    assert str(too_large.value) != str(too_small.value), (
        "the two ends need different messages; an operator has to know which one it was"
    )
