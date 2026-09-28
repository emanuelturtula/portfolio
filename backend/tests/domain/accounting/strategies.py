"""Hypothesis strategies for spec 019's property tests: random valid event histories.

**The universe is small on purpose**: `BTC`, `KAS` and `BGB` are inventory, `USDT` and `USDC`
are cash. Five symbols make every pair shape likely -- a buy, a sale, a swap, a conversion --
and make it likely that a fee lands in an asset with a history, or in one without.

**Every fee position is drawn**: none; in the received asset (paid or rebated); in the given
asset (paid or rebated); in a third cash asset; in a third non-cash asset, paid or rebated;
and a zero fee naming an asset (R8). Paid fees and rebates are bounded only where `Trade`
bounds them, so the boundary cases -- a fee leaving one unit received, a rebate leaving one
unit given -- are reachable.

**Amounts are drawn at 0, 2, 8 or 18 places**, so that most divisions do not come out even
and the rounding paths are exercised rather than avoided, and up to 10**5 in magnitude. That
bound is what keeps every *sum* inside `MONEY_PRECISION`: the one range the spec accepts may
still raise (spec 019, R1, "remaining range") is a basis or proceeds total past 10**20, and
no history here can add up to that. The *average* can still exceed it -- a dust quantity
bought for a large amount -- and that is R1's `None`, which the strategies reach on purpose.

**Timestamps collide on purpose**: they are drawn from a few dozen minutes, so ties on the
instant are common and the `source`, `external_id` and `kind` tie-breaks all run. Ids are
the event's index as a string, so `"10"` sorts before `"9"`, and identities never repeat
unless a test repeats them.

`test_invariants.py::test_the_strategy_reaches_*` proves each of these is actually generated,
rather than trusting this docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final

from hypothesis import strategies as st

from portfolio.domain.accounting import (
    AccountingConfig,
    Adjustment,
    EventKey,
    Trade,
    Transfer,
)
from portfolio.domain.exchanges import FillSide

if TYPE_CHECKING:
    from portfolio.domain.accounting.events import AccountingEvent

NON_CASH: Final = ("BTC", "KAS", "BGB")
CASH: Final = ("USDT", "USDC")
UNIVERSE: Final = NON_CASH + CASH
CONFIG: Final = AccountingConfig(frozenset(CASH))
SOURCES: Final = ("bingx", "bitget", "manual")
LOCATIONS: Final = ("bitget", "bingx", "cold-storage", "phone-wallet")
START: Final = datetime(2026, 3, 1, tzinfo=UTC)
PLACES: Final = (0, 2, 8, 18)
MAX_WHOLE: Final = 10**5

FEE_POSITIONS: Final = (
    "none",
    "received",
    "received rebate",
    "given",
    "given rebate",
    "third cash",
    "third non-cash",
    "third non-cash rebate",
    "zero named",
)

UNIT: Final = Fraction(1, 10**18)


def from_units(units: int) -> Decimal:
    """`units` millionths-of-a-trillionth: an exact decimal on the 18-place grid."""
    return Decimal(f"{units}E-18")


def to_units(value: Decimal) -> int:
    scaled = Fraction(value) / UNIT
    assert scaled.denominator == 1, value
    return scaled.numerator


@st.composite
def amounts(draw: st.DrawFn, *, maximum: int = MAX_WHOLE) -> Decimal:
    """A positive amount with 0, 2, 8 or 18 places and at most `maximum` in value.

    One draw in ten is the smallest amount there is, one unit at 18 places, and one in ten
    is `maximum` itself. The extremes are where R1's unrepresentable average and the
    one-unit fee boundaries live, and a uniform draw would reach them too rarely.

    One draw in ten is a handful of units at 18 places -- 2E-18 to 16E-18. Among small
    integers of units, a proportional share is often an exact half-unit tie, and a tie on
    an odd number of units is the one input where a complement taken by a second rounding
    differs from one taken by subtraction. The mutation sweep showed such splits going
    unchecked without this branch.
    """
    extreme = draw(st.sampled_from(["no"] * 7 + ["dust", "maximum", "units"]))
    if extreme == "dust":
        return Decimal("1E-18")
    if extreme == "units":
        return Decimal(f"{draw(st.integers(min_value=2, max_value=16))}E-18")
    if extreme == "maximum":
        return Decimal(maximum)
    places = draw(st.sampled_from(PLACES))
    whole = draw(st.integers(min_value=0, max_value=maximum))
    fraction = draw(st.integers(min_value=0, max_value=10**places - 1)) if places else 0
    value = Decimal(f"{whole}.{fraction:0{places}d}") if places else Decimal(whole)
    if value == 0:
        value = Decimal(f"1E-{places}") if places else Decimal(1)
    return value


def units_below(draw: st.DrawFn, ceiling: Decimal) -> Decimal | None:
    """An amount strictly between zero and `ceiling`, on the grid, or `None` if none exists."""
    top = to_units(ceiling) - 1
    if top < 1:
        return None
    return from_units(draw(st.integers(min_value=1, max_value=top)))


@st.composite
def keys(draw: st.DrawFn, index: int) -> EventKey:
    minutes = draw(st.integers(min_value=0, max_value=40))
    microseconds = draw(st.sampled_from([0, 0, 0, 123000, 999999]))
    source = draw(st.sampled_from(SOURCES))
    moment = START + timedelta(minutes=minutes, microseconds=microseconds)
    return EventKey(moment, source, str(index))


@st.composite
def trades(draw: st.DrawFn, event_key: EventKey) -> Trade:
    base, quote = draw(st.permutations(UNIVERSE))[:2]
    side = draw(st.sampled_from([FillSide.BUY, FillSide.SELL]))
    quantity = draw(amounts())
    quote_quantity = draw(amounts())
    received_asset, received = (base, quantity) if side is FillSide.BUY else (quote, quote_quantity)
    given_asset, given = (quote, quote_quantity) if side is FillSide.BUY else (base, quantity)
    thirds_cash = [asset for asset in CASH if asset not in (base, quote)]
    thirds_non_cash = [asset for asset in NON_CASH if asset not in (base, quote)]

    position = draw(st.sampled_from(FEE_POSITIONS))
    fee: Decimal | None = Decimal(0)
    fee_asset: str | None = None
    if position == "received":
        fee, fee_asset = units_below(draw, received), received_asset
    elif position == "received rebate":
        fee, fee_asset = draw(amounts()).copy_negate(), received_asset
    elif position == "given":
        fee, fee_asset = draw(amounts()), given_asset
    elif position == "given rebate":
        below = units_below(draw, given)
        fee, fee_asset = (None if below is None else below.copy_negate()), given_asset
    elif position == "third cash" and thirds_cash:
        paid = draw(amounts(maximum=1000))
        rebate = draw(st.sampled_from([False, False, True]))
        fee, fee_asset = (
            (paid.copy_negate() if rebate else paid),
            draw(st.sampled_from(thirds_cash)),
        )
    elif position == "third non-cash" and thirds_non_cash:
        fee, fee_asset = draw(amounts(maximum=1000)), draw(st.sampled_from(thirds_non_cash))
    elif position == "third non-cash rebate" and thirds_non_cash:
        rebated = draw(amounts(maximum=1000)).copy_negate()
        fee, fee_asset = rebated, draw(st.sampled_from(thirds_non_cash))
    elif position == "zero named":
        fee, fee_asset = Decimal(0), draw(st.sampled_from(UNIVERSE))
    if fee is None:  # no room on the grid for this position: no fee at all
        fee, fee_asset = Decimal(0), None
    return Trade(
        key=event_key,
        base_asset=base,
        quote_asset=quote,
        side=side,
        quantity=quantity,
        quote_quantity=quote_quantity,
        fee_amount=fee,
        fee_asset=fee_asset,
    )


@st.composite
def adjustments(draw: st.DrawFn, event_key: EventKey) -> Adjustment:
    asset = draw(st.sampled_from(UNIVERSE))
    unit_cost = draw(st.one_of(st.none(), st.just(Decimal(0)), amounts()))
    return Adjustment(key=event_key, asset=asset, quantity=draw(amounts()), unit_cost=unit_cost)


@st.composite
def transfers(draw: st.DrawFn, event_key: EventKey) -> Transfer:
    origin, destination = draw(st.permutations(LOCATIONS))[:2]
    return Transfer(
        key=event_key,
        asset=draw(st.sampled_from(UNIVERSE)),
        quantity=draw(amounts()),
        from_location=origin,
        to_location=destination,
    )


@st.composite
def events(draw: st.DrawFn, index: int) -> AccountingEvent:
    event_key = draw(keys(index))
    kind = draw(st.sampled_from(["trade"] * 6 + ["adjustment", "transfer"]))
    if kind == "trade":
        return draw(trades(event_key))
    if kind == "adjustment":
        return draw(adjustments(event_key))
    return draw(transfers(event_key))


@st.composite
def histories(draw: st.DrawFn, *, min_size: int = 0, max_size: int = 16) -> list[AccountingEvent]:
    """A list of valid events with distinct identities, in no particular order."""
    size = draw(st.integers(min_value=min_size, max_value=max_size))
    return [draw(events(index)) for index in range(size)]
