"""Reads and writes of the `manual_adjustments` table (#18).

Queries and nothing else: no validation, no clock, no policy about what makes an adjustment
acceptable. The repository is handed an `AsyncSession` and it does not commit -- the service
that opened the unit of work decides when it ends, and it recomputes the snapshot after.

**Every lookup is scoped by `user_id`**, including the one that takes a primary key, for the
reason `WalletRepository` gives: another owner's adjustment is then an ordinary not-found, never
a leak and never a confirmation that the id exists.

**Nothing is compared or ordered in SQL but integers.** `quantity` and `unit_cost` are `TEXT`
money and `occurred_at` is `TEXT` time, and SQLite compares either by an affinity rule that
agrees with the value only while every row was written by one code path. The reads are ordered
by `id`, and the service orders the list by `occurred_at` in Python.

## Two shapes of a row

* The CRUD methods hand the service the mapped `ManualAdjustment`, as `WalletRepository` does.
  The service snapshots it into a view before it commits.
* `list_adjustments_for_accounting` returns `AdjustmentRecord`s: frozen, and only the columns
  the engine's `Adjustment` is built from. The recompute hands them to a worker thread, where an
  ORM row attached to an async session must not travel. **The note is not among them**: it is
  free text the owner wrote, the accounting has no use for it, and a column never loaded is a
  column that cannot reach a log or a snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from portfolio.db.models import ManualAdjustment

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AdjustmentRecord",
    "ManualAdjustmentRepository",
    "select_adjustments_for_accounting",
]


@dataclass(frozen=True, slots=True)
class AdjustmentRecord:
    """One stored adjustment as the accounting reads it: what builds the engine's `Adjustment`.

    `id` is the event's identity (`services.accounting.external_id_of`); `asset`, `quantity`,
    `unit_cost` and `occurred_at` are copied into the event as stored. `unit_cost` is `None`
    for an unknown cost, which is not zero.
    """

    id: int
    asset: str
    quantity: Decimal
    unit_cost: Decimal | None
    occurred_at: datetime


class ManualAdjustmentRepository:
    """Every query this application makes against `manual_adjustments`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: int) -> list[ManualAdjustment]:
        """Every adjustment an owner has recorded, by id.

        By id because it is an `INTEGER`: the order the owner sees -- by `occurred_at`, then
        id -- is the service's to apply, in Python, on the datetimes rather than their text.
        """
        result = await self._session.scalars(
            select(ManualAdjustment)
            .where(ManualAdjustment.user_id == user_id)
            .order_by(ManualAdjustment.id)
        )
        return list(result)

    async def get(self, user_id: int, adjustment_id: int) -> ManualAdjustment | None:
        """One adjustment by id, provided it belongs to this owner."""
        found: ManualAdjustment | None = await self._session.scalar(
            select(ManualAdjustment).where(
                ManualAdjustment.id == adjustment_id,
                ManualAdjustment.user_id == user_id,
            )
        )
        return found

    async def add(
        self,
        *,
        user_id: int,
        asset: str,
        quantity: Decimal,
        unit_cost: Decimal | None,
        occurred_at: datetime,
        note: str,
        created_at: datetime,
    ) -> ManualAdjustment:
        """Insert an adjustment the caller has already validated, and flush for its id.

        `updated_at` starts equal to `created_at`, for the reason `WalletRepository.add` gives.
        The flush is what assigns the `AUTOINCREMENT` id, which the service logs and returns.
        """
        adjustment = ManualAdjustment(
            user_id=user_id,
            asset=asset,
            quantity=quantity,
            unit_cost=unit_cost,
            occurred_at=occurred_at,
            note=note,
            created_at=created_at,
            updated_at=created_at,
        )
        self._session.add(adjustment)
        await self._session.flush()
        return adjustment

    async def update(
        self,
        adjustment: ManualAdjustment,
        *,
        asset: str,
        quantity: Decimal,
        unit_cost: Decimal | None,
        occurred_at: datetime,
        note: str,
        updated_at: datetime,
    ) -> None:
        """Replace the five editable fields in place, and stamp `updated_at`.

        A full replacement, as `PUT` is: `unit_cost=None` clears a cost to unknown rather than
        leaving the old one. `user_id`, `id` and `created_at` are never touched.
        """
        adjustment.asset = asset
        adjustment.quantity = quantity
        adjustment.unit_cost = unit_cost
        adjustment.occurred_at = occurred_at
        adjustment.note = note
        adjustment.updated_at = updated_at
        await self._session.flush()

    async def delete(self, adjustment: ManualAdjustment) -> None:
        """Delete the row. Its id is never reused: the table is `AUTOINCREMENT`."""
        await self._session.delete(adjustment)
        await self._session.flush()

    async def list_adjustments_for_accounting(self, user_id: int) -> list[AdjustmentRecord]:
        """Every adjustment `user_id` has recorded, as plain records, by id.

        The whole list, every time, for the reason `list_fills_for_accounting` gives: the engine
        replays from the first event. **The note is never loaded**: the statement names its
        columns, and `select_adjustments_for_accounting` is public so that a test can compile it
        and see.
        """
        result = await self._session.execute(select_adjustments_for_accounting(user_id))
        return [
            AdjustmentRecord(
                id=row.id,
                asset=row.asset,
                quantity=row.quantity,
                unit_cost=row.unit_cost,
                occurred_at=row.occurred_at,
            )
            for row in result
        ]


def select_adjustments_for_accounting(user_id: int) -> Select[*tuple[Any, ...]]:
    """The statement `list_adjustments_for_accounting` runs, with every column it loads named.

    The five columns an `Adjustment` is built from, and nothing else: not the note, and not the
    two instants that are our clock rather than the owner's history. Ordered by `id`, an
    `INTEGER`; the engine orders the events by its own key, in Python.
    """
    return (
        select(
            ManualAdjustment.id,
            ManualAdjustment.asset,
            ManualAdjustment.quantity,
            ManualAdjustment.unit_cost,
            ManualAdjustment.occurred_at,
        )
        .where(ManualAdjustment.user_id == user_id)
        .order_by(ManualAdjustment.id)
    )
