"""`replay`: events in, positions out, by weighted average cost. A pure function.

The whole contract is spec 019's *Design* section, and the rulings recorded under it. What
this module adds is how the arithmetic keeps the invariants exact rather than approximately
true.

**The state of a pool is `(Qk, C, Qu, R, U)` and its flags**: known-basis quantity, known
basis, unknown-basis quantity, realized P&L and unmatched proceeds. `C` is the state. The
average cost is derived from it for display and never fed back, which is why a hundred
partial sales do not leak a hundred rounding residues out of the basis. The rejected
alternative -- keeping `(Q, average)` and deriving the basis as `average * Q` -- makes the
average exact by construction and rounds it on every acquisition instead.

**Every operation on an amount is one of `money`'s**: `add`, `subtract` and `multiply` are
exact, and `divide` is the one rounding, half to even, to eighteen places. There is no bare
`+`, `-`, `*` or `/` on a `Decimal` in this package, because each of those rounds to the
calling thread's context, and a result that depends on who called it is not a pure
function. `abs` and unary minus are arithmetic too, and are spelled `copy_abs` and
`copy_negate`, which only touch the sign. Comparisons are exact and are used freely.

**A split rounds once, and its complement is a subtraction.** When a disposal takes part
of a pool, the part taken comes from `divide` and the part left is `total - part`. Nothing
is rounded twice, so the two parts always add back to the whole, and conservation (I8) holds
with no tolerance at all.

**A disposal that reaches the pool takes all of it** -- quantity and basis -- so a pool whose
known quantity reaches zero has a basis of exactly zero (I4). The rounding residue of every
earlier partial disposal lands in the realized P&L of the disposal that empties it.

**Every amount is carried at exactly eighteen places.** An event amount is validated by
value, so `1.000...0` with fifty zeros is a legal quantity; it is re-expressed at
`AMOUNT_SCALE` on the way in, which is exact, and which keeps every integer `money` builds as
small as the amounts themselves rather than as wide as the widest spelling a caller chose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from portfolio.domain.accounting.constants import (
    AMOUNT_SCALE,
    AVERAGE_COST_SCALE,
    BASIS_SCALE,
    ENGINE_VERSION,
    METHOD,
    QUANTITY_SCALE,
)
from portfolio.domain.accounting.events import (
    AccountingConfig,
    Adjustment,
    Trade,
    Transfer,
)
from portfolio.domain.accounting.fingerprint import event_kind, fingerprint
from portfolio.domain.accounting.results import (
    AccountingResult,
    Lot,
    NegativeInventory,
    Position,
    PositionFlag,
    UnattributedFee,
)
from portfolio.domain.money import add, divide, multiply, quantize, subtract

if TYPE_CHECKING:
    from collections.abc import Iterable

    from portfolio.domain.accounting.events import AccountingEvent, EventKey
    from portfolio.domain.accounting.results import AccountingWarning

__all__ = ["ConflictingEventError", "replay"]

_ZERO = Decimal((0, (0,), -AMOUNT_SCALE))
"""Zero at the scale every amount is carried at, so an untouched field is `0E-18` too."""

_DEFAULT_CONFIG = AccountingConfig()


class ConflictingEventError(ValueError):
    """Two events share an identity, `(kind, source, external_id)`, and differ in content.

    A repeat with **equal** content is the same event read twice and is counted once (I6).
    A repeat with different content cannot come from stored rows -- the database's unique
    constraints make it unreachable -- so it means the input is corrupt, and no answer
    computed from either version would be one to show.

    **The message names the kind and nothing else**: the source and the external id are on
    the exception as attributes for a caller that needs them, and are kept out of the text,
    which is what ends up in a log. An external id is a trade number.
    """

    def __init__(self, kind: str, source: str, external_id: str) -> None:
        """Record the identity both events share."""
        super().__init__(
            f"two {kind} events share a source and an external_id and differ in content"
        )
        self.kind = kind
        self.source = source
        self.external_id = external_id


def replay(
    events: Iterable[AccountingEvent],
    config: AccountingConfig = _DEFAULT_CONFIG,
) -> AccountingResult:
    """Replay `events` by weighted average cost, and report where every non-cash asset stands.

    The answer depends on the events and the configuration alone: not on the order the
    events arrive in, not on how many times one of them arrives, not on the clock and not on
    the calling thread's decimal context. Duplicates are dropped (I6), the rest are put in
    `(occurred_at, source, external_id, kind)` order, and each is applied in turn.

    **Nothing about the owner's history raises.** A sale larger than the pool, a fee in an
    asset nobody bought, a swap from units of unknown cost: each is reported, as a warning,
    a flag or unmatched proceeds, and replay carries on. What raises is input that is not a
    history at all.

    Raises:
        TypeError: an item of `events` is not a `Trade`, an `Adjustment` or a `Transfer`,
            or `config` is not an `AccountingConfig`.
        ConflictingEventError: two events share an identity and differ in content.
        decimal.InvalidOperation: a basis, a quantity or proceeds summed past 10**20 across
            many events (spec 019, *Risks*): past the range `NumericText(18)` can store.
    """
    _require_config(config)
    ordered = _deduplicated_in_order(events)
    ledger = _Ledger(cash_assets=config.cash_assets)
    for event in ordered:
        # A `Transfer` is neither. Weighted average pools an asset across every location, so
        # a relocation is not an accounting event (I7). It is still input: it is counted,
        # and it is in the fingerprint.
        if isinstance(event, Trade):
            ledger.apply_trade(event)
        elif isinstance(event, Adjustment):
            ledger.apply_adjustment(event)
    return AccountingResult(
        method=METHOD,
        engine_version=ENGINE_VERSION,
        input_fingerprint=fingerprint(ordered, config),
        positions=ledger.positions(),
        warnings=tuple(ledger.warnings),
        lots=tuple(ledger.lots),
        unallocated_costs=ledger.unallocated_costs,
        event_count=len(ordered),
    )


def _deduplicated_in_order(events: Iterable[AccountingEvent]) -> list[AccountingEvent]:
    """`events` with repeats dropped, in replay order.

    Deduplication is by identity, and the sort key extends the identity with the instant,
    so after deduplication no two events share a sort key: the order is total, and does not
    depend on the order the events arrived in -- which is what makes a permutation of the
    input fingerprint identically (I3).
    """
    by_identity: dict[tuple[str, str, str], AccountingEvent] = {}
    for item in events:
        event = _require_event(item)
        kind = event_kind(event)
        identity = (kind, event.key.source, event.key.external_id)
        seen = by_identity.setdefault(identity, event)
        if seen != event:
            raise ConflictingEventError(kind, event.key.source, event.key.external_id)
    return sorted(
        by_identity.values(),
        key=lambda event: (
            event.key.occurred_at,
            event.key.source,
            event.key.external_id,
            event_kind(event),
        ),
    )


def _require_config(value: object) -> None:
    """Refuse a configuration that is not an `AccountingConfig`, which validated itself.

    Takes `object` so that `mypy` does not read the refusal as dead code; the annotation on
    `replay` is a promise nobody keeps for a value built at run time.
    """
    if not isinstance(value, AccountingConfig):
        message = f"replay requires an AccountingConfig, got {type(value).__name__}"
        raise TypeError(message)


def _require_event(value: object) -> AccountingEvent:
    """`value`, if it is one of the three events, which validated themselves when built."""
    if not isinstance(value, Trade | Adjustment | Transfer):
        message = (
            f"replay accepts Trade, Adjustment and Transfer events, got {type(value).__name__}"
        )
        raise TypeError(message)
    return value


@dataclass(slots=True)
class _Pool:
    """One non-cash asset's running state. Mutable, and never seen outside this module."""

    known_quantity: Decimal = _ZERO
    basis: Decimal = _ZERO
    unknown_quantity: Decimal = _ZERO
    realized: Decimal = _ZERO
    unmatched: Decimal = _ZERO
    history_incomplete: bool = False
    unattributed_fee: bool = False


@dataclass(frozen=True, slots=True)
class _Disposal:
    """What `_Ledger.dispose` took: the known part, its basis, and the part with no known cost.

    `uncovered` is the unknown-basis part taken plus any shortfall -- everything disposed of
    that no known cost stands behind.
    """

    known: Decimal
    basis: Decimal
    uncovered: Decimal


@dataclass(slots=True)
class _Ledger:
    """Every pool, and what replay has said so far. The mutable half of a pure function."""

    cash_assets: frozenset[str]
    pools: dict[str, _Pool] = field(default_factory=dict)
    warnings: list[AccountingWarning] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    unallocated_costs: Decimal = _ZERO

    def apply_trade(self, trade: Trade) -> None:
        """Apply one fill: fold the fee, work the legs in order, and book the outcome.

        The order of work is fixed (spec 019, R4) -- the given leg, then a third-asset fee
        leg, then the received leg -- because it is the order the warnings and lots come out
        in, and a result has to be the same every time.
        """
        key = trade.key
        received_asset, given_asset = trade.received_asset, trade.given_asset
        received = _carried(trade.received_quantity)
        given = _carried(trade.given_quantity)
        fee = _carried(trade.fee_amount)
        fee_asset = trade.fee_asset
        # A zero fee is no leg at all (spec 019, R8), whatever asset it names.
        folded = False
        third_asset: str | None = None
        if not fee.is_zero():
            if fee_asset == received_asset:
                received, folded = subtract(received, fee), True
            elif fee_asset == given_asset:
                given, folded = add(given, fee), True
            else:
                third_asset = fee_asset
        received_is_cash = received_asset in self.cash_assets
        given_is_cash = given_asset in self.cash_assets
        # The non-cash principal whose figures a fee is part of (spec 019, R3).
        charged_to = (
            None
            if received_is_cash and given_is_cash
            else (given_asset if received_is_cash else received_asset)
        )

        # 1. The given leg. Cash is worth its amount; anything else its carried cost.
        disposal = (
            _Disposal(known=given, basis=given, uncovered=_ZERO)
            if given_is_cash
            else self.dispose(given_asset, given, key)
        )
        # 2. A fee in a third asset, as a disposal or a rebate acquisition.
        fee_value = (
            _ZERO
            if third_asset is None
            else self.fee_leg(third_asset, fee, key, charged_to=charged_to)
        )
        # 3. The received leg, by the shape of the trade.
        if received_is_cash and given_is_cash:
            # A conversion. Both sides are pinned at 1, so no position changes, and a fee's
            # known value has nothing to attach to. A folded fee is in cash by definition.
            self.unallocated_costs = add(self.unallocated_costs, fee if folded else fee_value)
        elif given_is_cash:
            # A buy: the cash given, and the fee's value, become the basis.
            self.acquire(received_asset, received, add(disposal.basis, fee_value), _ZERO, key)
        elif received_is_cash:
            self._book_sale(given_asset, given, subtract(received, fee_value), disposal)
        else:
            self._book_swap(
                received_asset, received, given, add(disposal.basis, fee_value), disposal, key
            )

    def _book_sale(
        self, asset: str, given: Decimal, proceeds: Decimal, disposal: _Disposal
    ) -> None:
        """Realize a sale of `given` units of `asset` for `proceeds`, net of every fee.

        The proceeds split in the proportion the quantity did: the share that sold
        known-cost units is matched against their basis and realized, and the share that
        sold units of unknown cost -- or units the history never held -- is unmatched.
        The unmatched share is the complement, by subtraction.
        """
        pool = self.pools[asset]
        matched = (
            proceeds
            if disposal.uncovered.is_zero()
            else divide(multiply(proceeds, disposal.known), given, BASIS_SCALE)
        )
        pool.realized = add(pool.realized, subtract(matched, disposal.basis))
        pool.unmatched = add(pool.unmatched, subtract(proceeds, matched))

    def _book_swap(
        self,
        asset: str,
        received: Decimal,
        given: Decimal,
        value: Decimal,
        disposal: _Disposal,
        key: EventKey,
    ) -> None:
        """Carry `value` -- the given leg's basis and the fee's -- over to `received` units.

        Nothing is realized: no price exists in this system to realize it at, and carried
        cost is weighted average's own valuation of what was given. The received units take
        the known/unknown proportion of the units given for them. When none of them have a
        known cost, the value has no known quantity to attach to, and it goes to
        `unallocated_costs` rather than into a basis over zero units.
        """
        known_in = (
            received
            if disposal.uncovered.is_zero()
            else divide(multiply(received, disposal.known), given, QUANTITY_SCALE)
        )
        if known_in > 0:
            self.acquire(asset, known_in, value, subtract(received, known_in), key)
        else:
            self.acquire(asset, _ZERO, _ZERO, received, key)
            self.unallocated_costs = add(self.unallocated_costs, value)

    def apply_adjustment(self, adjustment: Adjustment) -> None:
        """Acquire an adjustment's units, at its cost or at unknown cost. Cash changes nothing.

        The cost is `quantize(multiply(unit_cost, quantity), BASIS_SCALE)`: the one rounding
        in replay that is not a `divide`. `Adjustment` refused, at construction, any cost this
        cannot represent.
        """
        if adjustment.asset in self.cash_assets:
            return
        quantity = _carried(adjustment.quantity)
        if adjustment.unit_cost is None:
            self.acquire(adjustment.asset, _ZERO, _ZERO, quantity, adjustment.key)
            return
        cost = quantize(multiply(_carried(adjustment.unit_cost), quantity), BASIS_SCALE)
        self.acquire(adjustment.asset, quantity, cost, _ZERO, adjustment.key)

    def fee_leg(
        self, asset: str, fee: Decimal, key: EventKey, *, charged_to: str | None
    ) -> Decimal:
        """Work a fee paid in a third asset, and return its known value.

        * **Cash** is worth its amount, signed: a rebate is worth a negative amount.
        * **A non-cash fee paid** is disposed of like any other leg, and is worth the basis
          it takes with it -- its carried cost, not a market price, because no historical
          price exists here (spec 019, I5). Whatever part of it has no known cost is
          reported as an `UnattributedFee` and flags the position it was charged to.
        * **A non-cash rebate** is an acquisition at unknown basis, worth nothing known.
        """
        if asset in self.cash_assets:
            return fee
        if fee > 0:
            disposal = self.dispose(asset, fee, key)
            if disposal.uncovered > 0:
                self.warnings.append(UnattributedFee(key, asset, disposal.uncovered, charged_to))
                if charged_to is not None:
                    self.pool(charged_to).unattributed_fee = True
            return disposal.basis
        self.acquire(asset, _ZERO, _ZERO, fee.copy_abs(), key)
        return _ZERO

    def pool(self, asset: str) -> _Pool:
        """The pool for `asset`, opened empty the first time anything touches it."""
        return self.pools.setdefault(asset, _Pool())

    def acquire(
        self,
        asset: str,
        known_quantity: Decimal,
        basis: Decimal,
        unknown_quantity: Decimal,
        key: EventKey,
    ) -> None:
        """Add to a pool, and record the lot."""
        pool = self.pool(asset)
        pool.known_quantity = add(pool.known_quantity, known_quantity)
        pool.basis = add(pool.basis, basis)
        pool.unknown_quantity = add(pool.unknown_quantity, unknown_quantity)
        self.lots.append(
            Lot(
                asset=asset,
                key=key,
                quantity=add(known_quantity, unknown_quantity),
                cost_basis=basis,
                unknown_basis_quantity=unknown_quantity,
            )
        )

    def dispose(self, asset: str, amount: Decimal, key: EventKey) -> _Disposal:
        """Take `amount` out of a pool, proportionally from its known and unknown parts.

        **At or beyond the whole pool it takes everything**, basis included, and leaves the
        pool at exactly zero. Beyond it, the excess is a shortfall: it is reported with the
        moment it happened and the asset is flagged, and the quantity never goes below zero
        (I1).

        **Short of the whole pool it takes a proportional share.** The known part is the one
        rounded quantity, and the unknown part is its complement. The basis given up is the
        one rounded amount -- unless the known part taken is all of it, in which case it is
        all of the basis, by the rule that empties a pool exactly.
        """
        pool = self.pool(asset)
        total = add(pool.known_quantity, pool.unknown_quantity)
        if amount >= total:
            known, basis = pool.known_quantity, pool.basis
            pool.known_quantity = pool.basis = pool.unknown_quantity = _ZERO
            shortfall = subtract(amount, total)
            if shortfall > 0:
                self.warnings.append(NegativeInventory(key, asset, shortfall))
                pool.history_incomplete = True
            return _Disposal(known=known, basis=basis, uncovered=subtract(amount, known))
        known = (
            amount
            if pool.unknown_quantity.is_zero()
            else divide(multiply(amount, pool.known_quantity), total, QUANTITY_SCALE)
        )
        basis = (
            pool.basis
            if known == pool.known_quantity
            else divide(multiply(pool.basis, known), pool.known_quantity, BASIS_SCALE)
        )
        uncovered = subtract(amount, known)
        pool.known_quantity = subtract(pool.known_quantity, known)
        pool.basis = subtract(pool.basis, basis)
        pool.unknown_quantity = subtract(pool.unknown_quantity, uncovered)
        return _Disposal(known=known, basis=basis, uncovered=uncovered)

    def positions(self) -> tuple[Position, ...]:
        """Every pool as a `Position`, sorted by asset."""
        return tuple(_position(asset, self.pools[asset]) for asset in sorted(self.pools))


def _position(asset: str, pool: _Pool) -> Position:
    """A pool as the result reports it, with its average derived and its flags collected."""
    flags: set[PositionFlag] = set()
    if pool.unknown_quantity > 0:
        flags.add(PositionFlag.UNKNOWN_BASIS)
    if pool.history_incomplete:
        flags.add(PositionFlag.HISTORY_INCOMPLETE)
    if pool.unattributed_fee:
        flags.add(PositionFlag.UNATTRIBUTED_FEE)
    return Position(
        asset=asset,
        quantity=add(pool.known_quantity, pool.unknown_quantity),
        unknown_basis_quantity=pool.unknown_quantity,
        cost_basis=pool.basis,
        average_cost=_average_cost(pool),
        realized_pnl=pool.realized,
        unmatched_proceeds=pool.unmatched,
        flags=frozenset(flags),
    )


def _average_cost(pool: _Pool) -> Decimal | None:
    """`C / Qk` at `AVERAGE_COST_SCALE`, or `None` where there is no such figure to show.

    `None` when no known-cost quantity is held, and also when the quotient is 10**20 or
    more (spec 019, R1): `divide` refuses a result past `MONEY_PRECISION`, and a display
    figure is not a reason for a replay to fail. Validated input reaches it -- one
    quintillionth of a coin bought for a hundred dollars -- and the basis and quantity
    beside it are still reported exactly.
    """
    if pool.known_quantity.is_zero():
        return None
    try:
        return divide(pool.basis, pool.known_quantity, AVERAGE_COST_SCALE)
    except InvalidOperation:
        return None


def _carried(value: Decimal) -> Decimal:
    """An event amount at exactly `AMOUNT_SCALE` places. Exact: validation bounded its value."""
    return quantize(value, AMOUNT_SCALE)
