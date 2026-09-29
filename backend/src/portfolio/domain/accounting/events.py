"""The events `replay` consumes, and the refusals that make a bad one a caller's defect.

Three kinds, each a frozen dataclass that validates itself in `__post_init__`:

* a `Trade` -- one fill, from the owner's side, exactly as `NormalizedFill` describes it;
* an `Adjustment` -- an inflow the owner records, with a unit cost or without one (#18);
* a `Transfer` -- a relocation between two places, which changes no position.

**A bad event raises; it is never a replay warning.** A warning is for a fact about the
owner's history that the engine can describe and carry on from -- a sale larger than the
pool, a fee nobody bought. A `Trade` with a naive timestamp or a float quantity is not a
fact about anyone's history. It is a defect in whatever built it, and a replay that
tolerated it would be reporting numbers computed from something nobody meant.

**`TypeError` for a wrong type, `ValueError` for a wrong value**, and no message quotes an
amount or an identifier: an amount is the owner's holdings, and an id is a trade number.
Each message names the field and the rule it broke, which is what `NormalizedFill` does for
the same reason.

**Every amount obeys `NormalizedFill`'s rule**: a finite `Decimal` -- never a `bool`, an
`int` or a `float` -- with at most `AMOUNT_SCALE` fractional digits and at most
`MAX_AMOUNT_INTEGER_DIGITS` integer digits, both judged by value, so `1.50000000000000000000`
is accepted with its twenty places (spec 019, R9). The amount rule is the same so that no
stored amount is refused for its precision or its size.

**Three shapes are refused even when every field passes its own rule** -- `TradeShapeProblem`
names them, and `trade_shape_problem` is their single definition:

* a `base_asset` equal to the `quote_asset`;
* a fee in the asset received that consumes everything received;
* a rebate in the asset given that is at least as large as everything given.

Each leaves the trade without a leg to account for -- nothing received to carry the cost,
or nothing given to take it from -- and a replay that guessed would be computing a position
from something no venue meant. **`NormalizedFill` refuses the same three** by calling the
same function (spec 020), so a fill is refused where a venue's answer becomes a row, and the
append-only fill log holds only rows a `Trade` can be built from. One definition is the
point: two copies of these rules are how the claim that every stored fill converts went
false the first time. A row stored before that check, of one of these shapes, still makes
building its `Trade` raise `ValueError`; what the recompute does with it is #19's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from portfolio.domain.accounting.constants import (
    AMOUNT_SCALE,
    BASIS_SCALE,
    MAX_AMOUNT_INTEGER_DIGITS,
)
from portfolio.domain.exchanges import FillSide
from portfolio.domain.money import add, multiply, quantize, subtract

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_CASH_ASSETS",
    "AccountingConfig",
    "AccountingEvent",
    "Adjustment",
    "EventKey",
    "Trade",
    "TradeShapeProblem",
    "Transfer",
    "trade_shape_problem",
]

DEFAULT_CASH_ASSETS: Final[frozenset[str]] = frozenset({"USDC", "USDT"})
"""The assets pinned at a unit cost of exactly 1, unless the configuration says otherwise.

They are the unit of account: every cost, basis, proceeds and P&L figure is in these "cash
units", which with this set means US dollars in practice. A depeg is invisible by
construction, and so is the spread of a USDC/USDT conversion (spec 019, *Risks*).
"""


@dataclass(frozen=True, slots=True)
class EventKey:
    """When an event happened, where it came from, and the id its source gave it.

    `source` is the venue key (`"bitget"`, `"bingx"`) for a fill and `"manual"` for an
    entry the owner makes. `(kind, source, external_id)` is an event's identity, and
    `(occurred_at, source, external_id, kind)` is its place in the replay order.

    **`occurred_at` is normalised to UTC on construction.** Two keys for one instant then
    compare, sort and fingerprint identically whichever offset they were written with, and
    the fingerprint can spell the instant with a `Z` without converting anything.

    Raises:
        TypeError: `occurred_at` is not a `datetime`, or a text field is not a `str`.
        ValueError: `occurred_at` is naive, or cannot be expressed in UTC at all (a
            `datetime.min` written at a positive offset is a year-zero instant in UTC);
            or `source` or `external_id` is blank or is not UTF-8-encodable text.
    """

    occurred_at: datetime
    source: str
    external_id: str

    def __post_init__(self) -> None:
        """Refuse a key that cannot be placed in time, and normalise the one that can."""
        object.__setattr__(self, "occurred_at", _require_utc(self.occurred_at))
        _require_text(self.source, field="EventKey.source")
        _require_text(self.external_id, field="EventKey.external_id")


class TradeShapeProblem(StrEnum):
    """Why a trade whose every field is well formed still has no leg to account for.

    The three shapes `Trade` and `NormalizedFill` both refuse beyond their field rules, and
    `trade_shape_problem` is the one place they are decided (spec 020). Each leaves nothing
    received to carry the cost, or nothing given to take it from.
    """

    SAME_ASSET = "same_asset"
    """`base_asset` equals `quote_asset`: an asset traded for itself moves no position."""
    FEE_CONSUMES_RECEIVED = "fee_consumes_received"
    """A fee in the asset received that is at least everything received: nothing came in."""
    REBATE_EXCEEDS_GIVEN = "rebate_exceeds_given"
    """A rebate in the asset given that is at least everything given: nothing went out."""


def trade_shape_problem(
    *,
    base_asset: str,
    quote_asset: str,
    side: FillSide,
    quantity: Decimal,
    quote_quantity: Decimal,
    fee_amount: Decimal,
    fee_asset: str | None,
) -> TradeShapeProblem | None:
    """What leaves this trade without a leg to account for, or `None` when nothing does.

    **It assumes the field rules already passed**, and checks none of them: amounts that are
    finite `Decimal`s within the amount rule, `quantity` and `quote_quantity` above zero,
    non-blank text, and a `FillSide`. `Trade` and `NormalizedFill` each refuse a bad field
    first, with their own exception type; a second refusal here would give one condition
    two. Outside that precondition the answer is not specified -- an amount past the rule
    can raise `decimal.InvalidOperation`, or be judged at a rounded value.

    The checks, in the enum's order, and the first that applies is the answer:

    * `base_asset` equal to `quote_asset` is `SAME_ASSET`, compared exactly, as the text is
      stored;
    * **a zero fee is no leg** (spec 019, R8), so it is never a fee problem, whatever
      `fee_asset` names; nor is a `fee_asset` of `None`, which beside a non-zero fee is a
      field rule each caller has already refused;
    * a fee in the asset received that leaves nothing received is `FEE_CONSUMES_RECEIVED`;
    * a fee in the asset given that leaves nothing given is `REBATE_EXCEEDS_GIVEN`. A fee
      paid there only adds to what is given, so only a rebate can do it;
    * a fee in any third asset is a leg of its own and constrains neither.

    "Received" and "given" are `FillSide`'s: the base asset is received on a `BUY` and given
    on a `SELL`. **The comparison is at `AMOUNT_SCALE`, with `add` and `subtract`**, as
    `replay` carries the amounts: exact, since each amount is within the rule, and the sum's
    integers stay as small as the values rather than as wide as a caller's spelling -- `1`
    followed by a point and ten thousand zeros is one.
    """
    if base_asset == quote_asset:
        return TradeShapeProblem.SAME_ASSET
    if fee_amount.is_zero() or fee_asset is None:
        return None
    received_asset, given_asset = _received_then_given(side, base_asset, quote_asset)
    received_quantity, given_quantity = _received_then_given(side, quantity, quote_quantity)
    fee = quantize(fee_amount, AMOUNT_SCALE)
    if fee_asset == received_asset:
        if subtract(quantize(received_quantity, AMOUNT_SCALE), fee) <= 0:
            return TradeShapeProblem.FEE_CONSUMES_RECEIVED
    elif fee_asset == given_asset and add(quantize(given_quantity, AMOUNT_SCALE), fee) <= 0:
        return TradeShapeProblem.REBATE_EXCEEDS_GIVEN
    return None


def _received_then_given[T](side: FillSide, base: T, quote: T) -> tuple[T, T]:
    """A base-then-quote pair reordered as received-then-given, for `side`.

    A `BUY` receives the base and gives the quote; a `SELL` the reverse. The one place that
    reading of `FillSide` is written down, for assets and quantities alike.
    """
    return (base, quote) if side is FillSide.BUY else (quote, base)


@dataclass(frozen=True, slots=True)
class Trade:
    """One fill: `quantity` of `base_asset` bought or sold for `quote_quantity` of `quote_asset`.

    `side` is about the base asset and from the owner's side, as `FillSide` defines it: a
    `BUY` gives the quote and receives the base, a `SELL` the reverse.

    **`fee_amount` is signed** -- positive is a fee paid, negative a rebate -- and
    `fee_asset` names the asset it is in. A zero fee creates no fee leg and touches no
    position, so a `fee_asset` beside it is accepted and ignored (spec 019, R8), which is
    what `NormalizedFill` allows and a venue sends. A non-zero fee without one is refused:
    an amount of nothing in particular cannot be accounted for.

    **Where the fee is paid decides where it folds** (spec 019, *Legs*), and two of the
    three places can leave a leg empty, which is refused here rather than discovered in
    the middle of a replay:

    * a fee in the **received** asset is taken off what is received, which must stay above
      zero -- a fee of a whole fill's quantity received nothing;
    * a fee in the **given** asset is added to what is given, which must stay above zero --
      a rebate of a whole fill's cost gave nothing.

    A fee in any third asset is a leg of its own and constrains neither. Those two refusals,
    and a `base_asset` equal to the `quote_asset`, are decided by `trade_shape_problem`, the
    one definition `NormalizedFill` applies too.

    Raises:
        TypeError: `key` is not an `EventKey`, `side` is not a `FillSide` (a plain `"buy"`
            included), an asset is not a `str`, or an amount is not a `Decimal`.
        ValueError: any other rule above, or an amount outside the module's amount rule.
    """

    key: EventKey
    base_asset: str
    quote_asset: str
    side: FillSide
    quantity: Decimal
    """In the base asset. Greater than zero."""
    quote_quantity: Decimal
    """In the quote asset. Greater than zero."""
    fee_amount: Decimal
    """Signed: positive is a fee paid, negative a rebate."""
    fee_asset: str | None
    """Required when `fee_amount` is not zero. Ignored when it is."""

    def __post_init__(self) -> None:
        """Refuse a trade that is malformed, or whose shape leaves one of its legs empty.

        Every field rule first, then `trade_shape_problem`, whose precondition they are.
        """
        _require_key(self.key, kind="Trade")
        _require_text(self.base_asset, field="Trade.base_asset")
        _require_text(self.quote_asset, field="Trade.quote_asset")
        _require_side(self.side)
        _require_amount(self.quantity, field="Trade.quantity", minimum=_Bound.POSITIVE)
        _require_amount(self.quote_quantity, field="Trade.quote_quantity", minimum=_Bound.POSITIVE)
        _require_amount(self.fee_amount, field="Trade.fee_amount", minimum=_Bound.ANY)
        if self.fee_asset is not None:
            _require_text(self.fee_asset, field="Trade.fee_asset")
        elif not self.fee_amount.is_zero():
            message = "Trade.fee_asset must name an asset when Trade.fee_amount is not zero"
            raise ValueError(message)
        problem = trade_shape_problem(
            base_asset=self.base_asset,
            quote_asset=self.quote_asset,
            side=self.side,
            quantity=self.quantity,
            quote_quantity=self.quote_quantity,
            fee_amount=self.fee_amount,
            fee_asset=self.fee_asset,
        )
        if problem is not None:
            raise ValueError(_TRADE_SHAPE_MESSAGES[problem])

    @property
    def received_asset(self) -> str:
        """The base asset for a `BUY`, the quote asset for a `SELL`."""
        return _received_then_given(self.side, self.base_asset, self.quote_asset)[0]

    @property
    def received_quantity(self) -> Decimal:
        """What `received_asset` came in, before any fee folds into it."""
        return _received_then_given(self.side, self.quantity, self.quote_quantity)[0]

    @property
    def given_asset(self) -> str:
        """The quote asset for a `BUY`, the base asset for a `SELL`."""
        return _received_then_given(self.side, self.base_asset, self.quote_asset)[1]

    @property
    def given_quantity(self) -> Decimal:
        """What `given_asset` went out, before any fee folds into it."""
        return _received_then_given(self.side, self.quantity, self.quote_quantity)[1]


_TRADE_SHAPE_MESSAGES: Final[Mapping[TradeShapeProblem, str]] = MappingProxyType(
    {
        TradeShapeProblem.SAME_ASSET: (
            "Trade.base_asset and Trade.quote_asset must be different assets"
        ),
        TradeShapeProblem.FEE_CONSUMES_RECEIVED: (
            "Trade.fee_amount, paid in the asset received, must leave a quantity received "
            "greater than zero"
        ),
        TradeShapeProblem.REBATE_EXCEEDS_GIVEN: (
            "Trade.fee_amount, rebated in the asset given, must leave a quantity given "
            "greater than zero"
        ),
    }
)
"""`Trade`'s message for each shape: the text it raised before the rule was shared (#99)."""


@dataclass(frozen=True, slots=True)
class Adjustment:
    """An inflow the owner records: `quantity` of `asset`, at `unit_cost` or at unknown cost.

    **`None` is unknown, and unknown is not zero.** An opening balance entered without a
    cost is held at unknown basis -- in the position's `unknown_basis_quantity`, out of its
    average and out of its realized P&L. Valued at zero instead, it would report its whole
    sale price as profit the next time it was sold. A `unit_cost` of zero is accepted and
    means what it says: a known cost of nothing, such as an airdrop the owner chooses to
    record that way.

    An adjustment of a cash asset is accepted and changes nothing: cash is the unit of
    account, not inventory.

    **The total cost has to fit** (spec 019, R2). It is
    `quantize(multiply(unit_cost, quantity), BASIS_SCALE)`, and two amounts that each pass
    the amount rule can multiply past it: 1E19 units at a unit cost of 1E19. That is refused
    here, where the owner's entry can be corrected, rather than raised out of `replay`, where
    it would stop every position from being computed.

    Raises:
        TypeError: `key` is not an `EventKey`, `asset` is not a `str`, or an amount is not
            a `Decimal`.
        ValueError: `quantity` is not above zero, `unit_cost` is below zero, the total cost
            does not fit, `asset` is blank or not UTF-8-encodable, or an amount is outside
            the module's amount rule.
    """

    key: EventKey
    asset: str
    quantity: Decimal
    """Greater than zero."""
    unit_cost: Decimal | None
    """Cash units per unit, zero or more. `None` is an unknown cost, which is not zero."""

    def __post_init__(self) -> None:
        """Refuse an adjustment that is malformed, or whose total cost cannot be represented."""
        _require_key(self.key, kind="Adjustment")
        _require_text(self.asset, field="Adjustment.asset")
        _require_amount(self.quantity, field="Adjustment.quantity", minimum=_Bound.POSITIVE)
        if self.unit_cost is None:
            return
        _require_amount(self.unit_cost, field="Adjustment.unit_cost", minimum=_Bound.NOT_NEGATIVE)
        try:
            quantize(multiply(self.unit_cost, self.quantity), BASIS_SCALE)
        except InvalidOperation:
            message = (
                "Adjustment.unit_cost times Adjustment.quantity has more than "
                f"{MAX_AMOUNT_INTEGER_DIGITS} digits before the decimal point"
            )
            raise ValueError(message) from None


@dataclass(frozen=True, slots=True)
class Transfer:
    """`quantity` of `asset` moved from one place the owner controls to another.

    **It changes no position**, and that is the point of recording it (I7). Weighted
    average pools an asset across every venue and wallet, so moving a coin between them is
    not an accounting event. A `Transfer` is still part of the input -- it counts in
    `event_count` and in the fingerprint -- because the input changed.

    Raises:
        TypeError: `key` is not an `EventKey`, a text field is not a `str`, or `quantity`
            is not a `Decimal`.
        ValueError: `quantity` is not above zero or is outside the module's amount rule, a
            text field is blank or not UTF-8-encodable, or the two locations are the same.
    """

    key: EventKey
    asset: str
    quantity: Decimal
    """Greater than zero."""
    from_location: str
    to_location: str

    def __post_init__(self) -> None:
        """Refuse a transfer that is malformed or goes nowhere."""
        _require_key(self.key, kind="Transfer")
        _require_text(self.asset, field="Transfer.asset")
        _require_amount(self.quantity, field="Transfer.quantity", minimum=_Bound.POSITIVE)
        _require_text(self.from_location, field="Transfer.from_location")
        _require_text(self.to_location, field="Transfer.to_location")
        if self.from_location == self.to_location:
            message = "Transfer.from_location and Transfer.to_location must differ"
            raise ValueError(message)


type AccountingEvent = Trade | Adjustment | Transfer
"""Anything `replay` consumes."""


@dataclass(frozen=True, slots=True)
class AccountingConfig:
    """Which assets are cash. The only configuration the engine has.

    A `frozenset` and nothing looser, because the configuration is part of the input
    fingerprint and of every result computed under it: a `set` a caller kept a reference to
    and changed afterwards would be a result that no longer describes its own input.

    Raises:
        TypeError: `cash_assets` is not a `frozenset`, or a member is not a `str`.
        ValueError: `cash_assets` is empty, or a member is blank or not UTF-8-encodable.
    """

    cash_assets: frozenset[str] = DEFAULT_CASH_ASSETS

    def __post_init__(self) -> None:
        """Refuse a configuration with no unit of account, or one that is not text."""
        _require_cash_assets(self.cash_assets)


class _Bound(Enum):
    """The lower bound an amount is held to, beyond the amount rule itself."""

    ANY = "any"
    NOT_NEGATIVE = "not negative"
    POSITIVE = "positive"


def _require_amount(value: object, *, field: str, minimum: _Bound) -> None:
    """Refuse an amount outside the module's amount rule, or below `minimum`.

    One `quantize` answers both scale questions, as it does in `NormalizedFill`: a
    `decimal.InvalidOperation` means more integer digits than the scale leaves room for,
    and a result that differs from the input means fractional digits beyond it. Comparing
    values rather than counting digits is what lets trailing zeros through.

    `isinstance(value, Decimal)` is what refuses a `bool`, an `int` and a `float` alike,
    before anything converts one: `Decimal(0.1)` is not one tenth.
    """
    if not isinstance(value, Decimal):
        message = f"{field} must be a Decimal, got {type(value).__name__}"
        raise TypeError(message)
    if not value.is_finite():
        message = f"{field} must be a finite number"
        raise ValueError(message)
    if minimum == _Bound.POSITIVE and value <= 0:
        message = f"{field} must be greater than zero"
        raise ValueError(message)
    if minimum == _Bound.NOT_NEGATIVE and value < 0:
        message = f"{field} must not be negative"
        raise ValueError(message)
    try:
        exact = quantize(value, AMOUNT_SCALE)
    except InvalidOperation:
        message = (
            f"{field} has more than {MAX_AMOUNT_INTEGER_DIGITS} digits before the decimal point"
        )
        raise ValueError(message) from None
    if exact != value:
        message = f"{field} has more than {AMOUNT_SCALE} decimal places"
        raise ValueError(message)


def _require_text(value: object, *, field: str) -> None:
    """Refuse text that is not a `str`, is blank, or cannot be encoded as UTF-8.

    **The encoding is checked here, at construction**, because the fingerprint encodes
    every text field, and a lone surrogate -- `"\\ud800"`, which JSON can carry and Python
    will hold as a `str` -- would otherwise escape from `replay` as a bare
    `UnicodeEncodeError`, far from whatever built the event (the #12 lesson). The message
    names the field; the text is exactly what cannot be rendered.
    """
    if not isinstance(value, str):
        message = f"{field} must be a str, got {type(value).__name__}"
        raise TypeError(message)
    if not value.strip():
        message = f"{field} must not be blank"
        raise ValueError(message)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        message = f"{field} must be text that encodes as UTF-8"
        raise ValueError(message) from None


def _require_side(value: object) -> None:
    """Refuse a side that is not a `FillSide` -- a plain `"buy"` string included.

    Takes `object`, like every check here, so that `mypy` does not read the refusal as dead
    code: the annotation is a promise the type checker keeps for our code, and nobody keeps
    for a value built at run time.
    """
    if not isinstance(value, FillSide):
        message = f"Trade.side must be a FillSide, got {type(value).__name__}"
        raise TypeError(message)


def _require_cash_assets(value: object) -> None:
    """Refuse a set of cash assets that is not a non-empty `frozenset` of text."""
    if not isinstance(value, frozenset):
        message = f"AccountingConfig.cash_assets must be a frozenset, got {type(value).__name__}"
        raise TypeError(message)
    if not value:
        message = "AccountingConfig.cash_assets must name at least one asset"
        raise ValueError(message)
    for asset in value:
        _require_text(asset, field="AccountingConfig.cash_assets")


def _require_key(value: object, *, kind: str) -> None:
    """Refuse a `key` that is not an `EventKey`. The key validated itself when it was built."""
    if not isinstance(value, EventKey):
        message = f"{kind}.key must be an EventKey, got {type(value).__name__}"
        raise TypeError(message)


def _require_utc(value: object) -> datetime:
    """`value` as an aware UTC `datetime`, or a refusal.

    Aware means an offset can be computed, not merely that `tzinfo` is set: a `tzinfo`
    whose `utcoffset` answers `None` is as naive as none at all, and the standard library's
    own test for awareness is this one.

    `astimezone` can overflow: `datetime.min` is a valid aware value at `+05:00`, and five
    hours before it is not a `datetime` at all. The interpreter says so with an
    `OverflowError`, which is not a type anybody validating an event expects to catch, so it
    becomes the `ValueError` every other refusal here is.
    """
    if not isinstance(value, datetime):
        message = f"EventKey.occurred_at must be a datetime, got {type(value).__name__}"
        raise TypeError(message)
    if value.tzinfo is None or value.utcoffset() is None:
        message = "EventKey.occurred_at must be a timezone-aware datetime"
        raise ValueError(message)
    try:
        return value.astimezone(UTC)
    except OverflowError:
        message = "EventKey.occurred_at is outside the range a UTC datetime can represent"
        raise ValueError(message) from None
