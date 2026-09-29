"""Spec 019, *Events*: every refusal, and R2, R8 and R9 from the rulings.

A bad event is a caller's defect, not a replay warning, so it is refused at construction with
a `ValueError` (a `TypeError` for a wrong type) and never reaches `replay`. Each rule below
is exercised on every field it applies to, because the failure worth catching is not a
missing rule but a rule applied to `quantity` and forgotten on `fee_amount`.

**No message quotes an amount.** A fill quantity is the owner's holdings, and a refusal ends
up in a log. Every amount refusal is checked against the digits of the value it refused.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.accounting import (
    DEFAULT_CASH_ASSETS,
    AccountingConfig,
    Adjustment,
    EventKey,
    Trade,
    TradeShapeProblem,
    Transfer,
    trade_shape_problem,
)
from portfolio.domain.exchanges import FillSide
from tests.domain.accounting.strategies import amounts, from_units, to_units

if TYPE_CHECKING:
    from collections.abc import Callable

WHEN: Final = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
#: Built at runtime, as `tests/providers/exchanges/test_base.py` does: mypy writes a `Final`
#: string's literal type to its cache as UTF-8, and a lone surrogate crashes the writer.
LONE_SURROGATE: Final[str] = chr(0xD800)


def key(
    occurred_at: object = WHEN, source: object = "bitget", external_id: object = "e1"
) -> EventKey:
    return EventKey(occurred_at, source, external_id)  # type: ignore[arg-type]


def trade(**overrides: object) -> Trade:
    """A valid buy of 1 BTC for 30000 USDT with a 30 USDT fee, with `overrides` applied."""
    fields: dict[str, object] = {
        "key": key(),
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "side": FillSide.BUY,
        "quantity": Decimal("1"),
        "quote_quantity": Decimal("30000"),
        "fee_amount": Decimal("30"),
        "fee_asset": "USDT",
    }
    fields.update(overrides)
    return Trade(**fields)  # type: ignore[arg-type]


def adjustment(**overrides: object) -> Adjustment:
    fields: dict[str, object] = {
        "key": key(source="manual"),
        "asset": "BTC",
        "quantity": Decimal("2"),
        "unit_cost": Decimal("25000"),
    }
    fields.update(overrides)
    return Adjustment(**fields)  # type: ignore[arg-type]


def transfer(**overrides: object) -> Transfer:
    fields: dict[str, object] = {
        "key": key(),
        "asset": "BTC",
        "quantity": Decimal("0.5"),
        "from_location": "bitget",
        "to_location": "cold-storage",
    }
    fields.update(overrides)
    return Transfer(**fields)  # type: ignore[arg-type]


def digits_of(value: Decimal) -> str:
    """The longest run of digits in `value`'s plain spelling: what a leaked amount looks like."""
    runs: list[str] = re.findall(r"\d+", format(value, "f"))
    return max(runs, key=len)


def assert_amounts_not_quoted(error: BaseException, *amounts: Decimal) -> None:
    """No distinctive amount's digits appear in the message, in any spelling.

    Only runs of six digits or more are checked, because a short run such as `18` or `20`
    legitimately appears in a message that states the rule. At least one amount must be
    distinctive, or the check would be vacuous.
    """
    message = str(error)
    assert message, "a refusal with no message"
    flattened = message.replace(".", "").replace(",", "")
    distinctive = [amount for amount in amounts if len(digits_of(amount)) >= 6]
    assert distinctive, "choose at least one amount distinctive enough to detect"
    for amount in distinctive:
        assert digits_of(amount) not in flattened, message
        assert format(amount, "f") not in message, message
        assert str(amount) not in message, message


#: Every amount field of every event, and a builder that puts a value into it.
AMOUNT_FIELDS: Final[list[tuple[str, Callable[[object], object]]]] = [
    ("Trade.quantity", lambda value: trade(quantity=value)),
    ("Trade.quote_quantity", lambda value: trade(quote_quantity=value)),
    ("Trade.fee_amount", lambda value: trade(fee_amount=value)),
    # Each adjustment field beside a partner that cannot make the total cost overflow, so
    # that these tests are about the amount rule and the R2 tests about the product.
    ("Adjustment.quantity", lambda value: adjustment(quantity=value, unit_cost=None)),
    ("Adjustment.unit_cost", lambda value: adjustment(unit_cost=value, quantity=Decimal("0.5"))),
    ("Transfer.quantity", lambda value: transfer(quantity=value)),
]
AMOUNT_IDS: Final = [name for name, _ in AMOUNT_FIELDS]
AMOUNT_BUILDERS: Final = [build for _, build in AMOUNT_FIELDS]


# --------------------------------------------------------------------------------------
# Every amount: a finite Decimal, at most 18 places and 20 integer digits
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("build", AMOUNT_BUILDERS, ids=AMOUNT_IDS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool"),
        pytest.param(1, id="int"),
        pytest.param(1.5, id="float"),
        pytest.param("1", id="str"),
    ],
)
def test_an_amount_that_is_not_a_decimal_is_a_type_error(
    build: Callable[[object], object], value: object
) -> None:
    """Refused before any conversion: `Decimal(0.1)` is not one tenth, and `True` is not 1."""
    with pytest.raises(TypeError):
        build(value)


@pytest.mark.parametrize("build", AMOUNT_BUILDERS, ids=AMOUNT_IDS)
@pytest.mark.parametrize("value", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
def test_an_amount_that_is_not_finite_is_refused(
    build: Callable[[object], object], value: str
) -> None:
    with pytest.raises(ValueError, match=r"."):
        build(Decimal(value))


#: 19 fractional digits, none of them trailing zeros: finer than the 18-place grid.
NINETEEN_PLACES: Final = Decimal("0.3141592653589793238")
#: 21 integer digits: past `MONEY_PRECISION - 18`.
TWENTY_ONE_DIGITS: Final = Decimal("314159265358979323846")


@pytest.mark.parametrize("build", AMOUNT_BUILDERS, ids=AMOUNT_IDS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(NINETEEN_PLACES, id="19 places"),
        pytest.param(TWENTY_ONE_DIGITS, id="21 integer digits"),
        pytest.param(Decimal("3.14159265358979323846E+20"), id="21 digits in exponent form"),
    ],
)
def test_an_amount_off_the_storable_grid_is_refused_without_quoting_it(
    build: Callable[[object], object], value: Decimal
) -> None:
    """The rule `NormalizedFill` applies, so a stored fill always converts and nothing else does."""
    with pytest.raises(ValueError, match=r".") as caught:
        build(value)

    assert_amounts_not_quoted(caught.value, value)


@pytest.mark.parametrize("build", AMOUNT_BUILDERS, ids=AMOUNT_IDS)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Decimal("1.0000000000000000000"), id="19 places, trailing zero"),
        pytest.param(Decimal("99999999999999999999"), id="20 integer digits"),
        pytest.param(Decimal("0.000000000000000001"), id="one unit at 18 places"),
        pytest.param(Decimal("99999999999999999999.999999999999999999"), id="the widest"),
    ],
)
def test_an_amount_on_the_grid_is_accepted(
    build: Callable[[object], object], value: Decimal
) -> None:
    """R9: the rule is by value. Trailing zeros are a spelling, not precision."""
    build(value)


# --------------------------------------------------------------------------------------
# Signs
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda value: trade(quantity=value), id="Trade.quantity"),
        pytest.param(lambda value: trade(quote_quantity=value), id="Trade.quote_quantity"),
        pytest.param(lambda value: adjustment(quantity=value), id="Adjustment.quantity"),
        pytest.param(lambda value: transfer(quantity=value), id="Transfer.quantity"),
    ],
)
@pytest.mark.parametrize("value", ["0", "-0", "0E-18", "-1", "-0.000000000000000001"])
def test_a_quantity_must_be_greater_than_zero(
    build: Callable[[object], object], value: str
) -> None:
    with pytest.raises(ValueError, match=r"."):
        build(Decimal(value))


@pytest.mark.parametrize("value", ["-1", "-0.000000000000000001"])
def test_a_unit_cost_below_zero_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        adjustment(unit_cost=Decimal(value))


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-0"), None])
def test_a_unit_cost_of_zero_or_unknown_is_accepted(value: Decimal | None) -> None:
    """Zero is a known cost of nothing; `None` is unknown, which the spec says is not zero."""
    built = adjustment(unit_cost=value)

    assert built.unit_cost == value
    if value is None:
        assert built.unit_cost is None


def test_a_fee_may_be_negative() -> None:
    """`fee_amount` is signed: a rebate."""
    assert trade(fee_amount=Decimal("-30")).fee_amount == Decimal("-30")


# --------------------------------------------------------------------------------------
# EventKey
# --------------------------------------------------------------------------------------


def test_a_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match=r"."):
        key(occurred_at=datetime(2026, 1, 1, 10, 0))  # noqa: DTZ001 - the point of the test


class _NoOffset(tzinfo):
    """A `tzinfo` that answers `None`: set, and still naive by the standard library's test."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


def test_a_tzinfo_without_an_offset_is_naive() -> None:
    with pytest.raises(ValueError, match=r"."):
        key(occurred_at=datetime(2026, 1, 1, 10, 0, tzinfo=_NoOffset()))


def test_an_instant_utc_cannot_hold_is_a_value_error() -> None:
    """`datetime.min` at +05:00 is five hours before any `datetime`: a ValueError, not Overflow."""
    earliest = datetime.min.replace(tzinfo=timezone(timedelta(hours=5)))

    with pytest.raises(ValueError, match=r"."):
        key(occurred_at=earliest)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026-01-01T10:00:00Z", id="str"),
        pytest.param(date(2026, 1, 1), id="date"),
        pytest.param(1767261600, id="epoch int"),
    ],
)
def test_an_occurred_at_that_is_not_a_datetime_is_a_type_error(value: object) -> None:
    with pytest.raises(TypeError):
        key(occurred_at=value)


def test_occurred_at_is_normalised_to_utc() -> None:
    """Written at -03:00, stored as the same instant at UTC, and equal to the UTC spelling."""
    buenos_aires = timezone(timedelta(hours=-3))
    local = key(occurred_at=datetime(2026, 1, 1, 7, 0, tzinfo=buenos_aires))

    assert local.occurred_at.utcoffset() == timedelta(0)
    assert local.occurred_at == WHEN
    assert local.occurred_at.tzinfo == UTC
    assert local == key()


@pytest.mark.parametrize("field", ["source", "external_id"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="spaces"),
        pytest.param("\t\n", id="whitespace"),
        pytest.param(LONE_SURROGATE, id="lone surrogate"),
        pytest.param("id-" + LONE_SURROGATE, id="surrogate inside text"),
    ],
)
def test_a_key_text_field_must_be_encodable_and_not_blank(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        key(**{field: value})


@pytest.mark.parametrize("field", ["source", "external_id"])
@pytest.mark.parametrize("value", [None, 12345, b"bitget"])
def test_a_key_text_field_that_is_not_a_str_is_a_type_error(field: str, value: object) -> None:
    with pytest.raises(TypeError):
        key(**{field: value})


def test_events_are_frozen() -> None:
    built = trade()

    with pytest.raises(FrozenInstanceError):
        built.quantity = Decimal(2)  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# Trade
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["base_asset", "quote_asset", "fee_asset"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("  ", id="spaces"),
        pytest.param(LONE_SURROGATE, id="lone surrogate"),
        pytest.param("BTC" + LONE_SURROGATE, id="surrogate inside text"),
    ],
)
def test_a_trade_asset_must_be_encodable_and_not_blank(field: str, value: str) -> None:
    """`fee_asset` included, even beside a zero fee: R8 accepts a named asset, not a bad one."""
    with pytest.raises(ValueError, match=r"."):
        trade(**{field: value})
    if field == "fee_asset":
        with pytest.raises(ValueError, match=r"."):
            trade(fee_asset=value, fee_amount=Decimal(0))


@pytest.mark.parametrize("field", ["base_asset", "quote_asset", "fee_asset"])
def test_a_trade_asset_that_is_not_a_str_is_a_type_error(field: str) -> None:
    with pytest.raises(TypeError):
        trade(**{field: 42})


def test_a_trade_needs_two_different_assets() -> None:
    with pytest.raises(ValueError, match=r"."):
        trade(base_asset="USDT", quote_asset="USDT", fee_asset=None, fee_amount=Decimal(0))


@pytest.mark.parametrize("side", ["buy", "BUY", 0, None])
def test_a_side_that_is_not_a_fill_side_is_a_type_error(side: object) -> None:
    """A plain `"buy"` is refused too: the side is about the base asset, and that is the enum."""
    with pytest.raises(TypeError):
        trade(side=side)


def test_the_key_must_be_an_event_key() -> None:
    with pytest.raises(TypeError):
        trade(key=(WHEN, "bitget", "e1"))
    with pytest.raises(TypeError):
        adjustment(key=None)
    with pytest.raises(TypeError):
        transfer(key="bitget:e1")


@pytest.mark.parametrize("fee", ["30", "-30", "0.000000000000000001"])
def test_a_non_zero_fee_needs_an_asset(fee: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        trade(fee_amount=Decimal(fee), fee_asset=None)


@pytest.mark.parametrize("fee", ["0", "-0", "0.000", "0E-18"])
@pytest.mark.parametrize("asset", [None, "USDT", "BTC", "BGB"])
def test_r8_a_zero_fee_may_name_an_asset_or_none(fee: str, asset: str | None) -> None:
    """R8: `fee_asset` is required only when the fee is not zero, as `NormalizedFill` allows."""
    built = trade(fee_amount=Decimal(fee), fee_asset=asset)

    assert built.fee_asset == asset
    assert built.fee_amount.is_zero()


# The fee folds: a fee in the asset received must leave something received, and a rebate
# in the asset given must leave something given. Both sides of a BUY and of a SELL, at the
# boundary (exactly all of it) and past it.


#: A trade whose every amount is distinctive, so a leaked amount is detectable in a message.
DISTINCT: Final = {
    "quantity": Decimal("1.23456789"),
    "quote_quantity": Decimal("31415.926535"),
}


@pytest.mark.parametrize(
    ("side", "fee_asset", "fee"),
    [
        pytest.param(FillSide.BUY, "BTC", "1.23456789", id="buy, fee in base equals quantity"),
        pytest.param(FillSide.BUY, "BTC", "1.234567891", id="buy, fee past quantity"),
        pytest.param(FillSide.SELL, "USDT", "31415.926535", id="sell, fee in quote equals it"),
        pytest.param(FillSide.SELL, "USDT", "31415.9265351", id="sell, fee past proceeds"),
    ],
)
def test_a_fee_in_the_received_asset_must_leave_something_received(
    side: FillSide, fee_asset: str, fee: str
) -> None:
    with pytest.raises(ValueError, match=r".") as caught:
        trade(side=side, fee_asset=fee_asset, fee_amount=Decimal(fee), **DISTINCT)

    assert_amounts_not_quoted(caught.value, Decimal(fee), *DISTINCT.values())


@pytest.mark.parametrize(
    ("side", "fee_asset", "fee"),
    [
        pytest.param(FillSide.BUY, "BTC", "1.234567889999999999", id="buy, one unit received"),
        pytest.param(FillSide.SELL, "USDT", "31415.926534999999999999", id="sell, one unit"),
        pytest.param(FillSide.BUY, "BTC", "-5", id="buy, a rebate in the base"),
    ],
)
def test_a_fee_in_the_received_asset_just_under_it_is_accepted(
    side: FillSide, fee_asset: str, fee: str
) -> None:
    trade(side=side, fee_asset=fee_asset, fee_amount=Decimal(fee), **DISTINCT)


@pytest.mark.parametrize(
    ("side", "fee_asset", "fee"),
    [
        pytest.param(FillSide.BUY, "USDT", "-31415.926535", id="buy, rebate in quote equals cost"),
        pytest.param(FillSide.BUY, "USDT", "-31416.5", id="buy, rebate past cost"),
        pytest.param(FillSide.SELL, "BTC", "-1.23456789", id="sell, rebate in base equals it"),
        pytest.param(FillSide.SELL, "BTC", "-2.7182818", id="sell, rebate past quantity"),
    ],
)
def test_a_rebate_in_the_given_asset_must_leave_something_given(
    side: FillSide, fee_asset: str, fee: str
) -> None:
    with pytest.raises(ValueError, match=r".") as caught:
        trade(side=side, fee_asset=fee_asset, fee_amount=Decimal(fee), **DISTINCT)

    assert_amounts_not_quoted(caught.value, Decimal(fee), *DISTINCT.values())


@pytest.mark.parametrize(
    ("side", "fee_asset", "fee"),
    [
        pytest.param(FillSide.BUY, "USDT", "-31415.926534999999999999", id="buy, one unit given"),
        pytest.param(FillSide.SELL, "BTC", "-1.234567889999999999", id="sell, one unit given"),
        pytest.param(FillSide.BUY, "USDT", "1000000", id="buy, a fee far above the cost"),
    ],
)
def test_a_fee_in_the_given_asset_that_leaves_something_given_is_accepted(
    side: FillSide, fee_asset: str, fee: str
) -> None:
    trade(side=side, fee_asset=fee_asset, fee_amount=Decimal(fee), **DISTINCT)


def test_r7_a_third_asset_rebate_larger_than_the_trade_is_accepted() -> None:
    """R7: what counts as cash is configuration, so `Trade` cannot bound a third-asset rebate."""
    built = trade(quote_quantity=Decimal("10"), fee_amount=Decimal("-20"), fee_asset="USDC")

    assert built.fee_amount == Decimal("-20")


# --------------------------------------------------------------------------------------
# Adjustment, including R2
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param(" ", id="space"),
        pytest.param(LONE_SURROGATE, id="lone surrogate"),
    ],
)
def test_an_adjustment_asset_must_be_encodable_and_not_blank(value: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        adjustment(asset=value)


def test_an_adjustment_of_a_cash_asset_is_accepted() -> None:
    """Accepted, and changes nothing in replay (`test_replay.py`)."""
    adjustment(asset="USDT", unit_cost=Decimal("1"))


@pytest.mark.parametrize(
    ("quantity", "unit_cost"),
    [
        pytest.param("10000000000000000000", "10000000000000000000", id="1E19 at 1E19"),
        pytest.param("10", "10000000000000000000", id="exactly 10**20"),
        pytest.param("3", "33333333333333333333.333333333333333334", id="just past, by one unit"),
        pytest.param(
            "1.000000000000000001",
            "99999999999999999999.999999999999999999",
            id="rounds up past the ceiling",
        ),
    ],
)
def test_r2_an_adjustment_cost_that_cannot_be_represented_is_refused(
    quantity: str, unit_cost: str
) -> None:
    """R2: each amount passes its own rule, and their total would raise out of `replay`.

    Refused here, where the owner can correct the entry, with no amount in the message.
    """
    with pytest.raises(ValueError, match=r".") as caught:
        adjustment(quantity=Decimal(quantity), unit_cost=Decimal(unit_cost))

    assert_amounts_not_quoted(caught.value, Decimal(quantity), Decimal(unit_cost))


@pytest.mark.parametrize(
    ("quantity", "unit_cost"),
    [
        pytest.param("3", "33333333333333333333.333333333333333333", id="the widest cost"),
        pytest.param("0.000000000000000001", "99999999999999999999", id="a dust quantity"),
        pytest.param("99999999999999999999", "0", id="free"),
    ],
)
def test_r2_an_adjustment_cost_that_fits_is_accepted(quantity: str, unit_cost: str) -> None:
    adjustment(quantity=Decimal(quantity), unit_cost=Decimal(unit_cost))


def test_r2_does_not_apply_to_an_unknown_cost() -> None:
    """With no cost there is no product to overflow, whatever the quantity."""
    adjustment(quantity=Decimal("99999999999999999999"), unit_cost=None)


# --------------------------------------------------------------------------------------
# Transfer
# --------------------------------------------------------------------------------------


def test_a_transfer_between_the_same_location_is_refused() -> None:
    with pytest.raises(ValueError, match=r"."):
        transfer(from_location="bitget", to_location="bitget")


@pytest.mark.parametrize("field", ["asset", "from_location", "to_location"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("  ", id="spaces"),
        pytest.param(LONE_SURROGATE, id="lone surrogate"),
    ],
)
def test_a_transfer_text_field_must_be_encodable_and_not_blank(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        transfer(**{field: value})


@pytest.mark.parametrize("field", ["asset", "from_location", "to_location"])
def test_a_transfer_text_field_that_is_not_a_str_is_a_type_error(field: str) -> None:
    with pytest.raises(TypeError):
        transfer(**{field: None})


# --------------------------------------------------------------------------------------
# AccountingConfig
# --------------------------------------------------------------------------------------


def test_the_default_cash_assets_are_usdc_and_usdt() -> None:
    assert frozenset({"USDC", "USDT"}) == DEFAULT_CASH_ASSETS
    assert AccountingConfig().cash_assets == DEFAULT_CASH_ASSETS


def test_empty_cash_assets_are_refused() -> None:
    """With no cash asset there is no unit of account, and nothing can be valued."""
    with pytest.raises(ValueError, match=r"."):
        AccountingConfig(frozenset())


@pytest.mark.parametrize(
    "member",
    [
        pytest.param("", id="empty"),
        pytest.param(" ", id="space"),
        pytest.param(LONE_SURROGATE, id="lone surrogate"),
    ],
)
def test_a_blank_or_unencodable_cash_asset_is_refused(member: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        AccountingConfig(frozenset({"USDT", member}))


@pytest.mark.parametrize(
    "assets",
    [
        pytest.param({"USDT"}, id="a set"),
        pytest.param(["USDT"], id="a list"),
        pytest.param("USDT", id="a str"),
        pytest.param(frozenset({"USDT", 1}), id="a non-str member"),
    ],
)
def test_cash_assets_that_are_not_a_frozenset_of_str_are_a_type_error(assets: object) -> None:
    with pytest.raises(TypeError):
        AccountingConfig(assets)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Spec 020: one definition of an unaccountable shape, and Trade's messages kept
# --------------------------------------------------------------------------------------
#
# `trade_shape_problem` is the single definition `Trade` and `NormalizedFill` both refuse
# with. It validates nothing -- the field rules are its precondition -- so every input here
# passes them, and every expected answer is written by hand beside it.

#: Trade's three shape messages, copied by hand from `events.py` at badf77d, before spec 020
#: moved the rules into `trade_shape_problem`. Nothing pinned them until now: every Trade
#: refusal test above matches `r"."`, so a reworded message would have passed unnoticed.
SAME_ASSET_MESSAGE: Final = "Trade.base_asset and Trade.quote_asset must be different assets"
FEE_CONSUMES_RECEIVED_MESSAGE: Final = (
    "Trade.fee_amount, paid in the asset received, must leave a quantity received greater than zero"
)
REBATE_EXCEEDS_GIVEN_MESSAGE: Final = (
    "Trade.fee_amount, rebated in the asset given, must leave a quantity given greater than zero"
)


def shape_of(**overrides: object) -> TradeShapeProblem | None:
    """`trade_shape_problem` of a buy of `DISTINCT`, no fee, with `overrides` applied."""
    fields: dict[str, object] = {
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "side": FillSide.BUY,
        "quantity": DISTINCT["quantity"],
        "quote_quantity": DISTINCT["quote_quantity"],
        "fee_amount": Decimal(0),
        "fee_asset": None,
    }
    fields.update(overrides)
    return trade_shape_problem(**fields)  # type: ignore[arg-type]


def test_the_shape_problems_are_the_pinned_set() -> None:
    """Their values are what `NormalizedFill` and `Trade` look their messages up by."""
    assert issubclass(TradeShapeProblem, StrEnum)
    assert {member.name: member.value for member in TradeShapeProblem} == {
        "SAME_ASSET": "same_asset",
        "FEE_CONSUMES_RECEIVED": "fee_consumes_received",
        "REBATE_EXCEEDS_GIVEN": "rebate_exceeds_given",
    }


def test_trade_shape_problem_takes_keywords_only() -> None:
    """Seven arguments, two of them quantities and two of them assets: positions would swap."""
    parameters = inspect.signature(trade_shape_problem).parameters.values()

    assert [parameter.name for parameter in parameters] == [
        "base_asset",
        "quote_asset",
        "side",
        "quantity",
        "quote_quantity",
        "fee_amount",
        "fee_asset",
    ]
    assert all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters)


@pytest.mark.parametrize("side", [FillSide.BUY, FillSide.SELL])
@pytest.mark.parametrize(
    ("fee_amount", "fee_asset"),
    [
        pytest.param("0", None, id="no fee"),
        pytest.param("0", "USDT", id="a zero fee naming it"),
        pytest.param("31415.926535", "USDT", id="a fee that would consume a side"),
        pytest.param("-31415.926535", "USDT", id="a rebate that would empty a side"),
        pytest.param("5", "BNB", id="a fee in a third asset"),
    ],
)
def test_the_same_asset_is_a_shape_problem_whatever_the_fee(
    side: FillSide, fee_amount: str, fee_asset: str | None
) -> None:
    """Checked first: a fee that would also consume a side is still `SAME_ASSET`."""
    assert (
        shape_of(
            base_asset="USDT",
            quote_asset="USDT",
            side=side,
            fee_amount=Decimal(fee_amount),
            fee_asset=fee_asset,
        )
        is TradeShapeProblem.SAME_ASSET
    )


@pytest.mark.parametrize(
    ("base_asset", "quote_asset"),
    [
        pytest.param("BTC", "btc", id="case"),
        pytest.param("BTC", " BTC", id="a leading space"),
        pytest.param(
            "\N{LATIN SMALL LETTER E WITH ACUTE}TH",
            "e\N{COMBINING ACUTE ACCENT}TH",
            id="composed and decomposed",
        ),
    ],
)
def test_assets_are_compared_exactly(base_asset: str, quote_asset: str) -> None:
    assert shape_of(base_asset=base_asset, quote_asset=quote_asset) is None


@pytest.mark.parametrize(
    ("side", "fee_asset", "fee"),
    [
        pytest.param(FillSide.BUY, "BTC", "1.23456789", id="buy, fee in base equal to quantity"),
        pytest.param(FillSide.BUY, "BTC", "1.234567890000000001", id="buy, one unit past"),
        pytest.param(FillSide.BUY, "BTC", "99999999999999999999", id="buy, far past"),
        pytest.param(FillSide.SELL, "USDT", "31415.926535", id="sell, fee in quote equal to it"),
        pytest.param(FillSide.SELL, "USDT", "31415.926535000000000001", id="sell, one unit past"),
        pytest.param(FillSide.SELL, "USDT", "3.2E+4", id="sell, past, in exponent form"),
        pytest.param(
            FillSide.BUY, "BTC", "1.234567890000000000000000", id="buy, equal in a longer spelling"
        ),
    ],
)
def test_a_fee_that_consumes_what_was_received_is_a_shape_problem(
    side: FillSide, fee_asset: str, fee: str
) -> None:
    assert (
        shape_of(side=side, fee_asset=fee_asset, fee_amount=Decimal(fee))
        is TradeShapeProblem.FEE_CONSUMES_RECEIVED
    )


@pytest.mark.parametrize(
    ("side", "fee_asset", "fee"),
    [
        pytest.param(FillSide.BUY, "USDT", "-31415.926535", id="buy, rebate in quote equals cost"),
        pytest.param(FillSide.BUY, "USDT", "-31415.926535000000000001", id="buy, one unit past"),
        pytest.param(FillSide.SELL, "BTC", "-1.23456789", id="sell, rebate in base equals it"),
        pytest.param(FillSide.SELL, "BTC", "-1.234567890000000001", id="sell, one unit past"),
        pytest.param(FillSide.SELL, "BTC", "-99999999999999999999", id="sell, far past"),
    ],
)
def test_a_rebate_that_empties_what_was_given_is_a_shape_problem(
    side: FillSide, fee_asset: str, fee: str
) -> None:
    assert (
        shape_of(side=side, fee_asset=fee_asset, fee_amount=Decimal(fee))
        is TradeShapeProblem.REBATE_EXCEEDS_GIVEN
    )


@pytest.mark.parametrize(
    ("side", "fee_asset", "fee"),
    [
        # One unit inside each boundary.
        pytest.param(FillSide.BUY, "BTC", "1.234567889999999999", id="buy, fee one unit under"),
        pytest.param(FillSide.SELL, "USDT", "31415.926534999999999999", id="sell, fee one under"),
        pytest.param(FillSide.BUY, "USDT", "-31415.926534999999999999", id="buy, rebate one under"),
        pytest.param(FillSide.SELL, "BTC", "-1.234567889999999999", id="sell, rebate one under"),
        # R8: a zero fee is no leg, whichever asset it names and however zero is spelled.
        pytest.param(FillSide.BUY, "BTC", "0", id="buy, zero naming the received"),
        pytest.param(FillSide.SELL, "USDT", "-0", id="sell, negative zero naming the received"),
        pytest.param(FillSide.BUY, "USDT", "0E-18", id="buy, 0E-18 naming the given"),
        pytest.param(FillSide.SELL, "BNB", "0.000", id="sell, zero naming a third"),
        pytest.param(FillSide.BUY, None, "0", id="no fee at all"),
        # The two positions no size of fee can empty.
        pytest.param(FillSide.BUY, "BTC", "-99999999999999999999", id="buy, rebate in received"),
        pytest.param(FillSide.SELL, "BTC", "99999999999999999999", id="sell, fee in given"),
        # R7: a third asset constrains neither leg.
        pytest.param(FillSide.BUY, "BNB", "99999999999999999999", id="buy, fee in a third"),
        pytest.param(FillSide.SELL, "USDC", "-99999999999999999999", id="sell, rebate in a third"),
    ],
)
def test_every_other_fee_is_no_shape_problem(
    side: FillSide, fee_asset: str | None, fee: str
) -> None:
    assert shape_of(side=side, fee_asset=fee_asset, fee_amount=Decimal(fee)) is None


def test_a_non_zero_fee_without_an_asset_is_no_shape_problem() -> None:
    """A field rule each caller refuses first, so the shape check leaves it alone."""
    assert shape_of(fee_amount=Decimal("1.23456789"), fee_asset=None) is None


def test_the_comparison_is_by_value_however_long_the_spelling() -> None:
    """`1` followed by a point and ten thousand zeros is one, and one unit is one unit."""
    long_one = Decimal("1." + "0" * 10_000)

    assert shape_of(quantity=Decimal(1), fee_amount=long_one, fee_asset="BTC") is (
        TradeShapeProblem.FEE_CONSUMES_RECEIVED
    )
    assert (
        shape_of(quantity=long_one, fee_amount=Decimal("0.999999999999999999"), fee_asset="BTC")
        is None
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"base_asset": "USDT", "fee_asset": None, "fee_amount": Decimal(0)},
            SAME_ASSET_MESSAGE,
            id="the same asset",
        ),
        pytest.param(
            {"base_asset": "USDT", "fee_asset": "USDT", "fee_amount": Decimal("30000")},
            SAME_ASSET_MESSAGE,
            id="the same asset, with a fee that would consume a side",
        ),
        pytest.param(
            {"side": FillSide.BUY, "fee_asset": "BTC", "fee_amount": Decimal("1")},
            FEE_CONSUMES_RECEIVED_MESSAGE,
            id="buy, a fee in the base consuming it",
        ),
        pytest.param(
            {"side": FillSide.SELL, "fee_asset": "USDT", "fee_amount": Decimal("30000.5")},
            FEE_CONSUMES_RECEIVED_MESSAGE,
            id="sell, a fee in the quote past it",
        ),
        pytest.param(
            {"side": FillSide.BUY, "fee_asset": "USDT", "fee_amount": Decimal("-30000")},
            REBATE_EXCEEDS_GIVEN_MESSAGE,
            id="buy, a rebate in the quote equal to it",
        ),
        pytest.param(
            {"side": FillSide.SELL, "fee_asset": "BTC", "fee_amount": Decimal("-1.5")},
            REBATE_EXCEEDS_GIVEN_MESSAGE,
            id="sell, a rebate in the base past it",
        ),
    ],
)
def test_trade_keeps_its_shape_messages_word_for_word(
    overrides: dict[str, object], message: str
) -> None:
    """Spec 020 moved the rules, not the words: `Trade` says exactly what it said before."""
    with pytest.raises(ValueError, match=r".") as caught:
        trade(**overrides)

    assert type(caught.value) is ValueError
    assert str(caught.value) == message


def test_a_bad_field_is_reported_before_the_shape() -> None:
    """The field rules are the shape check's precondition, and each keeps its own refusal."""
    with pytest.raises(TypeError):
        trade(base_asset="USDT", side="buy")
    with pytest.raises(ValueError, match="decimal places"):
        trade(base_asset="USDT", fee_amount=NINETEEN_PLACES)


#: Assets drawn from few enough that any two fields often name the same one.
SHAPE_ASSETS: Final = ("BTC", "USDT", "KAS")


@st.composite
def shape_inputs(draw: st.DrawFn) -> dict[str, object]:
    """Field-valid trade inputs aimed at the three shapes' boundaries.

    The same asset one time in six; otherwise a fee in the asset received or given drawn one
    unit inside, at, or one unit past the leg it folds into, a fee or rebate of any size in
    any asset, or a zero fee naming any asset or none.
    """
    side = draw(st.sampled_from([FillSide.BUY, FillSide.SELL]))
    base, other = draw(st.permutations(SHAPE_ASSETS))[:2]
    quote = base if draw(st.sampled_from([False] * 5 + [True])) else other
    quantity = draw(amounts(maximum=10**19))
    quote_quantity = draw(amounts(maximum=10**19))
    if side is FillSide.BUY:
        received, given, received_asset, given_asset = quantity, quote_quantity, base, quote
    else:
        received, given, received_asset, given_asset = quote_quantity, quantity, quote, base
    around = draw(st.sampled_from(["received", "given", "free", "zero"]))
    fee_asset: str | None
    if around == "zero":
        fee = Decimal(draw(st.sampled_from(["0", "-0", "0E-18"])))
        fee_asset = draw(st.one_of(st.none(), st.sampled_from([*SHAPE_ASSETS, "BNB"])))
    elif around == "free":
        paid = draw(amounts(maximum=10**19))
        fee = paid.copy_negate() if draw(st.booleans()) else paid
        fee_asset = draw(st.sampled_from([*SHAPE_ASSETS, "BNB"]))
    else:
        leg = received if around == "received" else given
        units = to_units(leg) + draw(st.sampled_from([-1, 0, 1]))
        fee = from_units(units) if around == "received" else from_units(units).copy_negate()
        fee_asset = received_asset if around == "received" else given_asset
    return {
        "base_asset": base,
        "quote_asset": quote,
        "side": side,
        "quantity": quantity,
        "quote_quantity": quote_quantity,
        "fee_amount": fee,
        "fee_asset": fee_asset,
    }


@settings(max_examples=300, deadline=None)
@given(inputs=shape_inputs())
def test_trade_refuses_exactly_the_shapes_trade_shape_problem_names(
    inputs: dict[str, object],
) -> None:
    """One definition: `Trade` accepts field-valid inputs if and only if the function does.

    And when it refuses, it refuses with the message for the member the function returned,
    so the two cannot disagree about which shape a trade has.
    """
    problem = trade_shape_problem(**inputs)  # type: ignore[arg-type]
    messages = {
        TradeShapeProblem.SAME_ASSET: SAME_ASSET_MESSAGE,
        TradeShapeProblem.FEE_CONSUMES_RECEIVED: FEE_CONSUMES_RECEIVED_MESSAGE,
        TradeShapeProblem.REBATE_EXCEEDS_GIVEN: REBATE_EXCEEDS_GIVEN_MESSAGE,
    }

    if problem is None:
        Trade(key=key(), **inputs)  # type: ignore[arg-type]
    else:
        with pytest.raises(ValueError, match=r".") as caught:
            Trade(key=key(), **inputs)  # type: ignore[arg-type]
        assert str(caught.value) == messages[problem]
