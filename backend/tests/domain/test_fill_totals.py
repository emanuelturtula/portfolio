"""Spec 024's domain row: `total_fills` adds up any set of fills, exactly, in any order.

Pure and fast: no fixture, no database, no clock. Every expected figure is worked by hand
beside its literal, and a `Fraction` oracle -- which cannot round -- stands behind the
properties at the end.

What each block pins, against the issue's backend criteria:

* **every field** of the totals over a book mixing three quote assets;
* **nets are buys minus sells**, negative when a range is net selling, never clamped;
* **fees are summed with their sign** per fee asset; a zero fee with no asset adds and lists
  nothing, and a sum that nets to zero is still listed;
* **only a USDT-quoted fill has a USDT value**, the stored quote quantity, and each base
  asset says how many of its fills its USDT figures leave out (`usdt_unvalued_fill_count`);
* **sums are exact past 28 significant digits**, and the same sum with `+` under the
  interpreter's default context is shown to lose the last digit;
* **Hypothesis**: the totals of pages taken in any order and added up are the totals of the
  whole, and every figure equals the `Fraction` oracle's.
"""

from __future__ import annotations

import decimal
from dataclasses import asdict, replace
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from portfolio.domain.exchanges import FillSide
from portfolio.domain.fill_totals import (
    TOTALS_SCALE,
    USDT,
    AssetFillTotals,
    FeeTotal,
    FillLine,
    FillTotals,
    NotValuedInUsdtTotals,
    QuoteAssetFillTotals,
    UsdtFillTotals,
    total_fills,
    usdt_value,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from random import Random

BUY: Final = FillSide.BUY
SELL: Final = FillSide.SELL

#: The interpreter's own default precision. `portfolio.domain.money` raises the process-wide
#: default to 38 at import, so "the default context" has to be spelled out to be tested.
INTERPRETER_DEFAULT_PRECISION: Final = 28

#: 20 integer digits and 18 places: the widest amount a fill column stores, 38 digits.
WIDEST: Final = Decimal("12345678901234567890.123456789012345678")
ONE_UNIT: Final = Decimal("0.000000000000000001")


def line(
    base_asset: str = "BTC",
    quote_asset: str = USDT,
    side: FillSide = BUY,
    quantity: str = "1",
    quote_quantity: str = "100",
    fee_amount: str = "0",
    fee_asset: str | None = None,
) -> FillLine:
    """One `FillLine` from exact text. A zero fee with no asset unless one is given."""
    return FillLine(
        base_asset=base_asset,
        quote_asset=quote_asset,
        side=side,
        quantity=Decimal(quantity),
        quote_quantity=Decimal(quote_quantity),
        fee_amount=Decimal(fee_amount),
        fee_asset=fee_asset,
    )


def the_book() -> list[FillLine]:
    """The seven fills of `tests/fill_view_harness.py`, as the totals see them.

    The endpoint tests plant the same fills and expect the same figures on the wire, so the
    domain and the HTTP answer are held to one worked example.
    """
    return [
        line("BTC", USDT, BUY, "0.5", "30000", "0.0005", "BTC"),
        line("BTC", USDT, SELL, "0.2", "13000", "13", "USDT"),
        line("ETH", "BTC", BUY, "2", "0.1", "-0.002", "ETH"),
        line("BTC", "USDC", SELL, "0.1", "6100", "0", None),
        line("BTC", USDT, BUY, "0.3", "18000", "0.01", "BNB"),
        line("ETH", USDT, SELL, "1", "3000.123456789012345678", "-0.01", "BNB"),
        line("KAS", USDT, BUY, "1000", "100", "0.1", "USDT"),
    ]


def D(value: str) -> Decimal:  # noqa: N802 - a literal, spelled short so the tables read
    return Decimal(value)


def zero() -> Decimal:
    """Zero at the stored scale: what an empty figure is spelled as."""
    return Decimal((0, (0,), -TOTALS_SCALE))


# --------------------------------------------------------------------------------------
# Every field, over a book mixing three quote assets
# --------------------------------------------------------------------------------------


def test_every_field_of_the_book_is_worked_by_hand() -> None:
    """BTC: bought 0.8, sold 0.3; USDT 48000 out, 13000 in; one USDC sale left unvalued.

    ETH: 2 bought for BTC (unvalued), 1 sold for 3000.123456789012345678 USDT -- a figure that
    is not quantity x price, so the stored quote quantity is what the total has to carry.
    KAS: 1000 bought for 100 USDT. The per-quote block holds the BTC and USDC legs in their
    own assets; the fees keep their signs, and BNB nets to a listed zero.
    """
    totals = total_fills(the_book())

    assert totals == FillTotals(
        fill_count=7,
        by_asset=(
            AssetFillTotals(
                asset="BTC",
                fill_count=4,
                bought=D("0.8"),
                sold=D("0.3"),
                net=D("0.5"),
                usdt_spent=D("48000"),
                usdt_received=D("13000"),
                usdt_net=D("35000"),
                usdt_unvalued_fill_count=1,
            ),
            AssetFillTotals(
                asset="ETH",
                fill_count=2,
                bought=D("2"),
                sold=D("1"),
                net=D("1"),
                usdt_spent=D("0"),
                usdt_received=D("3000.123456789012345678"),
                usdt_net=D("-3000.123456789012345678"),
                usdt_unvalued_fill_count=1,
            ),
            AssetFillTotals(
                asset="KAS",
                fill_count=1,
                bought=D("1000"),
                sold=D("0"),
                net=D("1000"),
                usdt_spent=D("100"),
                usdt_received=D("0"),
                usdt_net=D("100"),
                usdt_unvalued_fill_count=0,
            ),
        ),
        usdt=UsdtFillTotals(
            spent=D("48100"),
            received=D("16000.123456789012345678"),
            net=D("32099.876543210987654322"),
        ),
        not_valued_in_usdt=NotValuedInUsdtTotals(
            fill_count=2,
            by_quote_asset=(
                QuoteAssetFillTotals(
                    quote_asset="BTC", fill_count=1, spent=D("0.1"), received=D("0"), net=D("0.1")
                ),
                QuoteAssetFillTotals(
                    quote_asset="USDC",
                    fill_count=1,
                    spent=D("0"),
                    received=D("6100"),
                    net=D("-6100"),
                ),
            ),
        ),
        fees=(
            FeeTotal(asset="BNB", amount=D("0")),
            FeeTotal(asset="BTC", amount=D("0.0005")),
            FeeTotal(asset="ETH", amount=D("-0.002")),
            FeeTotal(asset="USDT", amount=D("13.1")),
        ),
    )


def test_no_fills_is_zero_everywhere_and_lists_nothing() -> None:
    """The empty result: a count of zero, every list empty, and USDT figures of zero."""
    totals = total_fills([])

    assert totals == FillTotals(
        fill_count=0,
        by_asset=(),
        usdt=UsdtFillTotals(spent=D("0"), received=D("0"), net=D("0")),
        not_valued_in_usdt=NotValuedInUsdtTotals(fill_count=0, by_quote_asset=()),
        fees=(),
    )


def test_an_empty_figure_is_spelled_at_the_stored_scale() -> None:
    """`0E-18`, so an empty figure reaches the wire at eighteen places like its neighbours."""
    totals = total_fills([])

    for figure in (totals.usdt.spent, totals.usdt.received, totals.usdt.net):
        assert figure.as_tuple().exponent == -TOTALS_SCALE, repr(figure)
        assert not figure.is_signed(), "an empty net is zero, not negative zero"


def test_a_sum_of_shorter_amounts_is_still_at_the_stored_scale() -> None:
    """Amounts at one place still sum to an eighteen-place figure: the zero they start from."""
    totals = total_fills([line(quantity="0.5", quote_quantity="30000")])

    (btc,) = totals.by_asset
    assert btc.bought == D("0.5")
    assert btc.bought.as_tuple().exponent == -TOTALS_SCALE
    assert btc.sold.as_tuple().exponent == -TOTALS_SCALE
    assert totals.usdt.spent.as_tuple().exponent == -TOTALS_SCALE


def test_the_lists_are_sorted_whatever_order_the_fills_came_in() -> None:
    """`by_asset` by asset, `by_quote_asset` by quote asset, `fees` by asset."""
    reversed_order = [
        line("ZEC", "USDC", fee_amount="1", fee_asset="ZEC"),
        line("KAS", "EUR", fee_amount="1", fee_asset="KAS"),
        line("BTC", "BTCQ", fee_amount="1", fee_asset="BGB"),
        line("ADA", "AUD", fee_amount="1", fee_asset="ADA"),
    ]

    totals = total_fills(reversed_order)

    assert [row.asset for row in totals.by_asset] == ["ADA", "BTC", "KAS", "ZEC"]
    assert [row.quote_asset for row in totals.not_valued_in_usdt.by_quote_asset] == [
        "AUD",
        "BTCQ",
        "EUR",
        "USDC",
    ]
    assert [fee.asset for fee in totals.fees] == ["ADA", "BGB", "KAS", "ZEC"]


def test_quantities_are_never_summed_across_assets() -> None:
    """One BTC and one ETH are two rows of one, not one row of two."""
    totals = total_fills([line("BTC"), line("ETH")])

    assert [(row.asset, row.bought, row.fill_count) for row in totals.by_asset] == [
        ("BTC", D("1"), 1),
        ("ETH", D("1"), 1),
    ]
    assert totals.usdt.spent == D("200"), "only USDT is a common unit, so only USDT is summed"


# --------------------------------------------------------------------------------------
# Every net is buys minus sells, and may be negative
# --------------------------------------------------------------------------------------


def test_a_range_of_sells_has_negative_nets_everywhere() -> None:
    """Net selling: every net is buys minus sells, so every one is negative, and none clamped.

    BTC: 0 - 0.2 = -0.2; USDT: 0 - 13000; the USDC leg: spent 0 - received 6100.
    """
    totals = total_fills(
        [
            line("BTC", USDT, SELL, "0.2", "13000"),
            line("BTC", "USDC", SELL, "0.1", "6100"),
        ]
    )

    (btc,) = totals.by_asset
    assert (btc.bought, btc.sold, btc.net) == (D("0"), D("0.3"), D("-0.3"))
    assert (btc.usdt_spent, btc.usdt_received, btc.usdt_net) == (D("0"), D("13000"), D("-13000"))
    assert totals.usdt == UsdtFillTotals(spent=D("0"), received=D("13000"), net=D("-13000"))
    (usdc,) = totals.not_valued_in_usdt.by_quote_asset
    assert (usdc.spent, usdc.received, usdc.net) == (D("0"), D("6100"), D("-6100"))


def test_a_net_is_buys_minus_sells_not_sells_minus_buys() -> None:
    """Asymmetric figures, so a flipped sign cannot pass by coincidence."""
    totals = total_fills(
        [
            line("BTC", USDT, BUY, "3", "300"),
            line("BTC", USDT, SELL, "1", "150"),
            line("BTC", "EUR", BUY, "2", "40"),
            line("BTC", "EUR", SELL, "1", "15"),
        ]
    )

    (btc,) = totals.by_asset
    assert btc.net == D("3")  # 3 + 2 bought, 1 + 1 sold
    assert btc.usdt_net == D("150")  # 300 spent - 150 received
    assert totals.usdt.net == D("150")
    (eur,) = totals.not_valued_in_usdt.by_quote_asset
    assert (eur.spent, eur.received, eur.net) == (D("40"), D("15"), D("25"))


def test_a_net_of_exactly_zero_is_zero() -> None:
    """Bought and sold alike: zero, and not a negative zero."""
    totals = total_fills([line(side=BUY), line(side=SELL)])

    (btc,) = totals.by_asset
    assert btc.net == 0
    assert not btc.net.is_signed()
    assert not totals.usdt.net.is_signed()


# --------------------------------------------------------------------------------------
# Fees: signed, per fee asset, never converted
# --------------------------------------------------------------------------------------


def test_fees_are_summed_per_asset_with_their_sign() -> None:
    """A fee paid is positive and a rebate negative; the sum keeps both."""
    totals = total_fills(
        [
            line(fee_amount="0.5", fee_asset="BNB"),
            line(fee_amount="-0.2", fee_asset="BNB"),
            line(fee_amount="-0.002", fee_asset="ETH"),
            line(fee_amount="13", fee_asset=USDT),
        ]
    )

    assert totals.fees == (
        FeeTotal(asset="BNB", amount=D("0.3")),
        FeeTotal(asset="ETH", amount=D("-0.002")),
        FeeTotal(asset=USDT, amount=D("13")),
    )


def test_fees_are_never_folded_into_the_usdt_figures() -> None:
    """A USDT fee is a fee, listed as one; USDT spent is the quote quantity and nothing else."""
    totals = total_fills([line(quote_quantity="100", fee_amount="7", fee_asset=USDT)])

    assert totals.usdt.spent == D("100")
    assert totals.fees == (FeeTotal(asset=USDT, amount=D("7")),)


def test_a_fee_and_a_rebate_that_cancel_are_still_listed() -> None:
    """The owner paid fees in BNB; a sum that netted to nothing is still an answer."""
    totals = total_fills(
        [line(fee_amount="0.01", fee_asset="BNB"), line(fee_amount="-0.01", fee_asset="BNB")]
    )

    assert totals.fees == (FeeTotal(asset="BNB", amount=D("0")),)
    assert not totals.fees[0].amount.is_signed()


def test_a_zero_fee_with_no_asset_adds_nothing_and_lists_nothing() -> None:
    totals = total_fills([line(fee_amount="0", fee_asset=None)])

    assert totals.fees == ()
    assert totals.fill_count == 1, "the fill itself is still counted"


def test_a_zero_fee_naming_an_asset_lists_that_asset_at_zero() -> None:
    """Spec 024: an entry is listed when a fee in that asset occurred, whatever it summed to."""
    totals = total_fills([line(fee_amount="0", fee_asset="BGB")])

    assert totals.fees == (FeeTotal(asset="BGB", amount=D("0")),)


def test_a_fee_is_never_converted_or_added_to_another_asset() -> None:
    """Two fee assets, two entries; no USDT equivalent appears for the BNB one."""
    totals = total_fills(
        [line(fee_amount="1", fee_asset="BNB"), line(fee_amount="2", fee_asset=USDT)]
    )

    assert {fee.asset: fee.amount for fee in totals.fees} == {"BNB": D("1"), USDT: D("2")}


# --------------------------------------------------------------------------------------
# Only a USDT-quoted fill has a USDT value
# --------------------------------------------------------------------------------------


def test_a_usdt_value_is_the_stored_quote_quantity() -> None:
    """The stored figure itself, not a recomputation."""
    stored = D("3000.123456789012345678")

    assert usdt_value(USDT, stored) is stored
    assert usdt_value("USDC", stored) is None
    assert usdt_value("BTC", stored) is None


def test_a_quote_spelled_otherwise_is_not_usdt() -> None:
    """Compared exactly, as the venues spell it: `usdt` is some other asset, not a USDT value."""
    assert usdt_value("usdt", D("1")) is None
    assert usdt_value("USDT ", D("1")) is None


def test_a_non_usdt_fill_contributes_to_no_usdt_figure() -> None:
    """A USDC sale is counted, in USDC, under `not_valued_in_usdt` -- and nowhere in USDT."""
    totals = total_fills([line("BTC", "USDC", SELL, "0.1", "6100")])

    assert totals.usdt == UsdtFillTotals(spent=D("0"), received=D("0"), net=D("0"))
    (btc,) = totals.by_asset
    assert (btc.usdt_spent, btc.usdt_received, btc.usdt_net) == (D("0"), D("0"), D("0"))
    assert totals.not_valued_in_usdt == NotValuedInUsdtTotals(
        fill_count=1,
        by_quote_asset=(
            QuoteAssetFillTotals(
                quote_asset="USDC", fill_count=1, spent=D("0"), received=D("6100"), net=D("-6100")
            ),
        ),
    )


def test_a_usdt_fill_is_never_listed_as_not_valued() -> None:
    totals = total_fills([line("BTC", USDT, BUY, "1", "100")])

    assert totals.not_valued_in_usdt == NotValuedInUsdtTotals(fill_count=0, by_quote_asset=())


@pytest.mark.parametrize(
    ("quotes", "unvalued"),
    [
        ((USDT, USDT), 0),
        ((USDT, "USDC"), 1),
        (("USDC", "BTC", "EUR"), 3),
        ((USDT, "USDC", USDT, "EUR"), 2),
    ],
    ids=["all USDT", "one of two", "none in USDT", "two of four"],
)
def test_each_asset_says_how_many_of_its_fills_its_usdt_figures_leave_out(
    quotes: tuple[str, ...], unvalued: int
) -> None:
    """`usdt_unvalued_fill_count`: without it a row mixing quotes reads as fully valued."""
    totals = total_fills([line("ETH", quote) for quote in quotes])

    (eth,) = totals.by_asset
    assert eth.fill_count == len(quotes)
    assert eth.usdt_unvalued_fill_count == unvalued
    assert eth.usdt_spent == D("100") * (len(quotes) - unvalued)


def test_unvalued_counts_are_per_asset_not_shared() -> None:
    """A USDC fill of BTC leaves ETH's USDT figures complete."""
    totals = total_fills([line("BTC", "USDC"), line("ETH", USDT), line("ETH", "EUR")])

    assert {row.asset: row.usdt_unvalued_fill_count for row in totals.by_asset} == {
        "BTC": 1,
        "ETH": 1,
    }
    assert totals.not_valued_in_usdt.fill_count == 2


# --------------------------------------------------------------------------------------
# Exact past 28 significant digits
# --------------------------------------------------------------------------------------


def test_a_sum_needing_more_than_28_digits_is_exact() -> None:
    """20 integer digits and 18 places, plus one unit: 38 significant digits, every one kept."""
    totals = total_fills(
        [
            line(quantity=str(WIDEST), quote_quantity=str(WIDEST)),
            line(quantity=str(ONE_UNIT), quote_quantity=str(ONE_UNIT)),
        ]
    )

    expected = D("12345678901234567890.123456789012345679")
    (btc,) = totals.by_asset
    assert btc.bought == expected
    assert totals.usdt.spent == expected
    assert len(expected.as_tuple().digits) == 38


def test_the_same_sum_with_plus_under_the_default_context_loses_digits() -> None:
    """The control: `+` at the interpreter's 28 digits rounds the sum above, silently."""
    context = decimal.Context(prec=INTERPRETER_DEFAULT_PRECISION)

    naive = context.add(WIDEST, ONE_UNIT)

    assert naive != D("12345678901234567890.123456789012345679")
    assert naive == D("12345678901234567890.12345679")
    with decimal.localcontext(context):
        assert naive == WIDEST + ONE_UNIT, "a plain `+` in that context rounds the same way"


def test_the_totals_are_exact_inside_a_default_precision_context() -> None:
    """`total_fills` inside the interpreter's default context gives every digit anyway."""
    lines = [
        line(quantity=str(WIDEST), quote_quantity=str(WIDEST), fee_amount="1", fee_asset="BTC"),
        line(side=SELL, quantity=str(ONE_UNIT), quote_quantity=str(ONE_UNIT)),
        line(fee_amount=str(ONE_UNIT.copy_negate()), fee_asset="BTC"),
    ]

    with decimal.localcontext() as context:
        context.prec = INTERPRETER_DEFAULT_PRECISION
        totals = total_fills(lines)

    (btc,) = totals.by_asset
    assert btc.net == D("12345678901234567891.123456789012345677")
    assert totals.usdt.net == D("12345678901234567990.123456789012345677")
    assert totals.fees == (FeeTotal(asset="BTC", amount=D("0.999999999999999999")),)


def test_a_hostile_ambient_context_changes_nothing() -> None:
    """Six digits, rounding up, and any inexact operation trapped: the same totals.

    Trapping `Inexact` and `Rounded` turns any arithmetic done in the ambient context into an
    exception, even one whose answer happened to survive.
    """
    book = the_book()
    expected = total_fills(book)

    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_UP
        context.traps[decimal.Inexact] = True
        context.traps[decimal.Rounded] = True
        trapped = total_fills(book)

    assert trapped == expected


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


def test_a_side_that_is_neither_buy_nor_sell_is_refused() -> None:
    """Unreachable from a stored row (a `CHECK` holds the column), and refused if it happens."""
    stray = replace(line(), side=cast("FillSide", "hold"))

    with pytest.raises(AssertionError, match="unreachable"):
        total_fills([stray])


def test_an_amount_that_is_not_a_decimal_is_refused() -> None:
    """An `int` is refused rather than silently widened; a float never reaches this point."""
    stray = replace(line(), quantity=cast("Decimal", 1))

    with pytest.raises(TypeError, match="Decimal"):
        total_fills([stray])


def test_an_amount_that_is_not_finite_is_refused() -> None:
    stray = replace(line(), quote_quantity=D("NaN"))

    with pytest.raises(ValueError, match="NaN"):
        total_fills([stray])


def test_a_side_given_as_its_text_is_counted_like_the_enum() -> None:
    """`FillSide` is a `StrEnum`: the text `sell` is the sell side, not a stray value."""
    as_text = replace(line(quote_quantity="5"), side=cast("FillSide", "sell"))

    totals = total_fills([as_text])

    assert totals.usdt == UsdtFillTotals(spent=D("0"), received=D("5"), net=D("-5"))


# --------------------------------------------------------------------------------------
# Properties: any set of fills, any order, any pages
# --------------------------------------------------------------------------------------

BASES: Final = ("BTC", "ETH", "KAS")
QUOTES: Final = (USDT, "USDC", "BTC", "EUR")
FEE_ASSETS: Final = (None, "BNB", USDT, "BTC", "ETH")
#: The widest coefficient a fill column holds: 38 digits at 18 places.
MAX_COEFFICIENT: Final = 10**38 - 1

PROPERTY: Final = settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


def at_scale(units: int, places: int) -> Decimal:
    """`units` at `places` decimal places, built without any context."""
    sign = 1 if units < 0 else 0
    digits = tuple(int(character) for character in str(abs(units)))
    return Decimal((sign, digits, -places))


@st.composite
def fill_lines(draw: st.DrawFn) -> FillLine:
    """A valid line: positive amounts at 0 to 18 places, up to 38 digits; a signed fee."""
    base = draw(st.sampled_from(BASES))
    quote = draw(st.sampled_from([quote for quote in QUOTES if quote != base]))
    places = draw(st.sampled_from((0, 2, 8, 18)))
    quantity = at_scale(draw(st.integers(1, MAX_COEFFICIENT)), places)
    quote_quantity = at_scale(draw(st.integers(1, MAX_COEFFICIENT)), draw(st.sampled_from((0, 18))))
    fee_asset = draw(st.sampled_from(FEE_ASSETS))
    fee = (
        at_scale(draw(st.integers(-(10**30), 10**30)), 18) if fee_asset is not None else Decimal(0)
    )
    return FillLine(
        base_asset=base,
        quote_asset=quote,
        side=draw(st.sampled_from(FillSide)),
        quantity=quantity,
        quote_quantity=quote_quantity,
        fee_amount=fee,
        fee_asset=fee_asset,
    )


def exact(totals: FillTotals) -> dict[str, Any]:
    """The totals with every `Decimal` as a `Fraction`: comparable, and addable, exactly."""

    def convert(node: object) -> object:
        if isinstance(node, Decimal):
            return Fraction(node)
        if isinstance(node, dict):
            return {key: convert(value) for key, value in node.items()}
        if isinstance(node, list | tuple):
            return [convert(item) for item in node]
        return node

    converted = convert(asdict(totals))
    assert isinstance(converted, dict)
    return converted


def keyed(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {row[key]: row for row in rows}


def merged(parts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The totals of several disjoint pages, added up field by field, in `Fraction`s.

    Written in the test, not taken from the code under test, so that "the pages add up to the
    whole" is checked against an addition that cannot share a bug with `total_fills`.
    """

    def add_rows(rows: list[list[dict[str, Any]]], key: str) -> list[dict[str, Any]]:
        combined: dict[str, dict[str, Any]] = {}
        for page_rows in rows:
            for name, row in keyed(page_rows, key).items():
                if name not in combined:
                    combined[name] = dict(row)
                    continue
                for field, value in row.items():
                    if field != key:
                        combined[name][field] += value
        return [combined[name] for name in sorted(combined)]

    return {
        "fill_count": sum(part["fill_count"] for part in parts),
        "by_asset": add_rows([part["by_asset"] for part in parts], "asset"),
        "usdt": {
            field: sum((part["usdt"][field] for part in parts), Fraction(0))
            for field in ("spent", "received", "net")
        },
        "not_valued_in_usdt": {
            "fill_count": sum(part["not_valued_in_usdt"]["fill_count"] for part in parts),
            "by_quote_asset": add_rows(
                [part["not_valued_in_usdt"]["by_quote_asset"] for part in parts], "quote_asset"
            ),
        },
        "fees": add_rows([part["fees"] for part in parts], "asset"),
    }


def oracle(lines: Sequence[FillLine]) -> dict[str, Any]:
    """Every figure straight from the definition, in `Fraction`s, which cannot round."""
    zero_f = Fraction(0)
    assets: dict[str, dict[str, Any]] = {}
    quotes: dict[str, dict[str, Any]] = {}
    fees: dict[str, Fraction] = {}
    usdt = {"spent": zero_f, "received": zero_f}
    for item in lines:
        buying = item.side == FillSide.BUY
        asset = assets.setdefault(
            item.base_asset,
            {
                "fill_count": 0,
                "bought": zero_f,
                "sold": zero_f,
                "spent": zero_f,
                "received": zero_f,
                "valued": 0,
            },
        )
        asset["fill_count"] += 1
        asset["bought" if buying else "sold"] += Fraction(item.quantity)
        if item.quote_asset == "USDT":
            asset["valued"] += 1
            asset["spent" if buying else "received"] += Fraction(item.quote_quantity)
            usdt["spent" if buying else "received"] += Fraction(item.quote_quantity)
        else:
            quote = quotes.setdefault(
                item.quote_asset, {"fill_count": 0, "spent": zero_f, "received": zero_f}
            )
            quote["fill_count"] += 1
            quote["spent" if buying else "received"] += Fraction(item.quote_quantity)
        if item.fee_asset is not None:
            fees[item.fee_asset] = fees.get(item.fee_asset, zero_f) + Fraction(item.fee_amount)
    return {
        "fill_count": len(lines),
        "by_asset": [
            {
                "asset": name,
                "fill_count": row["fill_count"],
                "bought": row["bought"],
                "sold": row["sold"],
                "net": row["bought"] - row["sold"],
                "usdt_spent": row["spent"],
                "usdt_received": row["received"],
                "usdt_net": row["spent"] - row["received"],
                "usdt_unvalued_fill_count": row["fill_count"] - row["valued"],
            }
            for name, row in sorted(assets.items())
        ],
        "usdt": {
            "spent": usdt["spent"],
            "received": usdt["received"],
            "net": usdt["spent"] - usdt["received"],
        },
        "not_valued_in_usdt": {
            "fill_count": sum(row["fill_count"] for row in quotes.values()),
            "by_quote_asset": [
                {
                    "quote_asset": name,
                    "fill_count": row["fill_count"],
                    "spent": row["spent"],
                    "received": row["received"],
                    "net": row["spent"] - row["received"],
                }
                for name, row in sorted(quotes.items())
            ],
        },
        "fees": [{"asset": name, "amount": amount} for name, amount in sorted(fees.items())],
    }


def pages_of(lines: Sequence[FillLine], size: int) -> list[list[FillLine]]:
    return [list(lines[start : start + size]) for start in range(0, len(lines), size)]


@PROPERTY
@given(
    lines=st.lists(fill_lines(), max_size=40),
    size=st.integers(1, 12),
    shuffler=st.randoms(use_true_random=False),
)
def test_the_totals_of_the_pages_added_up_are_the_totals_of_the_whole(
    lines: list[FillLine], size: int, shuffler: Random
) -> None:
    """Page the fills, total each page, and add the pages up: the whole's totals, exactly.

    The pages are also concatenated in a shuffled order and totalled as one set, which must
    give the same answer again: nothing about the totals depends on which page came first.
    """
    pages = pages_of(lines, size)
    whole = total_fills(lines)

    assert merged([exact(total_fills(page)) for page in pages]) == exact(whole)
    shuffled = list(pages)
    shuffler.shuffle(shuffled)
    assert total_fills([item for page in shuffled for item in page]) == whole


@PROPERTY
@given(lines=st.lists(fill_lines(), max_size=40))
def test_every_figure_equals_the_fraction_oracle(lines: list[FillLine]) -> None:
    """Exact at any length: 38-digit amounts, summed forty at a time, never rounded."""
    assert exact(total_fills(lines)) == oracle(lines)


@PROPERTY
@given(lines=st.lists(fill_lines(), max_size=40))
def test_the_figures_agree_with_each_other(lines: list[FillLine]) -> None:
    """The cross-checks a reader of the page would make, over any set of fills."""
    totals = total_fills(lines)

    assert totals.fill_count == len(lines) == sum(row.fill_count for row in totals.by_asset)
    unvalued = sum(row.usdt_unvalued_fill_count for row in totals.by_asset)
    assert unvalued == totals.not_valued_in_usdt.fill_count
    assert totals.not_valued_in_usdt.fill_count == sum(
        row.fill_count for row in totals.not_valued_in_usdt.by_quote_asset
    )
    assert Fraction(totals.usdt.spent) == sum(
        (Fraction(row.usdt_spent) for row in totals.by_asset), Fraction(0)
    )
    assert Fraction(totals.usdt.received) == sum(
        (Fraction(row.usdt_received) for row in totals.by_asset), Fraction(0)
    )
    for row in totals.by_asset:
        assert Fraction(row.net) == Fraction(row.bought) - Fraction(row.sold)
        assert Fraction(row.usdt_net) == Fraction(row.usdt_spent) - Fraction(row.usdt_received)
        assert 0 <= row.usdt_unvalued_fill_count <= row.fill_count
    assert Fraction(totals.usdt.net) == Fraction(totals.usdt.spent) - Fraction(totals.usdt.received)
    listed = {item.fee_asset for item in lines if item.fee_asset is not None}
    assert [fee.asset for fee in totals.fees] == sorted(listed)
