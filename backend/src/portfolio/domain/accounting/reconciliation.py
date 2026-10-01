"""Reconciliation: what the replay says is held against what was read as held. Pure (#104).

`replay` cannot see a buy that is older than a venue keeps while its coins are still held:
every later sale fits inside the recorded pool and nothing warns (`docs/accounting.md`, *A
short history does not always show*). What shows the gap is a comparison of two quantities per
asset -- the position the history gives, and the balances read from the wallets and the
venues. `reconcile` is that comparison, and nothing else: it reads no balance and no snapshot,
and the caller hands it three mappings.

## The held side is a lower bound, so the two directions are not symmetric

The balances a caller can read are never everything the owner holds: a wallet that is not
registered, an account at a venue this application does not read, a source that failed. So
(spec 025):

* **`HISTORY_SHORT` -- held above the history -- is a finding.** The owner holds at least what
  was read and the history accounts for less, so acquisitions are missing from it. Reading
  more sources could only widen the gap.
* **`HISTORY_OVER` -- the history above what was read -- is not.** Coins held somewhere this
  application does not read, a withdrawal, a network fee, a trading fee the import did not
  record, and a sale or a conversion the import did not see all produce it, and nothing here
  can tell them apart.

**That rests on what the caller hands in, and this function cannot check it.** The held side
is a lower bound only if every quantity in `wallets` and `exchanges` is a current reading: an
out-of-date one counts coins that may since have moved into another reading, and the sum is
then above what is held. `services/reconciliation.py` leaves such readings out (spec 025, R9).
Even then two current readings are taken at different times -- minutes apart while both
syncs run, and up to the service's age limit when a source has stopped being read without a
recorded failure -- so a `HISTORY_SHORT` is a prompt to look, not a verdict.

## The tolerance is relative to the quantity, and compared exactly

A difference counts as a `MATCH` when it is at most `RECONCILIATION_TOLERANCE_PCT` percent of
the larger of the two sides: `|difference| x 100 <= tolerance x max(history, held)`. It is
relative to the quantity rather than to its value because only two assets have a price.

**No division and no rounding.** Both sides of the inequality are `money.multiply`'s exact
products and the sums are `money.add` and `money.subtract`, so the answer does not depend on
the calling thread's decimal context: two eighteen-place quantities in the billions already
need more than the interpreter's default 28 digits, and a product rounded there would move an
asset across the boundary without a word. The comparison operators on `Decimal` are exact.

## Every figure is carried at `AMOUNT_SCALE`

Each input is added to a zero at eighteen places, so a wallet's eight-place quantity and a
venue's eighteen-place one leave here spelled alike, and an asset missing from a mapping is
`0E-18` rather than absent. Nothing is rounded to get there: `add` keeps the smaller exponent
of its operands and every digit of the sum.

**So "at `AMOUNT_SCALE`" holds for inputs of at most eighteen places**, which is every
quantity this application stores. An input with more is not refused and not rounded: it keeps
its places, and the figures computed from it carry them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.accounting.constants import AMOUNT_SCALE
from portfolio.domain.accounting.events import DEFAULT_CASH_ASSETS
from portfolio.domain.money import add, multiply, require_amount, subtract

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "RECONCILIATION_TOLERANCE_PCT",
    "AssetReconciliation",
    "ReconciliationStatus",
    "reconcile",
]

RECONCILIATION_TOLERANCE_PCT: Final = Decimal(1)
"""The largest difference, in percent of the larger side, that still counts as a match.

**Why one percent** (spec 025). The one-time historical imports hold zero fees, and a venue's
fee is about a tenth of a percent of a trade, so a complete history sits a fraction of a
percent above the balances. One percent keeps that from being reported on every asset.

What it lets through is small unless the missing units were dear: a missing buy moves the
average cost by its share of the holding times how far its price sat from the average,
relative to the average. Under one percent of the holding, that is about one percent or less,
unless it was bought at a price far from the average.
"""

_HUNDRED: Final = Decimal(100)

_ZERO: Final = Decimal((0, (0,), -AMOUNT_SCALE))
"""Zero at `AMOUNT_SCALE` places: what an asset absent from a mapping holds, and the term
every input is added to so that it leaves at that scale."""


class ReconciliationStatus(StrEnum):
    """How one asset's history compares with what was read as held. The member is its wire form.

    * `MATCH` -- the two agree within `RECONCILIATION_TOLERANCE_PCT`.
    * `HISTORY_SHORT` -- more is held than the history accounts for: a finding.
    * `HISTORY_OVER` -- the history accounts for more than was read as held: shown, and not a
      finding, because coins held elsewhere produce it as readily as a sale the import did not
      see. See the module docstring for why the two are not symmetric.
    """

    MATCH = "match"
    HISTORY_SHORT = "history_short"
    HISTORY_OVER = "history_over"


@dataclass(frozen=True, slots=True)
class AssetReconciliation:
    """One non-cash asset: the quantity the history gives, the quantity read, and the verdict.

    * `history_quantity` -- the replayed position's quantity.
    * `wallet_quantity` and `exchange_quantity` -- what the wallets and the venues were read
      as holding; `held_quantity` is their sum.
    * `difference` -- `held_quantity - history_quantity`, signed: positive when more is held
      than the history accounts for.

    Every figure is exact. Each is at `AMOUNT_SCALE` places when the inputs had at most that
    many, and carries the extra places of a finer input otherwise.
    """

    asset: str
    history_quantity: Decimal
    wallet_quantity: Decimal
    exchange_quantity: Decimal
    held_quantity: Decimal
    difference: Decimal
    status: ReconciliationStatus


def reconcile(
    history: Mapping[str, Decimal],
    wallets: Mapping[str, Decimal],
    exchanges: Mapping[str, Decimal],
    *,
    cash_assets: frozenset[str] = DEFAULT_CASH_ASSETS,
) -> tuple[AssetReconciliation, ...]:
    """Compare, per asset, the quantity the history gives with the quantity read as held.

    The assets are the union of the three mappings' keys, **minus the cash assets** -- the
    engine keeps no quantity for the unit of account, so a venue's USDT balance has nothing to
    be compared with -- and minus any asset whose history and held quantities are both zero.
    Sorted by asset. An asset absent from a mapping holds zero there.

    Args:
        history: the replayed quantity per asset, `Position.quantity`.
        wallets: the quantity read from the owner's wallets, per asset.
        exchanges: the quantity read from the owner's exchange accounts, per asset.
        cash_assets: the assets left out; the engine's default set unless told otherwise.

    Raises:
        ValueError: a quantity is negative, a NaN or an infinity. The message names which of
            the three mappings held it, and never the asset or the amount.
        TypeError: a quantity is not a `Decimal`.
    """
    _require_quantities(history, field="history")
    _require_quantities(wallets, field="wallets")
    _require_quantities(exchanges, field="exchanges")
    rows: list[AssetReconciliation] = []
    for asset in sorted((history.keys() | wallets.keys() | exchanges.keys()) - cash_assets):
        history_quantity = add(_ZERO, history.get(asset, _ZERO))
        wallet_quantity = add(_ZERO, wallets.get(asset, _ZERO))
        exchange_quantity = add(_ZERO, exchanges.get(asset, _ZERO))
        held_quantity = add(wallet_quantity, exchange_quantity)
        if history_quantity.is_zero() and held_quantity.is_zero():
            continue
        difference = subtract(held_quantity, history_quantity)
        rows.append(
            AssetReconciliation(
                asset=asset,
                history_quantity=history_quantity,
                wallet_quantity=wallet_quantity,
                exchange_quantity=exchange_quantity,
                held_quantity=held_quantity,
                difference=difference,
                status=_status(history_quantity, held_quantity, difference),
            )
        )
    return tuple(rows)


def _status(history: Decimal, held: Decimal, difference: Decimal) -> ReconciliationStatus:
    """`MATCH` within the tolerance, otherwise the direction of the difference.

    `|difference| x 100 <= tolerance x max(history, held)`, both products exact. `copy_abs`
    only clears the sign bit, where `abs()` is an arithmetic operation that rounds to the
    calling thread's precision. A difference past the tolerance is never zero, so its sign is
    the whole answer.
    """
    allowed = multiply(RECONCILIATION_TOLERANCE_PCT, max(history, held))
    if multiply(difference.copy_abs(), _HUNDRED) <= allowed:
        return ReconciliationStatus.MATCH
    if difference > 0:
        return ReconciliationStatus.HISTORY_SHORT
    return ReconciliationStatus.HISTORY_OVER


def _require_quantities(quantities: Mapping[str, Decimal], *, field: str) -> None:
    """Refuse a mapping holding anything that is not a finite `Decimal` of zero or more.

    A negative quantity is not a fact about anyone's holdings: a position never goes below
    zero, a confirmed balance is a count of units, and a venue's negative balance is refused
    where it is parsed. So it is a defect in whatever built the mapping, and comparing it
    would report a difference computed from something nobody meant.
    """
    for quantity in quantities.values():
        require_amount(quantity, subject=f"reconcile {field}")
        if quantity < 0:
            message = f"reconcile {field} must hold no negative quantity"
            raise ValueError(message)
