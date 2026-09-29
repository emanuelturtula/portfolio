"""What a set of exchange fills adds up to: per asset, in USDT, per other quote, and in fees.

`total_fills(lines)` is the aggregation behind `GET /api/exchanges/fills` (#93): plain sums
over whatever fills the caller selected, with no cost basis, average price or P&L -- those are
the accounting engine's. It lives here rather than in the service so that the arithmetic is
held to the domain's exactness rules and coverage floor, and so that #17 and #19 can reuse it.

**Pure.** No I/O, no clock, no ORM: a `FillLine` is the handful of fields the sums need, and
the caller builds them from whatever it loaded.

## Every sum is exact

Each figure is accumulated with `money.add` and `money.subtract`, which align the operands
and add their coefficients as integers, so no sum is ever rounded -- not by the interpreter's
default context of 28 significant digits, and not by anyone's `decimal.localcontext()`. An
18-place amount beside a total in the billions already needs more than 28 digits, and `+`
under the default context would drop the last of them without a word (spec 024).

A sum over no fills is zero **at the stored scale**, `0E-18`, so an empty figure reads on the
wire as `0.000000000000000000`, like every other amount beside it. A sum over stored amounts
carries their eighteen places too: `add` keeps the smaller exponent of its operands.

## Only a USDT-quoted fill has a USDT value

A fill's USDT value is its stored `quote_quantity` when `quote_asset` is exactly `USDT`, and
nothing otherwise -- never `quantity * price`, and never a conversion at today's price, which
would present a current price as the price on the day (#93, *Decisions taken*). A fill quoted
in anything else is counted under `not_valued_in_usdt`, with its quote summed in its own
asset, and its base asset's row says how many of its fills the USDT figures leave out.

## Every net is buys minus sells

`net`, `usdt_net`, `usdt.net` and each quote asset's `net` are what was bought or spent minus
what was sold or received. A positive net is net buying. Over a filtered range any of them may
be negative, and none is clamped.

## Fees are summed per fee asset, with their sign, and never converted

Positive is a fee paid, negative a rebate. A fill with no fee asset -- a zero fee, which is
the only fee `NormalizedFill` lets go without one -- adds nothing and lists nothing. A fill
with a fee asset lists that asset, even when its amount, or the sum, is zero: the owner paid
fees in it, and a sum that netted to nothing is still an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final, assert_never

from portfolio.domain.exchanges import FillSide
from portfolio.domain.money import add, subtract

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "TOTALS_SCALE",
    "USDT",
    "AssetFillTotals",
    "FeeTotal",
    "FillLine",
    "FillTotals",
    "NotValuedInUsdtTotals",
    "QuoteAssetFillTotals",
    "UsdtFillTotals",
    "total_fills",
    "usdt_value",
]

USDT: Final = "USDT"
"""The one quote asset whose amounts are USDT values. Compared exactly, as the venues spell it."""

TOTALS_SCALE: Final = 18
"""The scale a zero total is spelled at: `db.models.FILL_SCALE`, the scale every fill is stored at.

Spelled again rather than imported, for the reason `domain/accounting/constants.py` gives about
its own copy: `db` sits above `domain`, and `domain` imports nothing from the application. It
decides only how an **empty** sum is written; a sum over stored amounts is at their scale
whatever this says.
"""

_ZERO: Final = Decimal((0, (0,), -TOTALS_SCALE))


@dataclass(frozen=True, slots=True)
class FillLine:
    """The fields of one fill that its totals need. Nothing that identifies the fill.

    `quantity` is in `base_asset`, `quote_quantity` in `quote_asset`, and `fee_amount` in
    `fee_asset`, signed: positive a fee paid, negative a rebate. `fee_asset` is `None` only for
    a zero fee.
    """

    base_asset: str
    quote_asset: str
    side: FillSide
    quantity: Decimal
    quote_quantity: Decimal
    fee_amount: Decimal
    fee_asset: str | None


@dataclass(frozen=True, slots=True)
class AssetFillTotals:
    """One base asset: its quantities over every fill, and its USDT over the USDT-quoted ones.

    `bought`, `sold` and `net` cover every fill of the asset, whatever it was quoted in; they
    are in the asset itself and are never summed across assets. `usdt_spent`,
    `usdt_received` and `usdt_net` cover only its USDT-quoted fills, and
    `usdt_unvalued_fill_count` is how many of its `fill_count` fills they leave out -- without
    it, a row mixing quotes would read as fully valued.
    """

    asset: str
    fill_count: int
    bought: Decimal
    sold: Decimal
    net: Decimal
    usdt_spent: Decimal
    usdt_received: Decimal
    usdt_net: Decimal
    usdt_unvalued_fill_count: int


@dataclass(frozen=True, slots=True)
class UsdtFillTotals:
    """USDT across every asset: spent on buys, received from sells, and spent minus received."""

    spent: Decimal
    received: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class QuoteAssetFillTotals:
    """One quote asset other than USDT, summed in itself and never converted.

    `spent` is the quote paid on buys, `received` the quote received on sells, and `net` is
    `spent - received`.
    """

    quote_asset: str
    fill_count: int
    spent: Decimal
    received: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class NotValuedInUsdtTotals:
    """The fills quoted in anything but USDT: how many, and their sums per quote asset."""

    fill_count: int
    by_quote_asset: tuple[QuoteAssetFillTotals, ...]


@dataclass(frozen=True, slots=True)
class FeeTotal:
    """The signed sum of the fees paid in one asset. Positive is paid, negative a rebate."""

    asset: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class FillTotals:
    """What a set of fills adds up to. Every tuple is sorted by its asset or quote asset."""

    fill_count: int
    by_asset: tuple[AssetFillTotals, ...]
    usdt: UsdtFillTotals
    not_valued_in_usdt: NotValuedInUsdtTotals
    fees: tuple[FeeTotal, ...]


def usdt_value(quote_asset: str, quote_quantity: Decimal) -> Decimal | None:
    """A fill's USDT value: its stored `quote_quantity` if it was quoted in USDT, else `None`.

    The one statement of the rule, used for each row and for the totals alike. See the module
    docstring for why nothing else is converted.
    """
    return quote_quantity if quote_asset == USDT else None


@dataclass(slots=True)
class _Flow:
    """A running pair of sums: what went in on buys, and what came out on sells."""

    fill_count: int = 0
    buys: Decimal = field(default=_ZERO)
    sells: Decimal = field(default=_ZERO)

    def record(self, side: FillSide, amount: Decimal) -> None:
        """Count one fill and add `amount` to the side it went.

        Matched on both members rather than `buy` and "anything else", so that a third member,
        or a value that is not a side at all, is an `AssertionError` rather than a sale.
        """
        self.fill_count += 1
        match side:
            case FillSide.BUY:
                self.buys = add(self.buys, amount)
            case FillSide.SELL:
                self.sells = add(self.sells, amount)
            case _:
                assert_never(side)

    @property
    def net(self) -> Decimal:
        """Buys minus sells. Never clamped."""
        return subtract(self.buys, self.sells)


@dataclass(slots=True)
class _AssetFlow:
    """A base asset's running quantities, and the USDT of its USDT-quoted fills."""

    quantity: _Flow = field(default_factory=_Flow)
    usdt: _Flow = field(default_factory=_Flow)

    def freeze(self, asset: str) -> AssetFillTotals:
        """The asset's totals as they stand."""
        return AssetFillTotals(
            asset=asset,
            fill_count=self.quantity.fill_count,
            bought=self.quantity.buys,
            sold=self.quantity.sells,
            net=self.quantity.net,
            usdt_spent=self.usdt.buys,
            usdt_received=self.usdt.sells,
            usdt_net=self.usdt.net,
            usdt_unvalued_fill_count=self.quantity.fill_count - self.usdt.fill_count,
        )


def total_fills(lines: Iterable[FillLine]) -> FillTotals:
    """Sum `lines`: per base asset, in USDT, per other quote asset, and per fee asset.

    One pass, exact throughout (see the module docstring). The order of `lines` does not
    matter: every figure is a sum, and every tuple in the result is sorted -- `by_asset` by
    asset, `by_quote_asset` by quote asset, `fees` by asset -- so the same fills in any order,
    or split into pages and concatenated, give the same totals.

    Raises:
        ValueError: an amount is a NaN or an infinity (`money.add`).
        TypeError: an amount is not a `Decimal` (`money.add`).
        AssertionError: a line's side is neither `FillSide.BUY` nor `FillSide.SELL`.
    """
    fill_count = 0
    assets: dict[str, _AssetFlow] = {}
    usdt = _Flow()
    quotes: dict[str, _Flow] = {}
    fees: dict[str, Decimal] = {}
    for line in lines:
        fill_count += 1
        asset = assets.get(line.base_asset)
        if asset is None:
            asset = assets[line.base_asset] = _AssetFlow()
        asset.quantity.record(line.side, line.quantity)
        value = usdt_value(line.quote_asset, line.quote_quantity)
        if value is not None:
            asset.usdt.record(line.side, value)
            usdt.record(line.side, value)
        else:
            quote = quotes.get(line.quote_asset)
            if quote is None:
                quote = quotes[line.quote_asset] = _Flow()
            quote.record(line.side, line.quote_quantity)
        if line.fee_asset is not None:
            fees[line.fee_asset] = add(fees.get(line.fee_asset, _ZERO), line.fee_amount)
    by_quote_asset = tuple(
        QuoteAssetFillTotals(
            quote_asset=name,
            fill_count=flow.fill_count,
            spent=flow.buys,
            received=flow.sells,
            net=flow.net,
        )
        for name, flow in sorted(quotes.items())
    )
    return FillTotals(
        fill_count=fill_count,
        by_asset=tuple(flow.freeze(name) for name, flow in sorted(assets.items())),
        usdt=UsdtFillTotals(spent=usdt.buys, received=usdt.sells, net=usdt.net),
        not_valued_in_usdt=NotValuedInUsdtTotals(
            fill_count=sum(item.fill_count for item in by_quote_asset),
            by_quote_asset=by_quote_asset,
        ),
        fees=tuple(FeeTotal(asset=name, amount=amount) for name, amount in sorted(fees.items())),
    )
