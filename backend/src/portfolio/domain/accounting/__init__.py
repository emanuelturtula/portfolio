"""Weighted-average cost basis: a pure function from trade events to positions.

`replay(events, config)` turns fills, the owner's adjustments and transfers into what the
product exists to report -- per non-cash asset, the quantity held, the cost of the part whose
cost is known, the average cost, the realized P&L, and what could not be accounted for. The
contract is spec 019; the method and its caveats are in `docs/accounting.md` and ADR 0001.

**Pure, and enforced as pure.** No I/O, no clock, no logging, no randomness and no decimal
context: the `domain-is-pure` import contract forbids the modules that could do any of it,
and an AST test forbids the clock calls an import contract cannot see. The same events and
configuration always give the same result, and the same `input_fingerprint`.
"""

from portfolio.domain.accounting.constants import (
    AVERAGE_COST_SCALE,
    BASIS_SCALE,
    ENGINE_VERSION,
    METHOD,
    QUANTITY_SCALE,
)
from portfolio.domain.accounting.events import (
    DEFAULT_CASH_ASSETS,
    AccountingConfig,
    Adjustment,
    EventKey,
    Trade,
    Transfer,
)
from portfolio.domain.accounting.replay import ConflictingEventError, replay
from portfolio.domain.accounting.results import (
    AccountingResult,
    Lot,
    NegativeInventory,
    Position,
    PositionFlag,
    UnattributedFee,
)

__all__ = [
    "AVERAGE_COST_SCALE",
    "BASIS_SCALE",
    "DEFAULT_CASH_ASSETS",
    "ENGINE_VERSION",
    "METHOD",
    "QUANTITY_SCALE",
    "AccountingConfig",
    "AccountingResult",
    "Adjustment",
    "ConflictingEventError",
    "EventKey",
    "Lot",
    "NegativeInventory",
    "Position",
    "PositionFlag",
    "Trade",
    "Transfer",
    "UnattributedFee",
    "replay",
]
