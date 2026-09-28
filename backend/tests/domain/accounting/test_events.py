"""Spec 019, *Events*: every refusal, and R2, R8 and R9 from the rulings.

A bad event is a caller's defect, not a replay warning, so it is refused at construction with
a `ValueError` (a `TypeError` for a wrong type) and never reaches `replay`. Each rule below
is exercised on every field it applies to, because the failure worth catching is not a
missing rule but a rule applied to `quantity` and forgotten on `fee_amount`.

**No message quotes an amount.** A fill quantity is the owner's holdings, and a refusal ends
up in a log. Every amount refusal is checked against the digits of the value it refused.
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.domain.accounting import (
    DEFAULT_CASH_ASSETS,
    AccountingConfig,
    Adjustment,
    EventKey,
    Trade,
    Transfer,
)
from portfolio.domain.exchanges import FillSide

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
