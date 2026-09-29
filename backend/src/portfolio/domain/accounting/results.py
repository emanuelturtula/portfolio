"""What `replay` returns: positions, warnings, lots, and the fingerprint of what it was given.

Plain frozen dataclasses with no validation of their own. They are built in one place,
`replay`, from values the engine computed, and a second set of checks here would be a copy
of the engine's invariants that could drift from the property tests that actually hold the
engine to them.

**Every amount is a `Decimal` at exactly eighteen places**, including the zeros, because the
engine carries every amount at `AMOUNT_SCALE` and rounds every split to the same scale. That
is also what #19's `NumericText(18)` columns store, so nothing is rounded between here and
the database.

**Warnings are returned, never logged.** `domain` does not log, and a warning that only
exists in a log line is one the dashboard cannot show beside the number it qualifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from portfolio.domain.money import subtract

if TYPE_CHECKING:
    from decimal import Decimal

    from portfolio.domain.accounting.events import EventKey

__all__ = [
    "AccountingResult",
    "AccountingWarning",
    "Lot",
    "NegativeInventory",
    "Position",
    "PositionFlag",
    "UnattributedFee",
]


class PositionFlag(StrEnum):
    """What a position's figures cannot be taken at face value for. Alphabetical.

    * `HISTORY_INCOMPLETE` -- a disposal of the asset was larger than everything the
      history held. **Sticky**: the pool was emptied and replay carried on, but the
      realized P&L of that disposal was computed against a history that is missing
      something, and nothing later can say what.
    * `UNATTRIBUTED_FEE` -- a fee charged to this asset's trades, paid in a third asset,
      could not be valued, so this asset's basis (or, for a sale, its proceeds) leaves the
      fee out. **Sticky**, for the same reason.
    * `UNKNOWN_BASIS` -- some of the quantity held has no known cost. **Not sticky**: it
      describes the pool as it stands, and clears once those units have been disposed of.
    """

    HISTORY_INCOMPLETE = "history_incomplete"
    UNATTRIBUTED_FEE = "unattributed_fee"
    UNKNOWN_BASIS = "unknown_basis"


@dataclass(frozen=True, slots=True)
class Position:
    """One non-cash asset's pool, across every venue and wallet, after the last event.

    `quantity` is everything held, known cost or not. `cost_basis` is the basis of the
    known-cost part only, and `average_cost` is that basis over that part's quantity --
    derived for display, rounded once, and never fed back into the basis. It is `None` when
    no known-cost quantity is held, and also when the quotient is 10**20 cash units per unit
    or more, which no `NumericText(18)` column can hold (spec 019, R1): a buy of one
    quintillionth of a coin for a hundred dollars is valid input and reaches it.

    `realized_pnl` is proceeds minus basis over every sale of known-cost units for cash.
    `unmatched_proceeds` is the proceeds of units whose cost is unknown -- unknown-basis
    units, or units sold beyond what the history held -- kept out of `realized_pnl` because
    counting them there would report their whole price as profit.
    """

    asset: str
    quantity: Decimal
    unknown_basis_quantity: Decimal
    cost_basis: Decimal
    average_cost: Decimal | None
    realized_pnl: Decimal
    unmatched_proceeds: Decimal
    flags: frozenset[PositionFlag]

    @property
    def known_quantity(self) -> Decimal:
        """The part of `quantity` whose cost is known: what `average_cost` is an average over."""
        return subtract(self.quantity, self.unknown_basis_quantity)


@dataclass(frozen=True, slots=True)
class NegativeInventory:
    """A disposal of `asset` at `key` was `shortfall` larger than everything the pool held.

    The pool was emptied rather than taken below zero (I1), and replay carried on. `key`
    carries the moment, which is what the owner needs to go looking for the missing history:
    a deposit never imported, or a fill older than the venue's retention.
    """

    key: EventKey
    asset: str
    shortfall: Decimal


@dataclass(frozen=True, slots=True)
class UnattributedFee:
    """`quantity` of a fee paid in `fee_asset` at `key` could not be valued at a known cost.

    The fee asset's pool was short, or held that part at unknown basis, so the fee's value is
    understated by that much. `charged_to` is the asset whose figures leave it out -- the
    asset received by a buy or a swap, the asset given by a sale -- and carries
    `UNATTRIBUTED_FEE`. It is `None` for a conversion between two cash assets, which has no
    position to charge (spec 019, R3).
    """

    key: EventKey
    fee_asset: str
    quantity: Decimal
    charged_to: str | None


type AccountingWarning = NegativeInventory | UnattributedFee
"""Anything `AccountingResult.warnings` holds."""


@dataclass(frozen=True, slots=True)
class Lot:
    """One acquisition into a non-cash asset, with the cost this method attributed to it.

    `quantity` is everything acquired and `unknown_basis_quantity` the part of it with no
    known cost, as on `Position` (spec 019, R5). An acquisition at unknown cost -- an
    `Adjustment` without a unit cost, a third-asset rebate, or a swap whose given side had
    no known-cost part -- is a lot with a `cost_basis` of zero and all of its quantity
    unknown.

    **#19 persists these; nothing here reads them.** They are emitted now, stamped with the
    method that attributed their cost, so that a FIFO function written later can store its
    own lots beside these without a migration.
    """

    asset: str
    key: EventKey
    quantity: Decimal
    cost_basis: Decimal
    unknown_basis_quantity: Decimal


@dataclass(frozen=True, slots=True)
class AccountingResult:
    """Everything one replay produced, and what it was produced from.

    * `method` and `engine_version` -- what computed it.
    * `input_fingerprint` -- the SHA-256 of the method, the engine version, the cash assets
      and every event after deduplication. Equal fingerprints mean an equal result, which is
      what lets #19 skip a recompute.
    * `positions` -- one per non-cash asset any `Trade` leg or `Adjustment` touched, sorted
      by asset. A `Transfer` never creates one.
    * `warnings` -- in event order, and within one trade in the order its legs were worked
      (spec 019, R4).
    * `lots` -- one per acquisition into a non-cash asset, in the same order.
    * `unallocated_costs` -- known value that belongs to no position: a conversion's fee;
      the value given in a swap whose received side has no known-cost part; and, in a swap
      from units partly of unknown cost, the share of the fee that belongs to the received
      units of unknown cost (spec 019, R11).
    * `event_count` -- after deduplication, so a log read twice counts once.
    """

    method: str
    engine_version: int
    input_fingerprint: str
    positions: tuple[Position, ...]
    warnings: tuple[AccountingWarning, ...]
    lots: tuple[Lot, ...]
    unallocated_costs: Decimal
    event_count: int
