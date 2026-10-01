"""Criterion 2 of #104 (spec 025), at the seam: `AssetBalance` and `assemble_balances` alone.

A venue's balance answer becomes `AssetBalance`s, and `assemble_balances` turns them into
what `fetch_balances` promises: **one entry per asset, zero balances left out, sorted by
asset**. Both venues' parsers go through these two, so the rules are tested here once, with
no HTTP and no venue, and each venue's own tests then only have to show that it gets here.

What must fail if the behaviour is removed, each with a test below named after it:

* **a zero is dropped** -- whichever way it is spelled;
* **an asset named twice is refused**, and not summed, and not "the last one wins" -- even
  when one of the two entries is a zero, because it is the answer's shape that is unknown;
* **a negative amount is refused**, and so is anything `NumericText(18)` would transform: a
  nineteenth decimal place, a twenty-first integer digit, a NaN, a float;
* **no message carries an asset name or an amount** (spec 025: neither may reach a log line,
  and an exception's text is a log line as soon as something fails).

Every refusal is asserted to be exactly `ExchangeSchemaError`, one of the seven classes, so
the sync classifies it as `schema` rather than as an `internal` failure with a traceback.
"""

from __future__ import annotations

import dataclasses
import decimal
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.providers.exchanges import base as seam
from portfolio.providers.exchanges.base import (
    MAX_ASSET_NAME_LENGTH,
    AssetBalance,
    assemble_balances,
    is_asset_name,
)
from portfolio.providers.exchanges.errors import ExchangeError, ExchangeSchemaError

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Distinctive, so their absence from a message means something.
MARKED_ASSET: Final = "ZZMARKED"
MARKED_NEGATIVE: Final = "-4242.4242"
MARKED_FINE: Final = "4242.4242424242424242424"
MARKED_HUGE: Final = "424242424242424242424"

LONE_SURROGATE: Final[str] = chr(0xD800)
#: Built with `chr`, so this file stays ASCII: a euro sign, and one CJK character.
NON_ASCII_ASSET: Final[str] = "USD" + chr(0x20AC)
CJK_ASSET: Final[str] = chr(0x5E01)

LARGEST: Final = "99999999999999999999.999999999999999999"
"""Twenty digits before the point and eighteen after: the most a `NumericText(18)` holds."""


def refusal(asset: object = "BTC", quantity: object = Decimal(1)) -> ExchangeSchemaError:
    """Build an `AssetBalance` that must be refused, and hand back the refusal."""
    with pytest.raises(ExchangeSchemaError) as caught:
        AssetBalance(asset=asset, quantity=quantity)  # type: ignore[arg-type]
    assert type(caught.value) is ExchangeSchemaError
    return caught.value


def rendered(error: BaseException) -> str:
    """Everything of an exception that a log line or a traceback could print.

    The cause and the context are both included, suppressed or not (spec 025, R4): a
    `UnicodeEncodeError` kept as the context of a refusal holds the whole string it could
    not encode, which here is an asset name, and `raise ... from None` only hides it from
    the default traceback -- it is still on the exception for any renderer that looks.
    """
    return f"{error}{error!r}{error.args!r}{error.__cause__!r}{error.__context__!r}"


def balance(asset: str, quantity: str) -> AssetBalance:
    return AssetBalance(asset=asset, quantity=Decimal(quantity))


def pairs(balances: tuple[AssetBalance, ...]) -> list[tuple[str, str]]:
    return [(entry.asset, str(entry.quantity)) for entry in balances]


# --------------------------------------------------------------------------------------
# `AssetBalance`
# --------------------------------------------------------------------------------------


def test_a_balance_is_an_asset_and_a_quantity_and_nothing_else() -> None:
    assert [field.name for field in dataclasses.fields(AssetBalance)] == ["asset", "quantity"]
    assert "AssetBalance" in seam.__all__
    assert "assemble_balances" in seam.__all__


def test_a_balance_is_frozen() -> None:
    held = balance("BTC", "0.5")

    with pytest.raises(dataclasses.FrozenInstanceError):
        held.quantity = Decimal(2)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        held.asset = "ETH"  # type: ignore[misc]


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param("0.5", id="ordinary"),
        pytest.param("0", id="zero"),
        pytest.param("0E-18", id="zero at the fill scale"),
        pytest.param("0.000000000000000001", id="one unit at eighteen places"),
        pytest.param("0.000000000000000001000", id="trailing zeros past eighteen places"),
        pytest.param("1.50000000000000000000", id="twenty places that lose nothing"),
        pytest.param("9" * 20, id="twenty integer digits"),
        pytest.param(LARGEST, id="the largest storable amount"),
        pytest.param("1E+5", id="a positive exponent"),
    ],
)
def test_a_storable_quantity_is_kept_exactly_as_given(quantity: str) -> None:
    """Zero is constructible: it is `assemble_balances` that leaves a zero out."""
    given_quantity = Decimal(quantity)

    held = AssetBalance(asset="KAS", quantity=given_quantity)

    assert held.quantity is given_quantity
    assert held.quantity.as_tuple() == Decimal(quantity).as_tuple(), "the digits were touched"


def test_a_negative_zero_is_a_zero_and_not_a_negative_amount() -> None:
    held = balance("BTC", "-0")

    assert held.quantity == 0
    assert assemble_balances([held]) == ()


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param("-1", id="minus one"),
        pytest.param("-0.000000000000000001", id="minus one unit"),
        pytest.param(MARKED_NEGATIVE, id="marked"),
        pytest.param("-" + LARGEST, id="the most negative storable amount"),
    ],
)
def test_a_negative_quantity_is_refused(quantity: str) -> None:
    """A spot account does not hold less than nothing, and a venue saying so is not read."""
    error = refusal(quantity=Decimal(quantity))

    assert "quantity" in str(error)
    assert "negative" in str(error)


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param("0.0000000000000000001", id="nineteen places"),
        pytest.param("0.0000000000000000011", id="eighteen places and a nineteenth digit"),
        pytest.param("1.2345678901234567891", id="nineteen places on an ordinary amount"),
        pytest.param(MARKED_FINE, id="marked"),
    ],
)
def test_a_quantity_finer_than_the_fill_scale_is_refused(quantity: str) -> None:
    """`NumericText(18)` would round it, and a balance is stored as reported."""
    error = refusal(quantity=Decimal(quantity))

    assert "quantity" in str(error)
    assert "18 decimal places" in str(error)


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param("1" + "0" * 20, id="twenty-one integer digits"),
        pytest.param("9" * 21, id="twenty-one nines"),
        pytest.param(MARKED_HUGE, id="marked"),
        pytest.param("1E+20", id="written with an exponent"),
        pytest.param("1E+400", id="absurd"),
    ],
)
def test_a_quantity_too_large_for_the_column_is_refused(quantity: str) -> None:
    error = refusal(quantity=Decimal(quantity))

    assert "quantity" in str(error)
    assert "20 digits before the decimal point" in str(error)


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param(1.5, id="float"),
        pytest.param(1.0, id="whole float"),
        pytest.param(0.0, id="float zero"),
        pytest.param(True, id="bool"),
        pytest.param(1, id="int"),
        pytest.param(0, id="int zero"),
        pytest.param("1.5", id="str"),
        pytest.param(None, id="none"),
        pytest.param(Decimal("NaN"), id="nan"),
        pytest.param(Decimal("sNaN"), id="snan"),
        pytest.param(Decimal("-NaN"), id="negative nan"),
        pytest.param(Decimal("Infinity"), id="infinity"),
        pytest.param(Decimal("-Infinity"), id="negative infinity"),
    ],
)
def test_a_quantity_that_is_not_a_finite_decimal_is_refused(quantity: object) -> None:
    """Money is never a float: a parser that built one is refused here, before the column.

    A NaN is refused before the sign is looked at -- ordering one raises
    `decimal.InvalidOperation`, which is not one of the seven classes.
    """
    error = refusal(quantity=quantity)

    assert "quantity" in str(error)
    assert not isinstance(error.__cause__, decimal.DecimalException)


@pytest.mark.parametrize(
    "asset",
    [
        pytest.param("", id="empty"),
        pytest.param(" ", id="a space"),
        pytest.param("\t\n", id="whitespace"),
        pytest.param(None, id="none"),
        pytest.param(7, id="int"),
        pytest.param(b"BTC", id="bytes"),
        pytest.param(f"BT{LONE_SURROGATE}C", id="a lone surrogate"),
    ],
)
def test_an_asset_that_is_not_non_blank_encodable_text_is_refused(asset: object) -> None:
    error = refusal(asset=asset)

    assert "asset" in str(error)


@pytest.mark.parametrize("asset", ["BTC", "btc", "1INCH", " BTC ", NON_ASCII_ASSET, CJK_ASSET])
def test_an_asset_is_kept_exactly_as_given(asset: str) -> None:
    """No folding and no trimming here: a provider that normalises does so before this."""
    assert AssetBalance(asset=asset, quantity=Decimal(1)).asset == asset


@pytest.mark.parametrize(
    ("asset", "quantity", "markers"),
    [
        pytest.param(MARKED_ASSET, MARKED_NEGATIVE, ("4242",), id="negative"),
        pytest.param(MARKED_ASSET, MARKED_FINE, ("4242",), id="too fine"),
        pytest.param(MARKED_ASSET, MARKED_HUGE, ("4242",), id="too large"),
        pytest.param(MARKED_ASSET, "NaN", ("NaN",), id="nan"),
        pytest.param(f"{MARKED_ASSET}{LONE_SURROGATE}", "1", (), id="unencodable asset"),
    ],
)
def test_a_refused_balance_names_the_field_and_neither_the_asset_nor_the_amount(
    asset: str, quantity: str, markers: tuple[str, ...]
) -> None:
    """Both are the owner's holdings; the field and the rule are what anyone can act on."""
    error = refusal(asset=asset, quantity=Decimal(quantity))
    text = rendered(error)

    assert MARKED_ASSET not in text, text
    for marker in markers:
        assert marker not in text, text


def test_a_refused_asset_is_not_kept_on_the_refusal_as_its_context() -> None:
    """R4: the encoding error is not chained, in either slot.

    Its arguments are `('utf-8', <the whole string>, start, end, reason)`.
    """
    error = refusal(asset=f"{MARKED_ASSET}{LONE_SURROGATE}")

    assert error.__cause__ is None
    assert error.__context__ is None


def test_the_refusal_is_one_of_the_seven_classes() -> None:
    assert issubclass(ExchangeSchemaError, ExchangeError)
    assert isinstance(refusal(quantity=Decimal(-1)), ExchangeError)


# --------------------------------------------------------------------------------------
# `assemble_balances`
# --------------------------------------------------------------------------------------


def test_no_entries_is_an_account_that_holds_nothing() -> None:
    assert assemble_balances([]) == ()
    assert assemble_balances(()) == ()


def test_the_result_is_a_tuple_whatever_iterable_it_was_given() -> None:
    def parsed() -> Iterator[AssetBalance]:
        yield balance("KAS", "1500")
        yield balance("BTC", "0.25")

    result = assemble_balances(parsed())

    assert isinstance(result, tuple)
    assert pairs(result) == [("BTC", "0.25"), ("KAS", "1500")]


def test_the_entries_kept_are_the_entries_given() -> None:
    """Nothing is rebuilt, rounded or re-spelled: `1.50` stays `1.50`."""
    btc = balance("BTC", "1.50")
    kas = balance("KAS", "0.000000000000000001")

    result = assemble_balances([kas, btc])

    assert result[0] is btc
    assert result[1] is kas
    assert str(result[0].quantity) == "1.50"


@pytest.mark.parametrize(
    "zero",
    ["0", "0.0", "0E-18", "0.000000000000000000", "-0", "0E+5"],
)
def test_a_zero_balance_is_dropped_however_it_is_spelled(zero: str) -> None:
    """ "The account holds nothing of it" has one spelling: no entry."""
    result = assemble_balances([balance("ETH", zero), balance("BTC", "0.25"), balance("KAS", zero)])

    assert pairs(result) == [("BTC", "0.25")]


def test_an_account_of_nothing_but_zeros_holds_nothing() -> None:
    assert assemble_balances([balance("BTC", "0"), balance("ETH", "0E-18")]) == ()


def test_the_smallest_amount_is_not_a_zero() -> None:
    result = assemble_balances([balance("KAS", "0.000000000000000001")])

    assert pairs(result) == [("KAS", "1E-18")]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("1", "2", id="two holdings"),
        pytest.param("1", "1", id="the same holding twice"),
        pytest.param("1", "0", id="a holding and a zero"),
        pytest.param("0", "1", id="a zero and a holding"),
        pytest.param("0", "0", id="two zeros"),
    ],
)
def test_an_asset_named_twice_is_refused(first: str, second: str) -> None:
    """Refused, not summed and not last-one-wins: the two readings differ by the balance.

    The check runs before the zeros are dropped, so a duplicate is refused even when one of
    the two is empty -- it is the shape of the answer that is not recognised.
    """
    entries = [
        balance("KAS", "5"),
        balance(MARKED_ASSET, first),
        balance("BTC", "0.25"),
        balance(MARKED_ASSET, second),
    ]

    with pytest.raises(ExchangeSchemaError) as caught:
        assemble_balances(entries)

    assert type(caught.value) is ExchangeSchemaError


def test_the_duplicate_refusal_names_no_asset_no_amount_and_no_count() -> None:
    entries = [
        balance(MARKED_ASSET, "4242.4242"),
        balance("BTC", "0.25"),
        balance(MARKED_ASSET, "7373.7373"),
    ]

    with pytest.raises(ExchangeSchemaError) as caught:
        assemble_balances(entries)

    text = rendered(caught.value)
    assert MARKED_ASSET not in text, text
    assert "4242" not in text, text
    assert "7373" not in text, text
    assert "BTC" not in text, text
    assert not any(character.isdigit() for character in str(caught.value)), str(caught.value)


def test_two_spellings_of_one_name_are_two_assets_here() -> None:
    """The comparison is exact. Bitget's provider upper-cases *before* this, so that its two
    spellings meet here as a duplicate; this function folds nothing itself."""
    result = assemble_balances([balance("btc", "1"), balance("BTC", "2"), balance(" BTC", "3")])

    assert pairs(result) == [(" BTC", "3"), ("BTC", "2"), ("btc", "1")]


def test_the_balances_are_sorted_by_asset_in_code_point_order() -> None:
    """Digits, then capitals, then lower case: the same account reads the same on every run."""
    names = ["ZEC", "kas", "ETH", "1INCH", "BTC", "AAVE", "Btc"]

    result = assemble_balances([balance(name, "1") for name in names])

    assert [entry.asset for entry in result] == ["1INCH", "AAVE", "BTC", "Btc", "ETH", "ZEC", "kas"]


# --------------------------------------------------------------------------------------
# `is_asset_name`: the one rule for a name both venues' parsers hold a balance to (R6)
# --------------------------------------------------------------------------------------


def test_the_longest_asset_name_is_forty_characters() -> None:
    assert MAX_ASSET_NAME_LENGTH == 40
    assert "is_asset_name" in seam.__all__
    assert "MAX_ASSET_NAME_LENGTH" in seam.__all__


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("BTC", id="ordinary"),
        pytest.param("usdt", id="lower case"),
        pytest.param("x", id="one character"),
        pytest.param("a" * 40, id="exactly forty"),
        pytest.param("$U", id="a dollar sign"),
        pytest.param("D.O.G.E.", id="dots"),
        pytest.param("ATOM(ARC20)", id="parentheses"),
        pytest.param("A_B-C", id="an underscore and a hyphen"),
        pytest.param("M" + chr(0x00D8) + "TH", id="a non-ASCII letter"),
        pytest.param(chr(0x5E01), id="a CJK character"),
    ],
)
def test_a_name_an_asset_may_have_is_one(name: str) -> None:
    assert is_asset_name(name) is True
    assert AssetBalance(asset=name, quantity=Decimal(1)).asset == name, "and it stores"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("a" * 41, id="forty-one characters"),
        pytest.param(" ", id="a space"),
        pytest.param(" USDT", id="a leading space"),
        pytest.param("USDT ", id="a trailing space"),
        pytest.param("US DT", id="a space inside"),
        pytest.param("USDT\t", id="a tab"),
        pytest.param("USDT\n", id="a newline"),
        pytest.param("US" + chr(0x00A0) + "DT", id="a no-break space"),
        pytest.param("US" + chr(0x2003) + "DT", id="an em space"),
        pytest.param("US" + chr(0x3000) + "DT", id="an ideographic space"),
        pytest.param("US" + chr(0x0000) + "DT", id="a NUL"),
        pytest.param("US" + chr(0x001F) + "DT", id="a control character"),
        pytest.param("US" + chr(0x007F) + "DT", id="a delete"),
        pytest.param("US" + chr(0x0085) + "DT", id="a next-line control"),
        pytest.param("US" + chr(0x200B) + "DT", id="a zero-width space"),
        pytest.param(chr(0xFEFF) + "USDT", id="a byte order mark"),
        pytest.param("US" + chr(0x00AD) + "DT", id="a soft hyphen"),
        pytest.param("US" + chr(0xE000) + "DT", id="a private-use character"),
        pytest.param("US" + chr(0x0378) + "DT", id="an unassigned code point"),
        pytest.param("US" + LONE_SURROGATE + "DT", id="a lone surrogate"),
    ],
)
def test_a_name_with_whitespace_a_c_category_character_or_the_wrong_length_is_not_one(
    name: str,
) -> None:
    """Whitespace **anywhere** and every Unicode `C*` category: refused, never stripped."""
    assert is_asset_name(name) is False


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=45))
def test_whatever_the_rule_accepts_is_short_unspaced_and_encodes(name: str) -> None:
    """Refusing category `Cs` is what makes an accepted name encode as UTF-8, so a name the
    rule lets through is one `AssetBalance` and the database driver can take."""
    if is_asset_name(name):
        assert 1 <= len(name) <= 40
        assert not any(character.isspace() for character in name)
        assert name == name.strip()
        name.encode("utf-8")
        assert AssetBalance(asset=name, quantity=Decimal(1)).asset == name


@settings(max_examples=200, deadline=None)
@given(
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=0, max_size=20),
    st.sampled_from([" ", "\t", "\n", chr(0x00A0), chr(0x2003), chr(0x0000), chr(0x200B)]),
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=0, max_size=19),
)
def test_one_space_or_control_character_anywhere_spoils_a_name(
    before: str, spoiler: str, after: str
) -> None:
    assert is_asset_name(before + after) is bool(before + after)
    assert is_asset_name(before + spoiler + after) is False


# --------------------------------------------------------------------------------------
# Hypothesis: any answer a parser could hand over
# --------------------------------------------------------------------------------------

ASSET_NAMES: Final = st.sampled_from(
    ["BTC", "ETH", "KAS", "BGB", "USDT", "USDC", "1INCH", "btc", "A", "ZZ9"]
)
UNITS: Final = st.one_of(
    st.just(0),
    st.just(0),
    st.integers(min_value=1, max_value=200),
    st.integers(min_value=1, max_value=10**38 - 1),
)


def from_units(units: int) -> Decimal:
    return Decimal(f"{units}E-18")


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(ASSET_NAMES, UNITS), st.data())
def test_any_account_assembles_to_its_non_zero_balances_sorted(
    account: dict[str, int], data: st.DataObject
) -> None:
    entries = data.draw(
        st.permutations(
            [AssetBalance(asset, from_units(units)) for asset, units in account.items()]
        )
    )

    result = assemble_balances(entries)

    expected = sorted((asset, units) for asset, units in account.items() if units != 0)
    assert [(entry.asset, entry.quantity) for entry in result] == [
        (asset, from_units(units)) for asset, units in expected
    ]
    assert all(entry.quantity > 0 for entry in result)


@settings(max_examples=200, deadline=None)
@given(
    st.dictionaries(ASSET_NAMES, UNITS, min_size=1),
    st.data(),
)
def test_any_account_with_an_asset_named_twice_is_refused(
    account: dict[str, int], data: st.DataObject
) -> None:
    entries = [AssetBalance(asset, from_units(units)) for asset, units in account.items()]
    repeated = data.draw(st.sampled_from(sorted(account)))
    position = data.draw(st.integers(min_value=0, max_value=len(entries)))
    entries.insert(position, AssetBalance(repeated, from_units(data.draw(UNITS))))

    with pytest.raises(ExchangeSchemaError):
        assemble_balances(entries)


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=0, max_value=10**38 - 1))
def test_every_amount_the_column_can_hold_is_a_valid_balance(units: int) -> None:
    quantity = from_units(units)

    assert AssetBalance("KAS", quantity).quantity == quantity


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=1, max_value=10**40))
def test_every_negative_amount_is_refused(units: int) -> None:
    refusal(quantity=from_units(units).copy_negate())
