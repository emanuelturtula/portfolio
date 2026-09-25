"""Criteria 1 and 2: the normalized fill, the capabilities, and the helpers every venue shares.

Every expected value in this module is written by hand beside the input it belongs to, and
none is computed by calling the code under test. Where a boundary depends on a constant --
`FILL_SCALE`, `RETENTION_MARGIN` -- the boundary is written as a literal too, and the
constant is pinned separately against its own literal. A test that derived its boundary from
the constant would move with it, and could not notice the constant changing.

Every refusal asserts the **exact** class. `ExchangeSchemaError` is what a parser raises for
an answer it cannot trust; a `ValueError` is the caller's own mistake; the two are not
interchangeable and a test that accepted either would not notice them swapping.

No clock is read. Every instant is a literal, and `now` is passed explicitly wherever the
code takes one.
"""

from __future__ import annotations

import dataclasses
import decimal
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portfolio.db.models import FILL_SCALE
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.providers.base import decode_json
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.exchanges.base import (
    MAX_AMOUNT_DIGITS,
    MAX_RAW_PAYLOAD_DEPTH,
    RETENTION_MARGIN,
    CursorKind,
    ExchangeCapabilities,
    FillPage,
    FillWindow,
    NormalizedFill,
    RateLimit,
    RetentionClamp,
    assemble_fill_page,
    clamp_to_retention,
    datetime_from_epoch_ms,
    derive_quote_quantity,
    encode_raw_payload,
    epoch_ms,
    floor_to_millisecond,
    require_cursor_advanced,
    require_fill_amount,
)
from portfolio.providers.exchanges.errors import ExchangeSchemaError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# --------------------------------------------------------------------------------------
# Fixtures, as plain values
# --------------------------------------------------------------------------------------

NOW: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

#: A seven-day window, exactly the declared maximum below.
SINCE: Final = datetime(2026, 9, 1, tzinfo=UTC)
UNTIL: Final = datetime(2026, 9, 8, tzinfo=UTC)
INSIDE: Final = datetime(2026, 9, 3, 15, 30, 0, 123000, tzinfo=UTC)
ONE_MICROSECOND: Final = timedelta(microseconds=1)
#: The finest granularity a window or a declared duration may have: venues count in ms.
ONE_MILLISECOND: Final = timedelta(milliseconds=1)

AMOUNT_FIELDS: Final = ("quantity", "price", "quote_quantity", "fee_amount")
STRICTLY_POSITIVE_FIELDS: Final = ("quantity", "price", "quote_quantity")

#: The field names the spec's `NormalizedFill` block lists, pinned by hand.
EXPECTED_FILL_FIELDS: Final = (
    "external_trade_id",
    "external_order_id",
    "symbol",
    "base_asset",
    "quote_asset",
    "side",
    "quantity",
    "price",
    "quote_quantity",
    "quote_quantity_derived",
    "fee_amount",
    "fee_asset",
    "executed_at",
    "raw_payload",
)


def capabilities(
    *,
    page_size: int = 3,
    retention: timedelta | None = timedelta(days=90),
    max_query_window: timedelta = timedelta(days=7),
    requires_symbol: bool = False,
) -> ExchangeCapabilities:
    """A plausible venue: 90 days kept, 7-day queries, pages of three so a test can fill one."""
    return ExchangeCapabilities(
        exchange_key=ExchangeKey.BITGET,
        retention=retention,
        max_query_window=max_query_window,
        page_size=page_size,
        cursor_kind=CursorKind.TRADE_ID_BEFORE,
        rate_limit=RateLimit(max_requests=10, per_ms=1000),
        requires_symbol=requires_symbol,
    )


def make_fill(**overrides: object) -> NormalizedFill:
    """A valid fill, with any field replaced. 0.00012300 BTC at 86000.10 is 10.578012300."""
    fields: dict[str, object] = {
        "external_trade_id": "1001",
        "external_order_id": "5001",
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "side": FillSide.BUY,
        "quantity": Decimal("0.00012300"),
        "price": Decimal("86000.10"),
        "quote_quantity": Decimal("10.57801230"),
        "quote_quantity_derived": False,
        "fee_amount": Decimal("0.0105780123"),
        "fee_asset": "USDT",
        "executed_at": INSIDE,
        "raw_payload": '{"tradeId":"1001"}',
    }
    fields.update(overrides)
    return NormalizedFill(**fields)  # type: ignore[arg-type]


def refusal(**overrides: object) -> ExchangeSchemaError:
    """Build a fill that must be refused, and hand back the exact error it raised."""
    with pytest.raises(ExchangeSchemaError) as caught:
        make_fill(**overrides)
    assert type(caught.value) is ExchangeSchemaError
    return caught.value


# --------------------------------------------------------------------------------------
# Criterion 1: the reported quote quantity, the derived one, and the flag between them
# --------------------------------------------------------------------------------------


def test_the_fill_fields_are_the_ones_the_spec_names() -> None:
    assert tuple(field.name for field in dataclasses.fields(NormalizedFill)) == (
        EXPECTED_FILL_FIELDS
    )


def test_the_fill_scale_is_eighteen() -> None:
    """Pinned by hand: every place-count boundary below is written against 18, not against this."""
    assert FILL_SCALE == 18


@pytest.mark.parametrize(
    "reported",
    [
        pytest.param("1.000000000000000000", id="one unit above the product"),
        pytest.param("0.999999999999999998", id="one unit below the product"),
    ],
)
def test_a_reported_quote_quantity_is_kept_when_it_disagrees_with_the_product(
    reported: str,
) -> None:
    """3 x 0.333333333333333333 is 0.999999999999999999; the venue said something else.

    The venue's figure is kept exactly, spelling included, and the flag says it was
    reported. Recomputing it would introduce a one-unit disagreement with the venue's own
    records that reconciliation would chase forever.
    """
    fill = make_fill(
        quantity=Decimal("3"),
        price=Decimal("0.333333333333333333"),
        quote_quantity=Decimal(reported),
        quote_quantity_derived=False,
    )

    assert str(fill.quote_quantity) == reported
    assert fill.quote_quantity_derived is False

    page = assemble_fill_page(
        FillWindow(SINCE, UNTIL),
        [fill],
        capabilities=capabilities(),
        cursor=None,
        next_cursor=None,
        symbol=None,
    )
    assert str(page.fills[0].quote_quantity) == reported


@pytest.mark.parametrize(
    ("quantity", "price", "expected"),
    [
        # Padded to 18 places: 0.5 x 86000.10 = 43000.05, exactly.
        pytest.param("0.5", "86000.10", "43000.050000000000000000", id="exact, padded"),
        # 1.5e-18 and 2.5e-18 are ties at the 18th place. Half-even takes both to 2e-18;
        # half-up would give 2e-18 and 3e-18.
        pytest.param("1.5", "0.000000000000000001", "0.000000000000000002", id="tie up to even"),
        pytest.param("2.5", "0.000000000000000001", "0.000000000000000002", id="tie down to even"),
        # 36 fractional places, 39 significant digits: 124.499999999999999995 plus itself
        # shifted 18 places right is 124.500000000000000119|499999999999999995. One rounding
        # to 18 places reads the 19th digit, 4, and rounds down. Rounding to 38 significant
        # digits first makes the tail ...5000 and turns it into a tie, which half-even then
        # takes *up* to ...120 -- the double-rounding error a multiply under the money
        # context would have introduced.
        pytest.param(
            "1.000000000000000001",
            "124.499999999999999995",
            "124.500000000000000119",
            id="a product needing more than 18 places, rounded once",
        ),
    ],
)
def test_a_derived_quote_quantity_is_the_product_rounded_to_the_fill_scale(
    quantity: str, price: str, expected: str
) -> None:
    """Compared through `format(..., "f")`, which is how a fill column writes it.

    `str()` would switch to exponent form below 1E-6 and render the tie rows as `2E-18`;
    the fixed-point rendering is also what pins the result at exactly 18 places.
    """
    derived = derive_quote_quantity(Decimal(quantity), Decimal(price))

    assert format(derived, "f") == expected
    assert derived.as_tuple().exponent == -18

    fill = make_fill(
        quantity=Decimal(quantity),
        price=Decimal(price),
        quote_quantity=derived,
        quote_quantity_derived=True,
    )
    assert fill.quote_quantity_derived is True
    assert format(fill.quote_quantity, "f") == expected


def test_the_double_rounded_answer_is_not_the_one_returned() -> None:
    """The control for the last row above: the wrong answer exists and differs by one unit."""
    derived = derive_quote_quantity(
        Decimal("1.000000000000000001"), Decimal("124.499999999999999995")
    )

    assert derived == Decimal("124.500000000000000119")
    assert derived != Decimal("124.500000000000000120")


def test_derivation_uses_the_money_context_not_the_thread_context() -> None:
    """The same answers inside a context narrowed to ten digits."""
    outside = derive_quote_quantity(
        Decimal("1.000000000000000001"), Decimal("124.499999999999999995")
    )

    with decimal.localcontext() as context:
        context.prec = 10
        inside = derive_quote_quantity(
            Decimal("1.000000000000000001"), Decimal("124.499999999999999995")
        )
        padded = derive_quote_quantity(Decimal("0.5"), Decimal("86000.10"))

    assert str(inside) == "124.500000000000000119"
    assert str(outside) == "124.500000000000000119"
    assert str(padded) == "43000.050000000000000000"


# --------------------------------------------------------------------------------------
# Criterion 1: a value the column would transform is refused by the parser
# --------------------------------------------------------------------------------------

#: 19 fractional digits: one more than a fill column stores. `NumericText` would round it.
NINETEEN_PLACES: Final = "0.0000000000000000011"
#: 18 fractional digits: exactly what the column stores.
EIGHTEEN_PLACES: Final = "0.000000000000000001"
#: 21 fractional digits, all past the 18th zero: the same value as 18 places, so not finer.
TRAILING_ZEROS: Final = "0.000000000000000001000"


@pytest.mark.parametrize("field", AMOUNT_FIELDS)
def test_an_amount_finer_than_the_fill_scale_is_refused(field: str) -> None:
    """19 places refused, 18 accepted, and trailing zeros past 18 are not a false refusal."""
    refusal(**{field: Decimal(NINETEEN_PLACES), "fee_asset": "USDT"})
    refusal(**{field: Decimal("1.0000000000000000001"), "fee_asset": "USDT"})

    accepted = make_fill(**{field: Decimal(EIGHTEEN_PLACES)})
    padded = make_fill(**{field: Decimal(TRAILING_ZEROS)})

    assert getattr(accepted, field) == Decimal(EIGHTEEN_PLACES)
    assert getattr(padded, field) == Decimal(EIGHTEEN_PLACES)
    # Kept as handed in -- digits and exponent -- because the fill is not the place that
    # normalises a spelling. (`str()` renders this one as `1.000E-18`, so the tuple is what
    # is compared.)
    assert getattr(padded, field).as_tuple() == Decimal(TRAILING_ZEROS).as_tuple()


#: Twenty integer digits fit beside 18 fractional ones in 38; twenty-one do not.
TWENTY_INTEGER_DIGITS: Final = Decimal("9" * 20)
TWENTY_ONE_INTEGER_DIGITS: Final = Decimal("9" * 21)


@pytest.mark.parametrize("field", STRICTLY_POSITIVE_FIELDS)
@pytest.mark.parametrize("value", ["0", "0.000", "-0", "-1", "-0.000000000000000001"])
def test_non_positive_amounts_are_refused(field: str, value: str) -> None:
    refusal(**{field: Decimal(value)})


@pytest.mark.parametrize("field", AMOUNT_FIELDS)
def test_oversized_amounts_are_refused(field: str) -> None:
    refusal(**{field: TWENTY_ONE_INTEGER_DIGITS})
    if field == "fee_amount":
        refusal(fee_amount=-TWENTY_ONE_INTEGER_DIGITS)

    accepted = make_fill(**{field: TWENTY_INTEGER_DIGITS})
    assert getattr(accepted, field) == TWENTY_INTEGER_DIGITS


def test_non_positive_and_oversized_amounts_are_refused() -> None:
    """The spec's named case, with its companion: the smallest positive amount is accepted."""
    refusal(quantity=Decimal(0))
    refusal(price=Decimal("-1"))
    refusal(quote_quantity=Decimal("0.0"))
    refusal(quantity=TWENTY_ONE_INTEGER_DIGITS)

    smallest = make_fill(quantity=Decimal(EIGHTEEN_PLACES))
    assert smallest.quantity > 0


#: Distinctive digits, so their absence from a message means something.
MARKED_FINE: Final = "4242.4242424242424242424"
MARKED_NEGATIVE: Final = "-4242.4242"
MARKED_HUGE: Final = "424242424242424242424"


@pytest.mark.parametrize("field", AMOUNT_FIELDS)
def test_a_refused_fill_names_the_field_and_not_the_amount(field: str) -> None:
    """A fill quantity is the owner's holdings; the field is what anyone can act on."""
    cases = [MARKED_FINE, MARKED_HUGE]
    if field in STRICTLY_POSITIVE_FIELDS:
        cases.append(MARKED_NEGATIVE)

    for value in cases:
        error = refusal(**{field: Decimal(value), "fee_asset": "USDT"})
        rendered = f"{error}{error!r}{error.args}"

        assert field in str(error), str(error)
        assert "4242" not in rendered, rendered
        assert Decimal(value).to_eng_string() not in rendered


@pytest.mark.parametrize("field", AMOUNT_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1.5, id="float"),
        pytest.param(1.0, id="whole float"),
        pytest.param(True, id="bool"),
        pytest.param(1, id="int"),
        pytest.param("1.5", id="str"),
        pytest.param(None, id="none"),
        pytest.param(Decimal("NaN"), id="nan"),
        pytest.param(Decimal("sNaN"), id="snan"),
        pytest.param(Decimal("Infinity"), id="infinity"),
        pytest.param(Decimal("-Infinity"), id="negative infinity"),
    ],
)
def test_float_bool_and_non_finite_amounts_are_refused(field: str, value: object) -> None:
    refusal(**{field: value})


def test_a_rebate_is_negative_and_a_non_zero_fee_needs_an_asset() -> None:
    rebate = make_fill(fee_amount=Decimal("-0.0105780123"), fee_asset="USDT")
    free = make_fill(fee_amount=Decimal("0"), fee_asset=None)
    free_with_asset = make_fill(fee_amount=Decimal("0.000"), fee_asset="BNB")

    assert rebate.fee_amount == Decimal("-0.0105780123")
    assert free.fee_asset is None
    assert free_with_asset.fee_asset == "BNB"

    refusal(fee_amount=Decimal("0.01"), fee_asset=None)
    refusal(fee_amount=Decimal("-0.01"), fee_asset=None)


@pytest.mark.parametrize("trade_id", ["", " ", "   ", "\t", "\n"])
def test_an_empty_or_blank_trade_id_is_refused(trade_id: str) -> None:
    """The empty id is the one that turns the unique constraint into a silent drop."""
    refusal(external_trade_id=trade_id)


def test_a_trade_id_of_zero_is_not_blank() -> None:
    assert make_fill(external_trade_id="0").external_trade_id == "0"


@pytest.mark.parametrize(
    "side",
    [pytest.param("buy", id="the value as a plain str"), "BUY", "long", None],
)
def test_a_side_that_is_not_a_fill_side_is_refused(side: object) -> None:
    refusal(side=side)


def test_both_sides_are_accepted() -> None:
    assert make_fill(side=FillSide.SELL).side is FillSide.SELL
    assert make_fill(side=FillSide.BUY).side is FillSide.BUY


def test_a_naive_execution_time_is_refused() -> None:
    refusal(executed_at=datetime(2026, 9, 3, 15, 30))  # noqa: DTZ001 - naive on purpose

    offset = timezone(timedelta(hours=8))
    assert make_fill(executed_at=INSIDE.astimezone(offset)).executed_at == INSIDE


def test_a_fill_is_frozen() -> None:
    fill = make_fill()

    with pytest.raises(dataclasses.FrozenInstanceError):
        fill.quote_quantity = Decimal("1")  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# Criterion 1: the raw payload
# --------------------------------------------------------------------------------------

#: A venue's fill object as it arrives, with the three spellings that matter: a number with
#: trailing zeros, one in exponent form, and keys out of order at two levels.
RAW_BODY: Final = (
    '{"tradeId": "1001", "qty": 0.00012300, "px": 1E+2, "fee": "-0.001",'
    ' "nested": {"b": [1, 2, true, null], "a": "x"}, "count": 7}'
)
#: The same object, canonical: keys sorted, no whitespace, numbers spelt as received.
CANONICAL: Final = (
    '{"count":7,"fee":"-0.001","nested":{"a":"x","b":[1,2,true,null]},'
    '"px":1E+2,"qty":0.00012300,"tradeId":"1001"}'
)


def test_the_raw_payload_round_trips_through_the_shared_decoder() -> None:
    document = decode_json(RAW_BODY)

    encoded = encode_raw_payload(document)
    decoded = decode_json(encoded)

    assert encoded == CANONICAL
    assert decoded == document
    assert isinstance(decoded, dict)
    assert str(decoded["qty"]) == "0.00012300"
    assert str(decoded["px"]) == "1E+2"
    assert list(json.loads(encoded)) == sorted(json.loads(encoded))
    # Canonical means a second pass changes nothing.
    assert encode_raw_payload(decoded) == encoded


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('{"q": 1.5e1}', id="a decimal whose exponent is zero"),
        pytest.param('{"q": 150e-1}', id="the same value, one place"),
        pytest.param('{"q": -0.0}', id="negative zero"),
        pytest.param('{"q": 1E-30}', id="far below the fill scale"),
    ],
)
def test_a_decoded_decimal_comes_back_a_decimal_with_its_own_digits(body: str) -> None:
    """`1.5e1` decodes to `Decimal("15")`, and `15` written back would decode as an `int`."""
    document = decode_json(body)
    assert isinstance(document, dict)

    decoded = decode_json(encode_raw_payload(document))

    assert isinstance(decoded, dict)
    assert type(decoded["q"]) is Decimal
    assert decoded["q"].as_tuple() == document["q"].as_tuple()


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"qty": 1.5}, id="float"),
        pytest.param({"qty": [Decimal("1"), 0.5]}, id="nested float"),
        pytest.param({"ids": {1, 2}}, id="set"),
        pytest.param({"at": datetime(2026, 9, 25, tzinfo=UTC)}, id="datetime"),
        pytest.param(1.5, id="bare float"),
    ],
)
def test_encode_raw_payload_refuses_a_value_the_decoder_cannot_produce(document: object) -> None:
    """A provider bug, not a vendor's: `TypeError`, not a schema error."""
    with pytest.raises(TypeError):
        encode_raw_payload(document)


DECIMAL_LITERALS: Final = st.from_regex(
    r"-?(0|[1-9][0-9]{0,6})\.[0-9]{1,10}([eE][+-]?[0-9]{1,2})?", fullmatch=True
)
JSON_LEAVES: Final = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**20), max_value=10**20)
    | st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=8)
    | DECIMAL_LITERALS.map(Decimal)
)
JSON_DOCUMENTS: Final = st.recursive(
    JSON_LEAVES,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(
            st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=6),
            children,
            max_size=4,
        )
    ),
    max_leaves=12,
)


def shape(value: object) -> object:
    """A comparable form that keeps what `==` forgets: a `Decimal`'s digits and exponent.

    `1 == Decimal(1)` and `Decimal("1.0") == Decimal("1.00")`, so `==` alone would pass a
    payload that came back as an `int`, or with its places changed. The type name of every
    leaf and the full tuple of every `Decimal` are what "spelt as received" means here.
    """
    if isinstance(value, Decimal):
        return ("decimal", value.as_tuple())
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(item) for item in value]
    return (type(value).__name__, value)


@given(document=JSON_DOCUMENTS)
def test_the_raw_payload_round_trips_for_any_decodable_document(document: object) -> None:
    """Every `Decimal` comes back with its own digits, every other leaf with its own type."""
    encoded = encode_raw_payload(document)
    decoded = decode_json(encoded)

    assert decoded == document
    assert shape(decoded) == shape(document)
    assert encode_raw_payload(decoded) == encoded


# --------------------------------------------------------------------------------------
# Criterion 1: the parser boundary
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("0.00012300", "0.00012300", id="string keeps its places"),
        pytest.param("-0.001", "-0.001", id="negative string"),
        pytest.param(Decimal("86000.10"), "86000.10", id="decimal from the decoder"),
        pytest.param(7, "7", id="int"),
        pytest.param(0, "0", id="zero"),
    ],
)
def test_require_fill_amount_accepts_strings_decimals_and_ints_only(
    value: object, expected: str
) -> None:
    amount = require_fill_amount(value, field="price")

    assert isinstance(amount, Decimal)
    assert str(amount) == expected


MALFORMED_MARKER: Final = "4242abc"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool"),
        pytest.param(1.5, id="float"),
        pytest.param(MALFORMED_MARKER, id="malformed string"),
        pytest.param("", id="empty string"),
        pytest.param("NaN", id="nan string"),
        pytest.param("Infinity", id="infinity string"),
        pytest.param(Decimal("NaN"), id="nan decimal"),
        pytest.param(None, id="none"),
        pytest.param([1], id="list"),
    ],
)
def test_require_fill_amount_refuses_everything_else(value: object) -> None:
    with pytest.raises(ExchangeSchemaError) as caught:
        require_fill_amount(value, field="price")

    assert type(caught.value) is ExchangeSchemaError
    assert "price" in str(caught.value)
    assert MALFORMED_MARKER not in f"{caught.value}{caught.value!r}"


#: Past CPython's default int-to-str digit limit of 4300. Built at run time rather than
#: written out: a literal of five thousand ones is unreadable, and a long run of base58
#: digits is what the address scanner looks for.
OVERLONG_PRICE_DIGITS: Final = 5000


def test_a_five_thousand_digit_price_is_refused_by_the_parser_bound() -> None:
    """A vendor's 5000-digit price stops at `require_fill_amount`, in the taxonomy.

    **What this proves, and what it does not.** The refusal is the 100-digit bound on the
    parser boundary -- asserted by its reason below -- so no arithmetic is ever reached with
    this input. It says nothing about `multiply` on long operands; that is
    `test_a_derivation_from_five_thousand_digit_operands_is_exact`, which builds its
    operands without this function precisely so it cannot be stopped here first.
    """
    long_price = "1." + "1" * OVERLONG_PRICE_DIGITS

    with pytest.raises(ExchangeSchemaError) as caught:
        require_fill_amount(long_price, field="price")

    assert type(caught.value) is ExchangeSchemaError
    assert "price has more than 100 digits written out in full" in str(caught.value)
    assert "1111111111" not in f"{caught.value}{caught.value!r}{caught.value.args}"


def test_a_derivation_from_five_thousand_digit_operands_is_exact() -> None:
    """`derive_quote_quantity` reached directly, with coefficients past the 4300-digit limit.

    Built as `Decimal`s, never through `require_fill_amount`, so the parser bound cannot
    refuse them first. Both coefficients are 5001 digits:

    * quantity `1.111...1` (5000 ones after the point);
    * price `1.000...01` (4999 zeros, then a 1), which is `1 + 10**-5000`.

    The product is the quantity plus the quantity shifted 5000 places right, and that shift
    touches nothing before place 5000 -- so to 18 places it is eighteen ones, and the 19th
    digit is a 1, which rounds down. Written by hand from that argument.
    """
    quantity = Decimal("1." + "1" * OVERLONG_PRICE_DIGITS)
    price = Decimal("1." + "0" * (OVERLONG_PRICE_DIGITS - 1) + "1")
    assert len(quantity.as_tuple().digits) == len(price.as_tuple().digits) == 5001

    derived = derive_quote_quantity(quantity, price)
    with decimal.localcontext() as context:
        context.prec = 10
        derived_inside = derive_quote_quantity(quantity, price)

    assert format(derived, "f") == "1.111111111111111111"
    assert derived.as_tuple().exponent == -18
    assert derived_inside == derived


def nesting(depth: int) -> str:
    return "[" * depth + "]" * depth


def decodes(body: str) -> bool:
    try:
        decode_json(body)
    except ProviderResponseError:
        return False
    return True


def deepest_decodable_nesting() -> int:
    """The deepest `[[...]]` `decode_json` accepts on *this* interpreter, found by bisection.

    Not a constant: the scanner's limit is `Py_C_RECURSION_LIMIT`, a build constant that was
    measured at 2998 on a Windows build and past 5000 on the Linux the Pi runs. A depth
    written down from one of them is a test about the other one's interpreter.
    """
    shallow, deep = 1, 200_000
    assert decodes(nesting(shallow))
    if decodes(nesting(deep)):
        return deep
    while deep - shallow > 1:
        middle = (shallow + deep) // 2
        if decodes(nesting(middle)):
            shallow = middle
        else:
            deep = middle
    return shallow


def test_a_payload_nested_deeper_than_the_encoder_can_walk_is_a_schema_error() -> None:
    """The deepest fill object this host can decode, handed back to the encoder.

    A `RecursionError` would escape the taxonomy -- from a body the vendor chooses -- so it
    must arrive as the schema error. The document is inside an object, as a fill is, so it
    is one level shallower in lists than the probe's limit.
    """
    depth = deepest_decodable_nesting()
    assert depth > 100, "the probe found almost nothing decodable; it is not probing"
    document = decode_json('{"tradeId": "1", "x": ' + nesting(depth - 1) + "}")

    with pytest.raises(ExchangeSchemaError) as caught:
        encode_raw_payload(document)

    assert type(caught.value) is ExchangeSchemaError


def test_a_shallowly_nested_payload_encodes_and_round_trips() -> None:
    """The companion: nesting itself is not what is refused."""
    body = '{"tradeId": "1", "x": ' + nesting(20) + ', "qty": 0.00012300}'
    document = decode_json(body)

    decoded = decode_json(encode_raw_payload(document))

    assert decoded == document
    assert isinstance(decoded, dict)
    assert str(decoded["qty"]) == "0.00012300"


# --------------------------------------------------------------------------------------
# Criterion 1: epoch milliseconds, without a float
# --------------------------------------------------------------------------------------

#: 1_700_000_000 seconds after the epoch is 2023-11-14T22:13:20Z; 123 ms more is below.
EPOCH_MS: Final = 1_700_000_000_123
EPOCH_INSTANT: Final = datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)

#: The last millisecond `datetime` can represent: 9999-12-31T23:59:59.999Z.
LAST_MS: Final = 253_402_300_799_999
LAST_INSTANT: Final = datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)


def test_epoch_milliseconds_convert_exactly_both_ways() -> None:
    from_int = datetime_from_epoch_ms(EPOCH_MS)
    from_string = datetime_from_epoch_ms(str(EPOCH_MS))

    assert from_int == EPOCH_INSTANT
    assert from_string == EPOCH_INSTANT
    assert from_int.utcoffset() == timedelta(0)
    assert epoch_ms(EPOCH_INSTANT) == EPOCH_MS
    assert epoch_ms(datetime_from_epoch_ms(EPOCH_MS)) == EPOCH_MS
    assert datetime_from_epoch_ms(0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert datetime_from_epoch_ms(LAST_MS) == LAST_INSTANT
    assert epoch_ms(LAST_INSTANT) == LAST_MS


def test_epoch_ms_of_another_offset_is_the_same_instant() -> None:
    offset = timezone(timedelta(hours=-3))

    assert epoch_ms(EPOCH_INSTANT.astimezone(offset)) == EPOCH_MS


def test_epoch_ms_floors_a_sub_millisecond_remainder() -> None:
    """`// timedelta(milliseconds=1)` floors: 123.999 ms is 123, never rounded to 124."""
    moment = datetime(2023, 11, 14, 22, 13, 20, 123999, tzinfo=UTC)

    assert epoch_ms(moment) == EPOCH_MS


def test_epoch_ms_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the refusal is the whole assertion
        epoch_ms(datetime(2023, 11, 14, 22, 13, 20))  # noqa: DTZ001 - naive on purpose


@given(milliseconds=st.integers(min_value=0, max_value=LAST_MS))
def test_every_representable_millisecond_round_trips(milliseconds: int) -> None:
    assert epoch_ms(datetime_from_epoch_ms(milliseconds)) == milliseconds
    assert epoch_ms(datetime_from_epoch_ms(str(milliseconds))) == milliseconds


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool"),
        pytest.param(1.7e12, id="float"),
        pytest.param(-1, id="negative"),
        pytest.param("-1", id="negative string"),
        pytest.param("12a", id="12a"),
        pytest.param("", id="empty"),
        pytest.param(" 1", id="leading space"),
        pytest.param("1.5", id="decimal string"),
        pytest.param("١٢", id="non-ASCII digits"),
        pytest.param(None, id="none"),
        pytest.param(LAST_MS + 1, id="past the last representable millisecond"),
        pytest.param(10**20, id="absurdly large"),
    ],
)
def test_a_malformed_epoch_value_is_a_schema_error(value: object) -> None:
    """A venue's timestamp that is not a count of milliseconds, refused in the taxonomy.

    The last two rows are the ones that would escape as an untyped `OverflowError` from
    `timedelta` -- outside the seven classes the protocol says a provider raises.
    """
    with pytest.raises(ExchangeSchemaError) as caught:
        datetime_from_epoch_ms(value)

    assert type(caught.value) is ExchangeSchemaError


# --------------------------------------------------------------------------------------
# Criterion 2: capabilities
# --------------------------------------------------------------------------------------

EXPECTED_CAPABILITY_FIELDS: Final = {
    "exchange_key",
    "retention",
    "max_query_window",
    "page_size",
    "cursor_kind",
    "rate_limit",
    "requires_symbol",
}


def test_capabilities_declare_every_field_the_issue_names() -> None:
    """Retention, maximum query window, page size, cursor kind, rate limit, per-symbol."""
    declared = capabilities(requires_symbol=True)

    assert {field.name for field in dataclasses.fields(ExchangeCapabilities)} == (
        EXPECTED_CAPABILITY_FIELDS
    )
    assert declared.retention == timedelta(days=90)
    assert declared.max_query_window == timedelta(days=7)
    assert declared.page_size == 3
    assert declared.cursor_kind is CursorKind.TRADE_ID_BEFORE
    assert declared.rate_limit == RateLimit(max_requests=10, per_ms=1000)
    assert declared.requires_symbol is True
    assert capabilities(retention=None).retention is None


def test_the_cursor_kinds_are_the_pinned_set() -> None:
    assert {kind.name: kind.value for kind in CursorKind} == {
        "TRADE_ID_BEFORE": "trade_id_before",
        "TRADE_ID_AFTER": "trade_id_after",
        "TIME": "time",
        "NONE": "none",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"page_size": 0}, id="page size zero"),
        pytest.param({"page_size": -1}, id="negative page size"),
        pytest.param({"max_query_window": timedelta(0)}, id="zero query window"),
        pytest.param({"max_query_window": timedelta(days=-1)}, id="negative query window"),
        pytest.param({"retention": timedelta(0)}, id="zero retention"),
        pytest.param({"retention": timedelta(days=-1)}, id="negative retention"),
    ],
)
def test_capabilities_refuse_impossible_declarations(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - one refusal per row
        capabilities(**overrides)  # type: ignore[arg-type]

    assert type(caught.value) is ValueError


def test_the_smallest_possible_declarations_are_accepted() -> None:
    """The companion: one page of one fill, one millisecond of history, is still a venue.

    A millisecond, not a microsecond, since the reviewer's granularity fix: every venue
    speaks epoch milliseconds, so a duration finer than one cannot be asked for.
    """
    tiny = capabilities(page_size=1, retention=ONE_MILLISECOND, max_query_window=ONE_MILLISECOND)

    assert tiny.page_size == 1
    assert tiny.retention == tiny.max_query_window == timedelta(milliseconds=1)


@pytest.mark.parametrize(
    ("max_requests", "per_ms", "expected"),
    [
        pytest.param(3, 1000, 334, id="1000/3 rounds up"),
        pytest.param(7, 1000, 143, id="1000/7 rounds up"),
        pytest.param(10, 1000, 100, id="exact"),
        pytest.param(1, 1000, 1000, id="one per window"),
        pytest.param(1000, 1, 1, id="never below one"),
    ],
)
def test_the_minimum_interval_rounds_up(max_requests: int, per_ms: int, expected: int) -> None:
    """Up, never down: rounding down would send one request too many per window."""
    interval = RateLimit(max_requests=max_requests, per_ms=per_ms).min_interval_ms

    assert interval == expected
    assert type(interval) is int


@pytest.mark.parametrize(
    ("max_requests", "per_ms"),
    [
        pytest.param(0, 1000, id="no requests"),
        pytest.param(-1, 1000, id="negative requests"),
        pytest.param(3, 0, id="zero window"),
        pytest.param(3, -1000, id="negative window"),
        pytest.param(True, 1000, id="bool requests"),
        pytest.param(3, 1000.0, id="float window"),
    ],
)
def test_a_rate_limit_refuses_itself(max_requests: object, per_ms: object) -> None:
    """`RateLimit(0, 1000).min_interval_ms` would divide by zero; it is refused at birth."""
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - one refusal per row
        RateLimit(max_requests=max_requests, per_ms=per_ms)  # type: ignore[arg-type]

    assert type(caught.value) is ValueError


# --------------------------------------------------------------------------------------
# Criterion 2: the retention clamp
# --------------------------------------------------------------------------------------


def test_the_retention_margin_is_five_minutes() -> None:
    """Pinned by hand; the clamp tests below write the margin into their literals."""
    assert timedelta(minutes=5) == RETENTION_MARGIN


def test_a_request_older_than_retention_is_clamped_not_refused() -> None:
    """A year back against 90 days kept: the effective start is inside retention by 5 min.

    `NOW` is 2026-09-25T12:00Z. Ninety days before it is 2026-06-27T12:00Z (25 days back to
    31 Aug, 31 more to 31 Jul, 31 more to 30 Jun, 3 more to 27 Jun); five minutes later is
    12:05. The margin moves the start *later*, into the kept window -- the opposite sign
    would ask for five minutes the venue has already discarded.
    """
    requested = datetime(2025, 9, 25, 12, 0, tzinfo=UTC)

    clamp = clamp_to_retention(requested, now=NOW, capabilities=capabilities())

    assert isinstance(clamp, RetentionClamp)
    assert clamp.requested_since == requested
    assert clamp.effective_since == datetime(2026, 6, 27, 12, 5, tzinfo=UTC)
    assert clamp.clamped is True


@pytest.mark.parametrize(
    ("requested", "clamped"),
    [
        # Exactly at the retention edge: still five minutes too old.
        pytest.param(datetime(2026, 6, 27, 12, 0, tzinfo=UTC), True, id="at the edge"),
        pytest.param(
            datetime(2026, 6, 27, 12, 4, 59, 999999, tzinfo=UTC), True, id="inside the margin"
        ),
        pytest.param(datetime(2026, 6, 27, 12, 5, tzinfo=UTC), False, id="at the margin"),
        # A whole millisecond past, so the floor to the millisecond cannot move it back.
        pytest.param(datetime(2026, 6, 27, 12, 5, 0, 1000, tzinfo=UTC), False, id="past it"),
    ],
)
def test_the_clamp_boundary_is_the_edge_plus_the_margin(requested: datetime, clamped: bool) -> None:
    clamp = clamp_to_retention(requested, now=NOW, capabilities=capabilities())

    assert clamp.clamped is clamped
    assert clamp.effective_since == max(requested, datetime(2026, 6, 27, 12, 5, tzinfo=UTC))


def test_a_request_inside_retention_and_an_unlimited_venue_are_untouched() -> None:
    recent = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    ancient = datetime(2016, 1, 1, tzinfo=UTC)

    inside = clamp_to_retention(recent, now=NOW, capabilities=capabilities())
    unlimited = clamp_to_retention(ancient, now=NOW, capabilities=capabilities(retention=None))
    at_now = clamp_to_retention(NOW, now=NOW, capabilities=capabilities())

    assert (inside.effective_since, inside.clamped) == (recent, False)
    assert (unlimited.effective_since, unlimited.clamped) == (ancient, False)
    assert (at_now.effective_since, at_now.clamped) == (NOW, False)


@pytest.mark.parametrize(
    "retention",
    [
        pytest.param(timedelta(minutes=1), id="one minute kept"),
        pytest.param(ONE_MILLISECOND, id="one millisecond kept"),
        pytest.param(timedelta(minutes=5), id="exactly the margin"),
    ],
)
def test_a_retention_no_longer_than_the_margin_clamps_to_now(retention: timedelta) -> None:
    """The margin would otherwise put the effective start in the future.

    `now - 1 minute + 5 minutes` is four minutes from now, and a window starting there
    cannot contain a fill. The start is capped at `now` instead: an empty window rather than
    a backwards one. (Implementation's reading, beyond the spec's `max(...)` formula; the
    formula alone admits a start after `now`.)
    """
    requested = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    clamp = clamp_to_retention(requested, now=NOW, capabilities=capabilities(retention=retention))

    assert clamp.effective_since == NOW
    assert clamp.clamped is True


@pytest.mark.parametrize(
    ("requested", "now"),
    [
        pytest.param(NOW + ONE_MICROSECOND, NOW, id="a microsecond in the future"),
        pytest.param(datetime(2026, 9, 1), NOW, id="naive request"),  # noqa: DTZ001
        pytest.param(SINCE, datetime(2026, 9, 25, 12), id="naive now"),  # noqa: DTZ001
    ],
)
def test_a_future_or_naive_request_is_refused(requested: datetime, now: datetime) -> None:
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - one refusal per row
        clamp_to_retention(requested, now=now, capabilities=capabilities())

    assert type(caught.value) is ValueError


def test_clamped_is_derived_from_the_two_instants() -> None:
    assert RetentionClamp(requested_since=SINCE, effective_since=SINCE).clamped is False
    assert RetentionClamp(requested_since=SINCE, effective_since=UNTIL).clamped is True


# --------------------------------------------------------------------------------------
# Criterion 2: the page contract
# --------------------------------------------------------------------------------------

WINDOW: Final = FillWindow(since=SINCE, until=UNTIL)


def assemble(
    fills: Sequence[NormalizedFill],
    *,
    window: FillWindow = WINDOW,
    declared: ExchangeCapabilities | None = None,
    cursor: str | None = None,
    next_cursor: str | None = None,
    symbol: str | None = None,
) -> FillPage:
    return assemble_fill_page(
        window,
        fills,
        capabilities=declared or capabilities(),
        cursor=cursor,
        next_cursor=next_cursor,
        symbol=symbol,
    )


def test_a_valid_page_is_assembled_in_order_with_its_cursors() -> None:
    fills = [
        make_fill(external_trade_id="3", executed_at=INSIDE),
        make_fill(external_trade_id="1", executed_at=SINCE),
        make_fill(external_trade_id="2", executed_at=UNTIL - ONE_MICROSECOND),
    ]

    page = assemble(fills, cursor="c1", next_cursor="c2")

    assert page.window == WINDOW
    assert [fill.external_trade_id for fill in page.fills] == ["3", "1", "2"]
    assert isinstance(page.fills, tuple)
    assert (page.cursor, page.next_cursor, page.symbol) == ("c1", "c2", None)


@pytest.mark.parametrize(
    "fills",
    [pytest.param([], id="empty page"), pytest.param([make_fill()], id="single fill")],
)
def test_an_empty_or_single_fill_page_is_a_page(fills: list[NormalizedFill]) -> None:
    page = assemble(fills, next_cursor=None)

    assert list(page.fills) == fills
    assert page.next_cursor is None


def test_a_symbol_page_carries_its_symbol() -> None:
    page = assemble([make_fill()], declared=capabilities(requires_symbol=True), symbol="BTCUSDT")

    assert page.symbol == "BTCUSDT"


def test_a_window_of_exactly_the_maximum_is_accepted() -> None:
    assert timedelta(days=7) == UNTIL - SINCE
    assert assemble([]).window == WINDOW


DUPLICATE_ID: Final = "dup-trade-7777"
OUTSIDE_ID: Final = "late-trade-8888"
OTHER_SYMBOL: Final = "ETHUSDT"


def call_that_breaks(case: str) -> Callable[[], FillPage]:
    """One broken contract per row of the spec's table, as a zero-argument call."""
    # One millisecond too long: a microsecond would be refused by the window itself, for
    # its granularity, and the row would pass without reaching the maximum-length rule.
    long_window = FillWindow(since=SINCE, until=UNTIL + ONE_MILLISECOND)
    cases: dict[str, Callable[[], FillPage]] = {
        "window longer than the maximum": lambda: assemble([], window=long_window),
        "symbol given but not required": lambda: assemble([], symbol="BTCUSDT"),
        "symbol required but missing": lambda: assemble(
            [], declared=capabilities(requires_symbol=True)
        ),
        "fill before since": lambda: assemble(
            [make_fill(external_trade_id=OUTSIDE_ID, executed_at=SINCE - ONE_MICROSECOND)]
        ),
        "fill at until": lambda: assemble(
            [make_fill(external_trade_id=OUTSIDE_ID, executed_at=UNTIL)]
        ),
        "fill after until": lambda: assemble(
            [make_fill(external_trade_id=OUTSIDE_ID, executed_at=UNTIL + timedelta(days=1))]
        ),
        "duplicate trade id": lambda: assemble(
            [
                make_fill(external_trade_id=DUPLICATE_ID),
                make_fill(external_trade_id=DUPLICATE_ID, executed_at=SINCE),
            ]
        ),
        "more fills than the page size": lambda: assemble(
            [make_fill(external_trade_id=str(number)) for number in range(4)]
        ),
        "cursor did not advance": lambda: assemble([], cursor="c1", next_cursor="c1"),
        "fill for another symbol": lambda: assemble(
            [
                make_fill(external_trade_id="1"),
                make_fill(external_trade_id=OUTSIDE_ID, symbol=OTHER_SYMBOL),
            ],
            declared=capabilities(requires_symbol=True),
            symbol="BTCUSDT",
        ),
    }
    return cases[case]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("window longer than the maximum", ValueError),
        ("symbol given but not required", ValueError),
        ("symbol required but missing", ValueError),
        ("fill before since", ExchangeSchemaError),
        ("fill at until", ExchangeSchemaError),
        ("fill after until", ExchangeSchemaError),
        ("duplicate trade id", ExchangeSchemaError),
        ("more fills than the page size", ExchangeSchemaError),
        ("cursor did not advance", ExchangeSchemaError),
        ("fill for another symbol", ExchangeSchemaError),
    ],
)
def test_assemble_fill_page_refuses_each_broken_contract(
    case: str, expected: type[Exception]
) -> None:
    """The caller's mistakes are `ValueError`; the venue's are the schema error. Exactly."""
    with pytest.raises(expected) as caught:
        call_that_breaks(case)()

    assert type(caught.value) is expected
    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert DUPLICATE_ID not in rendered
    assert OUTSIDE_ID not in rendered
    assert "0.00012300" not in rendered


def test_a_per_symbol_page_accepts_fills_for_its_own_symbol() -> None:
    """The companion to the other-symbol row: every fill matching the query passes."""
    fills = [make_fill(external_trade_id=str(number)) for number in range(3)]

    page = assemble(fills, declared=capabilities(requires_symbol=True), symbol="BTCUSDT")

    assert [fill.symbol for fill in page.fills] == ["BTCUSDT"] * 3


def test_a_venue_that_lists_every_symbol_at_once_may_mix_them() -> None:
    """With no per-symbol query there is no symbol to disagree with."""
    fills = [
        make_fill(external_trade_id="1", symbol="BTCUSDT"),
        make_fill(external_trade_id="2", symbol=OTHER_SYMBOL),
    ]

    page = assemble(fills, symbol=None)

    assert [fill.symbol for fill in page.fills] == ["BTCUSDT", OTHER_SYMBOL]


def test_exactly_a_full_page_is_accepted() -> None:
    """The companion to the page-size row: three fills in a page of three is a full page."""
    fills = [make_fill(external_trade_id=str(number)) for number in range(3)]

    assert len(assemble(fills).fills) == 3


def test_a_window_edge_is_half_open() -> None:
    """`[since, until)`: a fill at `since` is kept, one at `until` belongs to the next window."""
    at_since = assemble([make_fill(executed_at=SINCE)])
    just_before_until = assemble([make_fill(executed_at=UNTIL - ONE_MICROSECOND)])

    assert at_since.fills[0].executed_at == SINCE
    assert just_before_until.fills[0].executed_at == UNTIL - ONE_MICROSECOND
    with pytest.raises(ExchangeSchemaError):
        assemble([make_fill(executed_at=UNTIL)])


def test_a_fill_in_another_offset_is_placed_by_its_instant() -> None:
    """Compared as instants: 23:59 at UTC-3 on the last day is 02:59 UTC the day after."""
    offset = timezone(timedelta(hours=-3))
    last_evening = datetime(2026, 9, 7, 23, 59, tzinfo=offset)

    with pytest.raises(ExchangeSchemaError):
        assemble([make_fill(executed_at=last_evening)])
    assert assemble([make_fill(executed_at=datetime(2026, 9, 7, 20, 59, tzinfo=offset))])


# --------------------------------------------------------------------------------------
# Criterion 2: the cursor guard
# --------------------------------------------------------------------------------------


def test_a_cursor_that_stops_advancing_raises() -> None:
    with pytest.raises(ExchangeSchemaError) as caught:
        require_cursor_advanced("c1", "c1")
    assert type(caught.value) is ExchangeSchemaError

    # The positive companion: an advancing cursor, a first page, and a last page all pass.
    require_cursor_advanced("c1", "c2")
    require_cursor_advanced(None, "c1")
    require_cursor_advanced("c1", None)
    require_cursor_advanced(None, None)


def test_the_guard_does_not_name_the_cursor() -> None:
    """A cursor is often a trade id, and a trade id is not something a message carries."""
    with pytest.raises(ExchangeSchemaError) as caught:
        require_cursor_advanced("trade-9191", "trade-9191")

    assert "9191" not in f"{caught.value}{caught.value!r}"


# --------------------------------------------------------------------------------------
# Criterion 2: the window
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("since", "until"),
    [
        pytest.param(UNTIL, SINCE, id="backwards"),
        pytest.param(SINCE, SINCE, id="empty"),
        pytest.param(datetime(2026, 9, 1), UNTIL, id="naive since"),  # noqa: DTZ001
        pytest.param(SINCE, datetime(2026, 9, 8), id="naive until"),  # noqa: DTZ001
    ],
)
def test_a_window_must_be_aware_and_forwards(since: datetime, until: datetime) -> None:
    with pytest.raises(ValueError) as caught:  # noqa: PT011 - one refusal per row
        FillWindow(since=since, until=until)

    assert type(caught.value) is ValueError


def test_the_shortest_window_is_accepted() -> None:
    window = FillWindow(since=SINCE, until=SINCE + ONE_MILLISECOND)

    assert window.until - window.since == ONE_MILLISECOND


# --------------------------------------------------------------------------------------
# The bounds a venue cannot push past, pinned by hand, and their remaining refusals
# --------------------------------------------------------------------------------------


def test_the_two_bounds_are_the_pinned_values() -> None:
    """Pinned by hand; the boundary tests below write 100 and 32 into their inputs."""
    assert MAX_AMOUNT_DIGITS == 100
    assert MAX_RAW_PAYLOAD_DEPTH == 32


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("9" * 100, id="one hundred integer digits"),
        pytest.param("0." + "1" * 98, id="ninety-eight places"),
        pytest.param("0." + "0" * 50 + "1", id="a small amount written in full"),
    ],
)
def test_an_amount_up_to_one_hundred_digits_is_accepted(value: str) -> None:
    assert require_fill_amount(value, field="price") == Decimal(value)


#: The two reasons `require_fill_amount` gives for an amount it will not hold. Asserted
#: per row, so a row refused by a *different* rule -- the pattern, the finiteness check --
#: fails rather than passing on the class alone.
TOO_LONG: Final = "has more than 100 digits written out in full"
UNREPRESENTABLE: Final = "is a number this application cannot represent"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        pytest.param("9" * 101, TOO_LONG, id="one hundred and one integer digits"),
        pytest.param("0." + "1" * 101, TOO_LONG, id="one hundred and one places"),
        pytest.param("1e999999999999999999", TOO_LONG, id="a huge exponent"),
        pytest.param("1e-999999999999999999", TOO_LONG, id="a tiny exponent"),
        pytest.param(Decimal("9" * 101), TOO_LONG, id="an over-long Decimal"),
        pytest.param(10**100, TOO_LONG, id="an over-long int"),
        # `Decimal()` itself refuses this exponent, with `InvalidOperation`: the one input
        # that reaches that arm, since the pattern has refused every other malformed string.
        pytest.param(
            "1e99999999999999999999", UNREPRESENTABLE, id="an exponent Decimal cannot hold"
        ),
    ],
)
def test_an_amount_longer_than_one_hundred_digits_is_a_schema_error(
    value: object, reason: str
) -> None:
    with pytest.raises(ExchangeSchemaError) as caught:
        require_fill_amount(value, field="price")

    assert type(caught.value) is ExchangeSchemaError
    # The field and the reason together, so a refusal of some other field cannot pass.
    assert f"price {reason}" in str(caught.value), str(caught.value)
    assert "999999999" not in f"{caught.value}{caught.value!r}"


def test_a_derived_product_too_large_for_the_column_is_a_schema_error() -> None:
    """1E+15 x 1E+6 is 1E+21: twenty-two integer digits, two more than a fill column holds."""
    with pytest.raises(ExchangeSchemaError) as caught:
        derive_quote_quantity(Decimal("1E+15"), Decimal("1E+6"))

    assert type(caught.value) is ExchangeSchemaError
    assert "quote_quantity" in str(caught.value)
    # The reason, with the column's integer room written by hand: 38 - 18 = 20.
    assert "more than 20 digits before the decimal point" in str(caught.value)
    # The companion: twenty integer digits is still a quote quantity.
    assert derive_quote_quantity(Decimal("1E+13"), Decimal("1E+6")) == Decimal("1E+19")


def test_a_payload_thirty_two_deep_encodes_and_thirty_three_is_refused() -> None:
    thirty_two = decode_json(nesting(32))
    thirty_three = decode_json(nesting(33))

    assert decode_json(encode_raw_payload(thirty_two)) == thirty_two
    with pytest.raises(ExchangeSchemaError) as caught:
        encode_raw_payload(thirty_three)
    assert type(caught.value) is ExchangeSchemaError
    assert "more than 32 levels deep" in str(caught.value), str(caught.value)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        pytest.param({"qty": Decimal("NaN")}, "non-finite Decimal", id="a NaN"),
        pytest.param({1: "x"}, "every object key to be a str", id="a key that is not a str"),
    ],
)
def test_encode_raw_payload_refuses_other_shapes_the_decoder_cannot_produce(
    document: object, reason: str
) -> None:
    with pytest.raises(TypeError) as caught:
        encode_raw_payload(document)

    assert type(caught.value) is TypeError
    assert reason in str(caught.value), str(caught.value)


@pytest.mark.parametrize("flag", [1, 0, "true", None], ids=["one", "zero", "text", "none"])
def test_the_derived_flag_must_be_a_bool(flag: object) -> None:
    """`1` would store and read back as `True`; the column is not where the type is decided."""
    error = refusal(quote_quantity_derived=flag)

    assert "quote_quantity_derived must be a bool" in str(error), str(error)


@pytest.mark.parametrize(
    "payload", ["", "   ", None, b"{}"], ids=["empty", "blank", "none", "bytes"]
)
def test_the_raw_payload_must_be_a_non_blank_string(payload: object) -> None:
    error = refusal(raw_payload=payload)

    assert "raw_payload must be a non-empty string" in str(error), str(error)


def test_capabilities_refuse_a_rate_limit_that_is_not_one() -> None:
    with pytest.raises(TypeError, match="rate_limit must be a RateLimit, got tuple"):
        ExchangeCapabilities(
            exchange_key=ExchangeKey.BITGET,
            retention=None,
            max_query_window=timedelta(days=7),
            page_size=3,
            cursor_kind=CursorKind.TIME,
            rate_limit=(10, 1000),  # type: ignore[arg-type]
            requires_symbol=False,
        )


# --------------------------------------------------------------------------------------
# Review A: millisecond granularity, end to end
# --------------------------------------------------------------------------------------
#
# The defect the reviewer reproduced: `now` carries microseconds, so the clamp produced an
# effective start of 12:05:00.123456; a venue answers in whole milliseconds, so the oldest
# fill it can return is at 12:05:00.123 -- *before* the window's start -- and
# `assemble_fill_page` refused a legitimate fill as outside the window. The start is now
# floored to the millisecond (never ceiled, which would skip a real fill), and a window or
# a declared duration finer than a millisecond is refused outright.

#: The reviewer's clock reading, microseconds and all.
REVIEWER_NOW: Final = datetime(2026, 9, 25, 12, 0, 0, 123456, tzinfo=UTC)
#: 90 days before it is 2026-06-27T12:00:00.123456Z; five minutes later is 12:05:00.123456;
#: floored to the millisecond, 12:05:00.123000. Written by hand, as is every instant here.
REVIEWER_EFFECTIVE_SINCE: Final = datetime(2026, 6, 27, 12, 5, 0, 123000, tzinfo=UTC)


@pytest.mark.parametrize(
    ("moment", "floored"),
    [
        pytest.param(
            datetime(2026, 6, 27, 12, 5, 0, 123456, tzinfo=UTC),
            datetime(2026, 6, 27, 12, 5, 0, 123000, tzinfo=UTC),
            id="the reviewer's instant",
        ),
        pytest.param(
            datetime(2026, 6, 27, 12, 5, 0, 123999, tzinfo=UTC),
            datetime(2026, 6, 27, 12, 5, 0, 123000, tzinfo=UTC),
            id="just under the next millisecond floors, never rounds up",
        ),
        pytest.param(
            datetime(2026, 6, 27, 12, 5, 0, 999, tzinfo=UTC),
            datetime(2026, 6, 27, 12, 5, 0, 0, tzinfo=UTC),
            id="under one millisecond floors to the second",
        ),
        pytest.param(
            datetime(2026, 6, 27, 12, 5, 0, 123000, tzinfo=UTC),
            datetime(2026, 6, 27, 12, 5, 0, 123000, tzinfo=UTC),
            id="a whole millisecond is unchanged",
        ),
    ],
)
def test_floor_to_millisecond_floors_and_never_ceils(moment: datetime, floored: datetime) -> None:
    result = floor_to_millisecond(moment)

    assert result == floored
    assert result.microsecond % 1000 == 0
    assert result <= moment


def test_the_reviewers_scenario_end_to_end() -> None:
    """Clamp, window, page: a venue fill at the floored start is accepted.

    Before the fix this failed at the window (a start of .123456) or, with the window
    unchecked, at `assemble_fill_page`, which refused the fill at .123000 as older than the
    window it was asked for.
    """
    clamp = clamp_to_retention(
        datetime(2025, 1, 1, tzinfo=UTC), now=REVIEWER_NOW, capabilities=capabilities()
    )
    window = FillWindow(
        since=clamp.effective_since, until=clamp.effective_since + timedelta(days=7)
    )

    page = assemble([make_fill(executed_at=REVIEWER_EFFECTIVE_SINCE)], window=window)

    assert clamp.effective_since == REVIEWER_EFFECTIVE_SINCE
    assert clamp.clamped is True
    assert window.since == REVIEWER_EFFECTIVE_SINCE
    assert [fill.executed_at for fill in page.fills] == [REVIEWER_EFFECTIVE_SINCE]


def test_a_request_that_is_only_floored_is_not_clamped() -> None:
    """Inside retention, a sub-millisecond start is floored and `clamped` stays False.

    `clamped` means "retention moved the start later"; flooring moves it earlier by less
    than a millisecond, which asks for more and hides nothing.
    """
    requested = datetime(2026, 8, 26, 12, 0, 0, 1, tzinfo=UTC)

    clamp = clamp_to_retention(requested, now=NOW, capabilities=capabilities())
    unlimited = clamp_to_retention(requested, now=NOW, capabilities=capabilities(retention=None))

    assert clamp.effective_since == datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert clamp.clamped is False
    assert unlimited.effective_since == datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert unlimited.clamped is False


def test_floor_to_millisecond_keeps_the_zone_and_floors_the_absolute_instant() -> None:
    """The grid is absolute, so an offset that is itself sub-millisecond still lands on it.

    At UTC+0.5 ms, local 12:05:00.123456 is 12:05:00.122956 UTC; floored, 12:05:00.122000
    UTC; back in the same zone, 12:05:00.122500. Worked by hand. A floor on the local
    `microsecond` field would give .123000, which is not on the absolute grid at all.
    """
    half_millisecond_east = timezone(timedelta(microseconds=500))
    minus_three = timezone(timedelta(hours=-3))

    odd = floor_to_millisecond(
        datetime(2026, 6, 27, 12, 5, 0, 123456, tzinfo=half_millisecond_east)
    )
    ordinary = floor_to_millisecond(datetime(2026, 6, 27, 9, 5, 0, 123456, tzinfo=minus_three))

    assert odd == datetime(2026, 6, 27, 12, 5, 0, 122500, tzinfo=half_millisecond_east)
    assert odd.tzinfo is half_millisecond_east
    assert ordinary == datetime(2026, 6, 27, 9, 5, 0, 123000, tzinfo=minus_three)
    assert ordinary.tzinfo is minus_three


def test_floor_to_millisecond_refuses_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware") as caught:
        floor_to_millisecond(datetime(2026, 6, 27, 12, 5))  # noqa: DTZ001 - naive on purpose

    assert type(caught.value) is ValueError


def test_the_clamp_floors_a_start_capped_at_now() -> None:
    """A retention shorter than the margin caps the start at `now`, floored like any other."""
    clamp = clamp_to_retention(
        datetime(2026, 9, 24, tzinfo=UTC),
        now=REVIEWER_NOW,
        capabilities=capabilities(retention=timedelta(minutes=1)),
    )

    assert clamp.effective_since == datetime(2026, 9, 25, 12, 0, 0, 123000, tzinfo=UTC)


@pytest.mark.parametrize(
    ("since", "until"),
    [
        pytest.param(SINCE + ONE_MICROSECOND, UNTIL, id="since a microsecond past a millisecond"),
        pytest.param(SINCE, UNTIL - ONE_MICROSECOND, id="until a microsecond short of one"),
        pytest.param(
            datetime(2026, 9, 1, 0, 0, 0, 123456, tzinfo=UTC), UNTIL, id="the reviewer's shape"
        ),
    ],
)
def test_a_window_bound_finer_than_a_millisecond_is_refused(
    since: datetime, until: datetime
) -> None:
    with pytest.raises(ValueError, match="must be a whole millisecond") as caught:
        FillWindow(since=since, until=until)

    assert type(caught.value) is ValueError


def test_a_window_on_whole_milliseconds_is_accepted() -> None:
    window = FillWindow(since=SINCE + ONE_MILLISECOND, until=UNTIL - ONE_MILLISECOND)

    assert window.until - window.since == timedelta(days=7) - 2 * ONE_MILLISECOND


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"max_query_window": ONE_MICROSECOND}, id="a microsecond query window"),
        pytest.param(
            {"max_query_window": timedelta(days=7, microseconds=1)},
            id="seven days and a microsecond",
        ),
        pytest.param(
            {"retention": timedelta(microseconds=1500)}, id="a millisecond and a half kept"
        ),
    ],
)
def test_capabilities_refuse_a_duration_finer_than_a_millisecond(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="a whole number of milliseconds") as caught:
        capabilities(**overrides)  # type: ignore[arg-type]

    assert type(caught.value) is ValueError


# --------------------------------------------------------------------------------------
# Review B: a lone surrogate is text no column can store
# --------------------------------------------------------------------------------------
#
# `decode_json` turns the JSON escape `\ud800` into a Python string holding a lone UTF-16
# surrogate. Python holds it happily; UTF-8 cannot encode it, so the row fails at INSERT with
# a `UnicodeEncodeError` -- outside the taxonomy, and after the page was accepted. It is
# refused where the fill is built instead, naming the field and never the text.

#: Built with `chr` and typed `str`, never written as a `Final` literal: mypy caches a
#: `Final` string's literal type as UTF-8, and a lone surrogate crashes its cache writer
#: with `UnicodeEncodeError` -- the same defect this section tests, in the type checker.
LONE_SURROGATE: Final[str] = chr(0xD800)
MARKED_SURROGATE: Final[str] = f"trade-{LONE_SURROGATE}-4242"
TEXT_FIELDS: Final = (
    "external_trade_id",
    "external_order_id",
    "symbol",
    "base_asset",
    "quote_asset",
    "fee_asset",
    "raw_payload",
)


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_a_lone_surrogate_in_a_text_field_is_a_schema_error(field: str) -> None:
    error = refusal(**{field: MARKED_SURROGATE})
    rendered = f"{error}{error!r}{error.args}"

    assert f"{field} does not encode as UTF-8" in str(error), str(error)
    assert LONE_SURROGATE not in rendered
    assert "\\ud800" not in rendered
    assert "4242" not in rendered


@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_non_ascii_text_that_encodes_is_accepted(field: str) -> None:
    """The companion: the refusal is about unencodable text, not about text outside ASCII."""
    fill = make_fill(**{field: "café-€"})

    assert getattr(fill, field) == "café-€"


@pytest.mark.parametrize(
    ("cursor", "next_cursor", "reason"),
    [
        pytest.param(
            MARKED_SURROGATE, None, "cursor does not encode as UTF-8", id="the cursor asked with"
        ),
        pytest.param(
            "c1",
            MARKED_SURROGATE,
            "next_cursor does not encode as UTF-8",
            id="the cursor handed back",
        ),
    ],
)
def test_a_cursor_holding_a_lone_surrogate_is_a_schema_error(
    cursor: str | None, next_cursor: str | None, reason: str
) -> None:
    """A checkpoint that cannot be stored cannot resume a sync."""
    with pytest.raises(ExchangeSchemaError) as caught:
        assemble([], cursor=cursor, next_cursor=next_cursor)

    assert type(caught.value) is ExchangeSchemaError
    assert reason in str(caught.value), str(caught.value)
    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert LONE_SURROGATE not in rendered
    assert "4242" not in rendered


def test_a_cursor_in_non_ascii_text_that_encodes_is_accepted() -> None:
    page = assemble([], cursor="café-1", next_cursor="café-2")

    assert (page.cursor, page.next_cursor) == ("café-1", "café-2")


def test_an_order_id_that_is_not_text_is_a_schema_error() -> None:
    """The optional order id skips the blank check, so its type is refused on its own."""
    error = refusal(external_order_id=5001)

    assert "external_order_id must be a string" in str(error), str(error)


@pytest.mark.parametrize(
    ("cursor", "next_cursor", "reason"),
    [
        pytest.param(5, None, "cursor must be a string", id="the cursor asked with"),
        pytest.param("c1", 5, "next_cursor must be a string", id="the cursor handed back"),
    ],
)
def test_a_cursor_that_is_not_text_is_a_schema_error(
    cursor: object, next_cursor: object, reason: str
) -> None:
    """A venue that sends its cursor as a JSON number: stored and re-sent, it would not match."""
    with pytest.raises(ExchangeSchemaError) as caught:
        assemble([], cursor=cursor, next_cursor=next_cursor)  # type: ignore[arg-type]

    assert type(caught.value) is ExchangeSchemaError
    assert reason in str(caught.value), str(caught.value)
