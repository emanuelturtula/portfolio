"""The engine's identity and its scales: what a result says it was produced by, and at what grain.

**`ENGINE_VERSION` is a promise about results, not about code.** Bump it with any change that
can give a different answer for the same events -- a rounding point moved, a fee folded
elsewhere, a warning emitted in a new case -- and not for a refactor that cannot. It is part
of the input fingerprint, because #19 skips a recompute when the fingerprint is unchanged: a
fixed engine bug that left the fingerprint alone would leave the wrong snapshot in place for
good.

**Every scale is eighteen**, the value of `db.models.FILL_SCALE`. It is spelled again here
rather than imported, because `db` sits above `domain` and `domain` imports nothing from the
application. Three reasons it has to be this number and not a larger one: a sum of stored
fill amounts stays exact at eighteen places, every value the engine derives fits the
`NumericText(18)` columns #19 writes it into, and eighteen leaves `MONEY_PRECISION - 18` = 20
digits in front of the point, which is the engine's range (spec 019, *Risks*).
"""

from __future__ import annotations

from typing import Final

from portfolio.domain.money import MONEY_PRECISION

__all__ = [
    "AMOUNT_SCALE",
    "AVERAGE_COST_SCALE",
    "BASIS_SCALE",
    "ENGINE_VERSION",
    "MAX_AMOUNT_INTEGER_DIGITS",
    "METHOD",
    "QUANTITY_SCALE",
]

METHOD: Final = "weighted_average"
"""The cost-basis method these results are computed by.

Stored beside every lot by #19, so that a later FIFO function can write its own lots into
the same table under its own method, without a migration.
"""

ENGINE_VERSION: Final = 1
"""Bumped by any change that can alter the result for the same input. See the module docstring."""

AMOUNT_SCALE: Final = 18
"""The most fractional digits an event amount may carry, by value (spec 019, R9).

`NormalizedFill`'s rule, so that every stored fill converts into a `Trade`. An amount is
also carried through the engine at exactly this many places, which is what keeps every
integer the arithmetic builds bounded by the amounts themselves.
"""

MAX_AMOUNT_INTEGER_DIGITS: Final = MONEY_PRECISION - AMOUNT_SCALE
"""Digits an event amount may carry before the point: 38 - 18 = 20. `NormalizedFill`'s rule too."""

QUANTITY_SCALE: Final = 18
"""The places a quantity split off a pool is rounded to, when a split has to round at all."""

BASIS_SCALE: Final = 18
"""The places a basis or a proceeds share is rounded to, and an `Adjustment` cost."""

AVERAGE_COST_SCALE: Final = 18
"""The places `Position.average_cost` is rounded to. A display figure, never fed back."""
