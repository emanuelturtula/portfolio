"""Weighted-average cost basis: a pure function from trade events to positions.

`replay(events, config)` turns fills, the owner's adjustments and transfers into what the
product exists to report -- per non-cash asset, the quantity held, the cost of the part whose
cost is known, the average cost, the realized P&L, and what could not be accounted for. The
contract is spec 019; the method and its caveats are in `docs/accounting.md` and ADR 0001.

**Pure, and enforced as pure.** No I/O, no clock, no logging, no randomness and no decimal
context: the `domain-is-pure` import contract forbids the modules that could do any of it,
and an AST test forbids the clock calls an import contract cannot see. The same events and
configuration always give the same result, and the same `input_fingerprint`.

`value_position` and `value_portfolio` (#19) combine a position with a current price. They
are here rather than in the service that looks the price up so that the arithmetic is held to
the engine's exactness rules and its coverage floor; see `valuation`.
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
    TradeShapeProblem,
    Transfer,
    trade_shape_problem,
)
from portfolio.domain.accounting.fingerprint import event_kind
from portfolio.domain.accounting.replay import ConflictingEventError, replay
from portfolio.domain.accounting.results import (
    AccountingResult,
    Lot,
    NegativeInventory,
    Position,
    PositionFlag,
    UnattributedFee,
)
from portfolio.domain.accounting.valuation import (
    RETURN_PCT_SCALE,
    VALUE_SCALE,
    Exclusion,
    ExclusionReason,
    PortfolioTotals,
    PositionValue,
    value_portfolio,
    value_position,
)

__all__ = [
    "AVERAGE_COST_SCALE",
    "BASIS_SCALE",
    "DEFAULT_CASH_ASSETS",
    "ENGINE_VERSION",
    "METHOD",
    "QUANTITY_SCALE",
    "RETURN_PCT_SCALE",
    "VALUE_SCALE",
    "AccountingConfig",
    "AccountingResult",
    "Adjustment",
    "ConflictingEventError",
    "EventKey",
    "Exclusion",
    "ExclusionReason",
    "Lot",
    "NegativeInventory",
    "PortfolioTotals",
    "Position",
    "PositionFlag",
    "PositionValue",
    "Trade",
    "TradeShapeProblem",
    "Transfer",
    "UnattributedFee",
    "event_kind",
    "replay",
    "trade_shape_problem",
    "value_portfolio",
    "value_position",
]
