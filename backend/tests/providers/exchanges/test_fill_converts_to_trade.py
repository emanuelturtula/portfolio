"""Spec 020, criterion 2: every fill `NormalizedFill` accepts converts to a `Trade`.

The fill log is append-only, and #19 builds a `Trade` from every row in it. One row that
does not convert stops every position from being computed, so the claim worth holding is not
"the three known shapes are refused" but what it generalises to: **whatever `NormalizedFill`
lets through, `Trade` accepts.** The conversion is written inline here, field for field,
because the mapper is #19's (spec 020, *Non-goals*).

**Candidates are drawn, not fills.** A candidate is a set of field values that may or may
not satisfy `NormalizedFill`. The ones it refuses are rejected, and every one it accepts must
convert. So the strategies aim at the edges of *both* types' rules, which is where a gap
between them would be:

* amounts at 0 to 18 places and 0 to 20 integer digits, one unit (1E-18), the widest amount,
  trailing zeros past the 18th place and exponent spellings, and now and then one off the
  grid, which both must refuse;
* every fee position: none, a zero fee naming any asset (R8) in several spellings of zero,
  and a fee or a rebate in the received, the given or a third asset. The ones that fold into
  a leg are drawn well inside it, one unit inside it, exactly at it, one unit past it and far
  past it;
* a base asset equal to the quote asset, drawn on purpose and not left to chance;
* `executed_at` anywhere from `datetime.min` to `datetime.max`: at UTC, at fixed offsets up
  to a microsecond short of a day either way, and at a `tzinfo` whose offset depends on the
  date. That includes the instants at the ends of the calendar that have no UTC spelling
  (spec 020, R1);
* text at the edges of the UTF-8 rule: non-ASCII, composed and decomposed accents,
  whitespace inside and around, a zero-width space and a NUL (neither is blank to
  `str.strip`), and now and then blank text or a lone surrogate, which both must refuse.

`test_the_candidates_reach` proves each of those is generated rather than trusting this
docstring, and the explicit examples on the property pin every known shape so that a
regression fails on every run, not only on the runs whose draw happens to find it.
"""

from __future__ import annotations

import contextlib
import decimal
import functools
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import HealthCheck, Phase, example, find, given, reject, settings
from hypothesis import strategies as st
from hypothesis.errors import NoSuchExample

from portfolio.domain.accounting import EventKey, Trade
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.providers.exchanges.base import NormalizedFill
from portfolio.providers.exchanges.errors import ExchangeSchemaError

if TYPE_CHECKING:
    from collections.abc import Callable

# --------------------------------------------------------------------------------------
# The grid
# --------------------------------------------------------------------------------------

UNIT: Final = Decimal("0.000000000000000001")
"""One unit at 18 places: the smallest amount a fill may carry."""
WIDEST: Final = Decimal("99999999999999999999.999999999999999999")
"""Twenty integer digits and eighteen places: the largest amount a fill may carry."""
_UNIT_FRACTION: Final = Fraction(1, 10**18)
#: A context wide enough that `normalize` never rounds a padded 44-digit spelling.
_WIDE: Final = decimal.Context(prec=100)

#: Amounts `NormalizedFill` and `Trade` must both refuse: finer than the grid or wider.
OFF_GRID: Final = (
    Decimal("0.0000000000000000001"),
    Decimal("1.0000000000000000001"),
    Decimal("100000000000000000000"),
    Decimal("1E+20"),
    Decimal("123456789012345678901.5"),
)

#: Zero, spelled the ways a venue or a parser might: a zero fee is no leg in any of them.
ZEROS: Final = ("-0", "0", "0.000", "0E-18", "-0E-30", "0E+5")


def from_units(units: int) -> Decimal:
    """`units` millionths-of-a-trillionth: an exact decimal on the 18-place grid."""
    return Decimal(f"{units}E-18")


def to_units(value: Decimal) -> int:
    """The number of 18-place units in `value`, which must be on the grid."""
    scaled = Fraction(value) / _UNIT_FRACTION
    assert scaled.denominator == 1, value
    return scaled.numerator


@st.composite
def grid_amounts(draw: st.DrawFn) -> Decimal:
    """A positive amount on the grid, in one of the spellings a venue sends.

    Built from its integer digits and its places, 0 to 20 and 0 to 18, so that narrow and
    wide amounts are both common, where a uniform draw of units would make nearly every amount
    twenty digits wide; one unit and the widest amount are drawn on purpose, because they are
    the edges. The spelling is then kept, padded
    with trailing zeros past the 18th place, or normalised to exponent form: the rule is by
    value, and both types must agree on it whatever the spelling.
    """
    shape = draw(st.sampled_from(["drawn"] * 6 + ["unit", "widest", "whole"]))
    if shape == "unit":
        value = UNIT
    elif shape == "widest":
        value = WIDEST
    else:
        integer_digits = draw(st.integers(min_value=0, max_value=20))
        places = 0 if shape == "whole" else draw(st.integers(min_value=0, max_value=18))
        whole = (
            draw(
                st.integers(min_value=10 ** (integer_digits - 1), max_value=10**integer_digits - 1)
            )
            if integer_digits
            else 0
        )
        fraction = draw(st.integers(min_value=0, max_value=10**places - 1)) if places else 0
        value = Decimal(f"{whole}.{fraction:0{places}d}") if places else Decimal(whole)
        if value.is_zero():
            value = Decimal(f"1E-{places}") if places else Decimal(1)
    spelling = draw(st.sampled_from(["as built"] * 4 + ["padded", "exponent"]))
    if spelling == "padded":
        written = format(value, "f")
        padding = "0" * draw(st.integers(min_value=1, max_value=6))
        return Decimal(f"{written}{padding}" if "." in written else f"{written}.{padding}")
    if spelling == "exponent":
        return value.normalize(_WIDE)
    return value


def near(draw: st.DrawFn, amount: Decimal) -> Decimal:
    """An amount well inside `amount`, one unit inside it, at it, one unit past it or far past.

    "At it" is sometimes `amount` itself and sometimes the same value in another spelling,
    because the comparison is by value.
    """
    units = to_units(amount)
    # Hypothesis leans towards the first element of a `sampled_from`, so the edges lead.
    where = draw(st.sampled_from(["at", "one inside", "one past", "inside", "far past", "inside"]))
    if where == "inside" and units > 1:
        return from_units(draw(st.integers(min_value=1, max_value=units - 1)))
    if where == "one inside" and units > 1:
        return from_units(units - 1)
    if where == "one past":
        return from_units(units + 1)
    if where == "far past":
        return from_units(units + draw(st.integers(min_value=2, max_value=10**38)))
    return amount if draw(st.booleans()) else from_units(units)


# --------------------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------------------

#: Assets drawn from a small pool so that two fields name the same one often enough for the
#: shapes to occur. Every member is valid text; several differ from another only in a way
#: an exact comparison sees -- case, a space, a no-break space, composed against decomposed.
ASSETS: Final = (
    "BTC",
    "USDT",
    "USDC",
    "KAS",
    "btc",
    "BTC ",
    " BTC",
    "B TC",
    "BT\N{NO-BREAK SPACE}C",
    "\N{LATIN SMALL LETTER E WITH ACUTE}TH",
    "e\N{COMBINING ACUTE ACCENT}TH",
    "\N{CJK UNIFIED IDEOGRAPH-5E01}",
    "\U0001f600",
    "\N{ZERO WIDTH SPACE}",
    "\x00",
)

#: Whitespace a venue's text could carry inside or around it. `str.strip` removes each.
SPACES: Final = (" ", "\t", "\n", "\N{NO-BREAK SPACE}", "\N{EM SPACE}", "\N{IDEOGRAPHIC SPACE}")

#: Text both types must refuse: blank to `str.strip`, including a separator Python counts
#: as whitespace, and lone surrogates UTF-8 cannot encode.
BLANK_TEXT: Final = ("", " ", "\t\n", "\N{IDEOGRAPHIC SPACE}", "\x1c")
UNENCODABLE_TEXT: Final = (chr(0xD800), "id-" + chr(0xDFFF))


@st.composite
def texts(draw: st.DrawFn) -> str:
    """Valid text at the edges of the rule: pooled, arbitrary, or with whitespace inside."""
    kind = draw(st.sampled_from(["pool"] * 4 + ["drawn", "spaced"]))
    if kind == "pool":
        return draw(st.sampled_from(ASSETS))
    visible = st.text(min_size=1, max_size=6).filter(lambda text: bool(text.strip()))
    if kind == "drawn":
        return draw(visible)
    before, after = draw(visible), draw(visible)
    inside = draw(st.sampled_from(SPACES))
    around = draw(st.sampled_from(["", *SPACES]))
    return f"{around}{before}{inside}{after}{around}"


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------

ONE_MICROSECOND: Final = timedelta(microseconds=1)
#: The widest offset `datetime.timezone` accepts: strictly less than a day.
WIDEST_OFFSET: Final = timedelta(hours=24) - ONE_MICROSECOND
FIVE_HOURS: Final = timedelta(hours=5)

NAMED_OFFSETS: Final = (
    timedelta(0),
    ONE_MICROSECOND,
    -ONE_MICROSECOND,
    timedelta(seconds=1),
    FIVE_HOURS,
    -FIVE_HOURS,
    timedelta(hours=5, minutes=45),
    timedelta(hours=-3),
    timedelta(hours=14),
    WIDEST_OFFSET,
    -WIDEST_OFFSET,
)

#: The first and last wall-clock readings a `datetime` holds, written at UTC. Each moment
#: below is a reading that `execution_times` then places at another offset with `replace`.
FIRST: Final = datetime.min.replace(tzinfo=UTC)
LAST: Final = datetime.max.replace(tzinfo=UTC)

#: The ends of the calendar, and the readings a few hours inside them that an offset of
#: that size just does or just does not carry past the end.
EDGE_MOMENTS: Final = (
    FIRST,
    FIRST + ONE_MICROSECOND,
    FIRST + FIVE_HOURS - ONE_MICROSECOND,
    FIRST + FIVE_HOURS,
    LAST,
    LAST - ONE_MICROSECOND,
    LAST - FIVE_HOURS + ONE_MICROSECOND,
    LAST - FIVE_HOURS,
)


class Seasonal(tzinfo):
    """+02:00 from January to June and -02:00 from July to December.

    An offset that depends on the date, as a real zone's does, without the zone database that
    a Windows interpreter does not ship. January is where `datetime.min` is and December is
    where `datetime.max` is, so each end of the calendar is reachable at an offset that
    carries it past the end.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        if dt is None:
            return None
        return timedelta(hours=2) if dt.month <= 6 else timedelta(hours=-2)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str | None:
        return "seasonal"


SEASONAL: Final = Seasonal()


@st.composite
def execution_times(draw: st.DrawFn) -> datetime:
    """An instant anywhere a `datetime` can be, at any offset, and now and then naive."""
    edge = draw(st.sampled_from([True, False, False]))
    moment = (
        draw(st.sampled_from(EDGE_MOMENTS)) if edge else draw(st.datetimes(timezones=st.just(UTC)))
    )
    zone = draw(st.sampled_from(["named", "drawn", "seasonal", "utc", "named", "drawn"]))
    if draw(st.integers(min_value=0, max_value=39)) == 0:
        return moment.replace(tzinfo=None)  # naive, which both must refuse
    if zone == "utc":
        return moment
    if zone == "seasonal":
        return moment.replace(tzinfo=SEASONAL)
    offset = (
        draw(st.sampled_from(NAMED_OFFSETS))
        if zone == "named"
        else draw(st.timedeltas(min_value=-WIDEST_OFFSET, max_value=WIDEST_OFFSET))
    )
    return moment.replace(tzinfo=timezone(offset))


def has_utc_spelling(moment: datetime) -> bool:
    """Whether an aware `moment` can be converted to UTC at all."""
    try:
        moment.astimezone(UTC)
    except OverflowError:
        return False
    return True


# --------------------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """The fields of a `NormalizedFill` that may or may not be one."""

    external_trade_id: str
    external_order_id: str | None
    symbol: str
    base_asset: str
    quote_asset: str
    side: FillSide
    quantity: Decimal
    price: Decimal
    quote_quantity: Decimal
    quote_quantity_derived: bool
    fee_amount: Decimal
    fee_asset: str | None
    executed_at: datetime
    raw_payload: str

    @property
    def received_asset(self) -> str:
        return self.base_asset if self.side is FillSide.BUY else self.quote_asset

    @property
    def received(self) -> Decimal:
        return self.quantity if self.side is FillSide.BUY else self.quote_quantity

    @property
    def given_asset(self) -> str:
        return self.quote_asset if self.side is FillSide.BUY else self.base_asset

    @property
    def given(self) -> Decimal:
        return self.quote_quantity if self.side is FillSide.BUY else self.quantity

    def fill(self) -> NormalizedFill | None:
        """The fill, or `None` when `NormalizedFill` refuses it -- with its own error only.

        Anything other than `ExchangeSchemaError` escapes and fails the property: a refusal
        outside the taxonomy is a defect of its own.
        """
        try:
            return NormalizedFill(
                external_trade_id=self.external_trade_id,
                external_order_id=self.external_order_id,
                symbol=self.symbol,
                base_asset=self.base_asset,
                quote_asset=self.quote_asset,
                side=self.side,
                quantity=self.quantity,
                price=self.price,
                quote_quantity=self.quote_quantity,
                quote_quantity_derived=self.quote_quantity_derived,
                fee_amount=self.fee_amount,
                fee_asset=self.fee_asset,
                executed_at=self.executed_at,
                raw_payload=self.raw_payload,
            )
        except ExchangeSchemaError:
            return None


#: The two positions that fold into a leg first: Hypothesis leans towards the first element.
FEE_POSITIONS: Final = (
    "given rebate",
    "received paid",
    "zero named",
    "received rebate",
    "given paid",
    "third paid",
    "third rebate",
    "none",
)


@st.composite
def fees(draw: st.DrawFn, candidate: Candidate) -> tuple[Decimal, str | None]:
    """A fee in every position, the ones that fold into a leg drawn around that leg."""
    position = draw(st.sampled_from(FEE_POSITIONS))
    if position == "none":
        return Decimal(draw(st.sampled_from(ZEROS))), None
    if position == "zero named":
        named = draw(
            st.sampled_from([candidate.received_asset, candidate.given_asset, draw(texts())])
        )
        return Decimal(draw(st.sampled_from(ZEROS))), named
    if position == "received paid":
        return near(draw, candidate.received), candidate.received_asset
    if position == "received rebate":
        return draw(grid_amounts()).copy_negate(), candidate.received_asset
    if position == "given paid":
        return draw(grid_amounts()), candidate.given_asset
    if position == "given rebate":
        return near(draw, candidate.given).copy_negate(), candidate.given_asset
    third = draw(texts())
    paid = draw(grid_amounts())
    return (paid if position == "third paid" else paid.copy_negate()), third


AMOUNT_FIELDS: Final = ("quantity", "price", "quote_quantity", "fee_amount")
TEXT_FIELDS: Final = (
    "external_trade_id",
    "external_order_id",
    "symbol",
    "base_asset",
    "quote_asset",
    "fee_asset",
    "raw_payload",
)


def with_field(candidate: Candidate, field: str, value: object) -> Candidate:
    """`candidate` with one field, named at run time, replaced by `value` of any type."""
    return replace(candidate, **{field: value})  # type: ignore[arg-type]


@st.composite
def candidates(draw: st.DrawFn) -> Candidate:
    """A candidate fill aimed at the edges, now and then with one field broken on purpose."""
    base = draw(texts())
    quote = base if draw(st.sampled_from([False] * 9 + [True])) else draw(texts())
    candidate = Candidate(
        external_trade_id=draw(st.one_of(texts(), st.integers(0, 10**20).map(str))),
        external_order_id=draw(st.one_of(st.none(), st.just(""), texts())),
        symbol=draw(texts()),
        base_asset=base,
        quote_asset=quote,
        side=draw(st.sampled_from(FillSide)),
        quantity=draw(grid_amounts()),
        price=draw(grid_amounts()),
        quote_quantity=draw(grid_amounts()),
        quote_quantity_derived=draw(st.booleans()),
        fee_amount=Decimal(0),
        fee_asset=None,
        executed_at=draw(execution_times()),
        raw_payload=draw(texts()),
    )
    fee_amount, fee_asset = draw(fees(candidate))
    candidate = replace(candidate, fee_amount=fee_amount, fee_asset=fee_asset)
    broken = draw(st.sampled_from(["none"] * 12 + ["amount", "blank", "unencodable"]))
    if broken == "amount":
        field = draw(st.sampled_from(AMOUNT_FIELDS))
        return with_field(candidate, field, draw(st.sampled_from(OFF_GRID)))
    if broken != "none":
        field = draw(st.sampled_from(TEXT_FIELDS))
        bad = BLANK_TEXT if broken == "blank" else UNENCODABLE_TEXT
        return with_field(candidate, field, draw(st.sampled_from(bad)))
    return candidate


# --------------------------------------------------------------------------------------
# The conversion, and the property
# --------------------------------------------------------------------------------------


def to_trade(fill: NormalizedFill, venue: ExchangeKey) -> Trade:
    """The `Trade` #19's mapper will build: the venue key as source, the trade id as id."""
    return Trade(
        key=EventKey(
            occurred_at=fill.executed_at, source=venue.value, external_id=fill.external_trade_id
        ),
        base_asset=fill.base_asset,
        quote_asset=fill.quote_asset,
        side=fill.side,
        quantity=fill.quantity,
        quote_quantity=fill.quote_quantity,
        fee_amount=fill.fee_amount,
        fee_asset=fill.fee_asset,
    )


def pinned(**overrides: object) -> Candidate:
    """A plain buy of 7.31731731 BTC for 91739.173917 USDT, with `overrides` applied."""
    base = Candidate(
        external_trade_id="1001",
        external_order_id="5001",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        side=FillSide.BUY,
        quantity=Decimal("7.31731731"),
        price=Decimal("12537.33"),
        quote_quantity=Decimal("91739.173917"),
        quote_quantity_derived=False,
        fee_amount=Decimal(0),
        fee_asset=None,
        executed_at=datetime(2026, 9, 3, 15, 30, tzinfo=UTC),
        raw_payload='{"tradeId":"1001"}',
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


#: The known shapes, each refused by `NormalizedFill` and so rejected, and the nearest
#: accepted neighbour of each, which must convert. Run on every execution of the property.
PINNED: Final = {
    "the same asset on both sides": pinned(quote_asset="BTC"),
    "a fee equal to the quantity bought": pinned(fee_amount=Decimal("7.31731731"), fee_asset="BTC"),
    "a fee one unit inside the quantity bought": pinned(
        fee_amount=Decimal("7.317317309999999999"), fee_asset="BTC"
    ),
    "a fee equal to the proceeds of a sale": pinned(
        side=FillSide.SELL, fee_amount=Decimal("91739.173917"), fee_asset="USDT"
    ),
    "a rebate equal to the cost of a buy": pinned(
        fee_amount=Decimal("-91739.173917"), fee_asset="USDT"
    ),
    "a rebate one unit inside the cost of a buy": pinned(
        fee_amount=Decimal("-91739.173916999999999999"), fee_asset="USDT"
    ),
    "a rebate equal to the quantity sold": pinned(
        side=FillSide.SELL, fee_amount=Decimal("-7.31731731"), fee_asset="BTC"
    ),
    "a zero fee naming the asset received": pinned(fee_amount=Decimal("-0"), fee_asset="BTC"),
    "datetime.min at +05:00": pinned(executed_at=FIRST.replace(tzinfo=timezone(FIVE_HOURS))),
    "datetime.max at -05:00": pinned(executed_at=LAST.replace(tzinfo=timezone(-FIVE_HOURS))),
    "datetime.min at UTC": pinned(executed_at=FIRST.replace(tzinfo=UTC)),
    "datetime.max at -05:00, five hours inside": pinned(
        executed_at=(LAST - FIVE_HOURS).replace(tzinfo=timezone(-FIVE_HOURS))
    ),
    "datetime.min in a seasonal zone": pinned(executed_at=FIRST.replace(tzinfo=SEASONAL)),
}

CONVERTS: Final = settings(
    max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


def with_pinned_examples[F: Callable[..., None]](test: F) -> F:
    """Apply every `PINNED` candidate as an explicit example. The venue changes no verdict."""
    for candidate in PINNED.values():
        test = example(candidate=candidate, venue=ExchangeKey.BITGET)(test)
    return test


@CONVERTS
@with_pinned_examples
@given(candidate=candidates(), venue=st.sampled_from(ExchangeKey))
def test_every_fill_normalized_fill_accepts_converts_to_a_trade(
    candidate: Candidate, venue: ExchangeKey
) -> None:
    """The invariant #19's recompute rests on, over candidates aimed at both types' edges.

    A candidate `NormalizedFill` refuses is rejected; that it refuses some of each kind is
    `test_the_candidates_reach`'s to prove. One it accepts must build a `Trade` whose every
    field is the fill's own, and whose key is the fill's instant, venue and trade id.
    """
    fill = candidate.fill()
    if fill is None:
        reject()

    trade = to_trade(fill, venue)

    assert trade.key.occurred_at == fill.executed_at
    assert trade.key.occurred_at.utcoffset() == timedelta(0)
    assert trade.key.source == venue.value
    assert trade.key.external_id == fill.external_trade_id
    assert trade.base_asset == fill.base_asset
    assert trade.quote_asset == fill.quote_asset
    assert trade.side is fill.side
    assert trade.quantity == fill.quantity
    assert trade.quote_quantity == fill.quote_quantity
    assert trade.fee_amount == fill.fee_amount
    assert trade.fee_asset == fill.fee_asset


@pytest.mark.parametrize("label", list(PINNED))
def test_each_pinned_candidate_is_refused_or_converts(label: str) -> None:
    """The explicit examples, one by one, with the verdict each must have written down.

    The property rejects a candidate `NormalizedFill` refuses, so an accepted boundary that
    began to be refused would be rejected there without a word. These say which way each
    must go. (A refusal that was dropped is caught by the property too: the example then
    reaches `Trade`, which refuses it.)
    """
    candidate = PINNED[label]
    refused = {
        "the same asset on both sides",
        "a fee equal to the quantity bought",
        "a fee equal to the proceeds of a sale",
        "a rebate equal to the cost of a buy",
        "a rebate equal to the quantity sold",
        "datetime.min at +05:00",
        "datetime.max at -05:00",
        "datetime.min in a seasonal zone",
    }
    fill = candidate.fill()

    if label in refused:
        assert fill is None
    else:
        assert fill is not None
        to_trade(fill, ExchangeKey.BINGX)


# --------------------------------------------------------------------------------------
# The candidates reach what the module docstring claims
# --------------------------------------------------------------------------------------

#: `derandomize` so the search is the same on every run: a reachability check that passes
#: or fails by luck would be a flaky test, not a guarantee.
FIND_SETTINGS: Final = settings(
    max_examples=3000,
    derandomize=True,
    phases=[Phase.generate],
    deadline=None,
    suppress_health_check=list(HealthCheck),
    database=None,
)


def fee_position(candidate: Candidate) -> str:
    """Where a candidate's fee is, in `FEE_POSITIONS`' words."""
    if candidate.fee_amount.is_zero():
        return "none" if candidate.fee_asset is None else "zero named"
    sign = "rebate" if candidate.fee_amount < 0 else "paid"
    if candidate.fee_asset == candidate.received_asset:
        return f"received {sign}"
    if candidate.fee_asset == candidate.given_asset:
        return f"given {sign}"
    return f"third {sign}"


def accepted(candidate: Candidate) -> bool:
    return candidate.fill() is not None


def amounts_of(candidate: Candidate) -> tuple[Decimal, ...]:
    return (candidate.quantity, candidate.price, candidate.quote_quantity, candidate.fee_amount)


def places_by_value(amount: Decimal) -> int:
    """How many fractional digits `amount` has once its trailing zeros are dropped."""
    exponent = amount.normalize(_WIDE).as_tuple().exponent
    assert isinstance(exponent, int)
    return max(0, -exponent)


def text_of(candidate: Candidate) -> tuple[str, ...]:
    values = (getattr(candidate, field) for field in TEXT_FIELDS)
    return tuple(value for value in values if value is not None)


def fee_against_leg(candidate: Candidate) -> tuple[str, int] | None:
    """Where a fee folding into a leg sits against it, in units: `("received", fee - leg)`.

    `None` for a fee that folds into neither, and for the same asset on both sides, where
    received and given are one asset and the question has no answer.
    """
    if candidate.base_asset == candidate.quote_asset or candidate.fee_amount.is_zero():
        return None
    fee = to_units(candidate.fee_amount) if _on_grid(candidate.fee_amount) else None
    if fee is None:
        return None
    if candidate.fee_asset == candidate.received_asset and _on_grid(candidate.received):
        return "received", fee - to_units(candidate.received)
    if candidate.fee_asset == candidate.given_asset and _on_grid(candidate.given):
        return "given", -fee - to_units(candidate.given)
    return None


def _on_grid(amount: Decimal) -> bool:
    return (Fraction(amount) / _UNIT_FRACTION).denominator == 1


def _is_utc_offset_zero(moment: datetime) -> bool:
    return moment.utcoffset() == timedelta(0)


def _exponent(amount: Decimal) -> int:
    """The exponent `amount` is spelled with: -20 for `1.00000000000000000000`, 2 for `1E+2`."""
    exponent = amount.as_tuple().exponent
    assert isinstance(exponent, int)
    return exponent


def _refused_blank(candidate: Candidate) -> bool:
    """Blank text in a field where blank is refused: every text field but the order id."""
    for field in TEXT_FIELDS:
        value = getattr(candidate, field)
        if field != "external_order_id" and value is not None and not value.strip():
            return True
    return False


def _accepted_with_fee(where: str) -> Callable[[Candidate], bool]:
    return lambda candidate: fee_position(candidate) == where and accepted(candidate)


REACHABLE: Final[dict[str, Callable[[Candidate], bool]]] = {
    **{
        f"an accepted fill with a fee {where}": _accepted_with_fee(where)
        for where in (
            "none",
            "zero named",
            "received paid",
            "received rebate",
            "given paid",
            "given rebate",
            "third paid",
            "third rebate",
        )
    },
    "an accepted buy": lambda c: c.side is FillSide.BUY and accepted(c),
    "an accepted sale": lambda c: c.side is FillSide.SELL and accepted(c),
    "an accepted zero fee naming the asset received": lambda c: (
        c.fee_amount.is_zero() and c.fee_asset == c.received_asset and accepted(c)
    ),
    "an accepted zero fee spelled -0 naming an asset": lambda c: (
        c.fee_amount.is_zero()
        and c.fee_amount.is_signed()
        and c.fee_asset is not None
        and accepted(c)
    ),
    # The boundaries of the three shapes, from both sides.
    "the same asset on both sides": lambda c: c.base_asset == c.quote_asset,
    "an accepted rebate in a third asset larger than the whole trade": lambda c: (
        fee_position(c) == "third rebate"
        and -c.fee_amount > max(c.quantity, c.quote_quantity)
        and accepted(c)
    ),
    "an accepted fee one unit inside what was received": lambda c: (
        fee_against_leg(c) == ("received", -1) and accepted(c)
    ),
    "a fee exactly what was received": lambda c: fee_against_leg(c) == ("received", 0),
    "a fee one unit past what was received": lambda c: fee_against_leg(c) == ("received", 1),
    "a fee far past what was received": lambda c: (
        (position := fee_against_leg(c)) is not None
        and position[0] == "received"
        and position[1] > 1
    ),
    "an accepted rebate one unit inside what was given": lambda c: (
        fee_against_leg(c) == ("given", -1) and accepted(c)
    ),
    "a rebate exactly what was given": lambda c: fee_against_leg(c) == ("given", 0),
    "a rebate one unit past what was given": lambda c: fee_against_leg(c) == ("given", 1),
    "a rebate far past what was given": lambda c: (
        (position := fee_against_leg(c)) is not None and position[0] == "given" and position[1] > 1
    ),
    # Amounts.
    "an accepted amount of one unit": lambda c: (
        any(abs(amount) == UNIT for amount in amounts_of(c)) and accepted(c)
    ),
    "an accepted quantity of one unit": lambda c: c.quantity == UNIT and accepted(c),
    "an accepted amount with twenty integer digits": lambda c: (
        any(not a.is_zero() and a.adjusted() == 19 for a in amounts_of(c)) and accepted(c)
    ),
    "an accepted fee with twenty integer digits": lambda c: (
        not c.fee_amount.is_zero() and c.fee_amount.adjusted() == 19 and accepted(c)
    ),
    "the widest amount, accepted": lambda c: (
        any(abs(amount) == WIDEST for amount in amounts_of(c)) and accepted(c)
    ),
    "an accepted amount at eighteen places by value": lambda c: (
        any(places_by_value(a) == 18 for a in amounts_of(c)) and accepted(c)
    ),
    "an accepted amount at a middle number of places": lambda c: (
        any(places_by_value(a) == 9 for a in amounts_of(c)) and accepted(c)
    ),
    "an accepted whole amount": lambda c: (
        any(not a.is_zero() and places_by_value(a) == 0 for a in amounts_of(c)) and accepted(c)
    ),
    "an accepted amount spelled past eighteen places": lambda c: (
        any(_exponent(a) < -18 for a in amounts_of(c)) and accepted(c)
    ),
    "an accepted amount in exponent form": lambda c: (
        any(_exponent(a) > 0 for a in amounts_of(c)) and accepted(c)
    ),
    "an amount off the grid": lambda c: any(
        not _on_grid(a) or abs(a) >= Decimal("1E+20") for a in amounts_of(c)
    ),
    # Time.
    "an accepted fill in year 1": lambda c: c.executed_at.year == 1 and accepted(c),
    "an accepted fill in year 9999": lambda c: c.executed_at.year == 9999 and accepted(c),
    "an accepted fill in year 1 at a non-UTC offset": lambda c: (
        c.executed_at.year == 1
        and c.executed_at.utcoffset() is not None
        and not _is_utc_offset_zero(c.executed_at)
        and accepted(c)
    ),
    "an accepted fill in year 9999 at a non-UTC offset": lambda c: (
        c.executed_at.year == 9999
        and c.executed_at.utcoffset() is not None
        and not _is_utc_offset_zero(c.executed_at)
        and accepted(c)
    ),
    "an accepted fill at a negative offset": lambda c: (
        (offset := c.executed_at.utcoffset()) is not None and offset < timedelta(0) and accepted(c)
    ),
    "an accepted fill at an offset finer than a second": lambda c: (
        (offset := c.executed_at.utcoffset()) is not None
        and offset.microseconds != 0
        and accepted(c)
    ),
    "an accepted fill in a zone whose offset depends on the date": lambda c: (
        c.executed_at.tzinfo is SEASONAL and accepted(c)
    ),
    "an instant at the start with no UTC spelling": lambda c: (
        c.executed_at.utcoffset() is not None
        and c.executed_at.year == 1
        and not has_utc_spelling(c.executed_at)
    ),
    "an instant at the end with no UTC spelling": lambda c: (
        c.executed_at.utcoffset() is not None
        and c.executed_at.year == 9999
        and not has_utc_spelling(c.executed_at)
    ),
    "a naive instant": lambda c: c.executed_at.utcoffset() is None,
    # Text.
    "accepted non-ASCII text": lambda c: (
        any(not text.isascii() for text in text_of(c)) and accepted(c)
    ),
    "an accepted asset outside the Basic Multilingual Plane": lambda c: (
        any(ord(char) > 0xFFFF for char in c.base_asset + c.quote_asset) and accepted(c)
    ),
    "accepted text with whitespace inside": lambda c: (
        any(any(char.isspace() for char in text.strip()) for text in text_of(c)) and accepted(c)
    ),
    "accepted text with whitespace around": lambda c: (
        any(text != text.strip() for text in text_of(c)) and accepted(c)
    ),
    "an accepted asset that is a zero-width space": lambda c: (
        "\N{ZERO WIDTH SPACE}" in (c.base_asset, c.quote_asset) and accepted(c)
    ),
    "an accepted blank order id": lambda c: c.external_order_id == "" and accepted(c),
    "blank text where it is refused": _refused_blank,
    "a lone surrogate": lambda c: any(
        0xD800 <= ord(char) <= 0xDFFF for text in text_of(c) for char in text
    ),
}


@functools.cache
def reached() -> frozenset[str]:
    """Every label in `REACHABLE` that one derandomized search over the candidates meets.

    One search for all of them rather than one per label: a candidate costs a few
    milliseconds to draw, and a separate search per label spent half a minute drawing the
    same candidates again. The search stops at the first candidate that completes the set,
    and a label still missing after `FIND_SETTINGS.max_examples` draws is simply absent,
    which fails that label's test below.
    """
    found: set[str] = set()

    def record(candidate: Candidate) -> bool:
        for label, condition in REACHABLE.items():
            if label not in found and condition(candidate):
                found.add(label)
        return len(found) == len(REACHABLE)

    with contextlib.suppress(NoSuchExample):
        find(candidates(), record, settings=FIND_SETTINGS)
    return frozenset(found)


@pytest.mark.parametrize("label", list(REACHABLE))
def test_the_candidates_reach(label: str) -> None:
    """Each edge the module docstring promises, found by search among the candidates.

    A strategy edit that quietly stopped drawing rebates at the given leg, or instants with
    no UTC spelling, would leave the property green over nothing.
    """
    assert label in reached()


def test_the_find_harness_can_fail() -> None:
    """The control: a condition no candidate meets is reported, not silently passed."""
    with pytest.raises(NoSuchExample):
        find(
            candidates(),
            lambda candidate: candidate.side not in (FillSide.BUY, FillSide.SELL),
            settings=settings(FIND_SETTINGS, max_examples=50),
        )
