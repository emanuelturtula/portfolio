"""Reads and writes of the cost-basis snapshot: its header, positions, lots and warnings (#19).

Queries and nothing else. Like every repository here it is handed an `AsyncSession` and **never
commits**: `AccountingService.recompute` owns the one transaction a snapshot is replaced in,
and the decision to write at all.

## A snapshot is replaced whole, and the cascade does the deleting

`replace` deletes the owner's header for the method and inserts the new one with its children.
The positions, lots and warnings of the old header go with it through `ON DELETE CASCADE`,
which the runtime engine's `PRAGMA foreign_keys=ON` makes real. There is no update path: a
snapshot is a function of the fills, and a partial update is a snapshot that is neither the
old answer nor the new one.

**SQLite reuses the row ids.** Without `AUTOINCREMENT` a new row takes the largest id in the
table plus one, and the header just deleted was the largest, so a replaced snapshot usually
comes back under the same ids. An id says nothing about whether a snapshot was rewritten;
`computed_at` and `input_fingerprint` do.

## Nothing is compared, ordered or aggregated in SQL on a money column

The rows are read whole and ordered by `asset` or by `seq` -- text and an integer. Every amount
is `NumericText`, and `SUM()`, `ORDER BY` or `<` on one would coerce it to a float in SQLite.
The totals are `domain.accounting.value_portfolio`'s, in Python.

## Warnings are stored without the trade id

`NegativeInventory` and `UnattributedFee` are flattened into one row shape, and the event's
`external_id` is left behind: the venue and the moment identify the fill for the owner, and a
warning is the row most likely to be quoted somewhere a trade id should not be. A lot keeps
it, because a lot is an acquisition that a later method has to be able to match.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import delete, insert, select

from portfolio.db.models import (
    AccountingLot,
    AccountingPosition,
    AccountingSnapshot,
    AccountingWarning,
)
from portfolio.domain.accounting import NegativeInventory, Position, PositionFlag

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.domain.accounting import AccountingResult, EventKey, Lot
    from portfolio.domain.accounting.results import AccountingWarning as DomainWarning

__all__ = [
    "AccountingSnapshotRepository",
    "AccountingWarningKind",
    "SnapshotHeader",
    "SnapshotWarning",
]

_FLAG_SEPARATOR = ","


class AccountingWarningKind(StrEnum):
    """Which of the engine's two warnings a stored row is. The member is its column value.

    Defined here rather than in `domain`, because it is a storage vocabulary: the engine
    distinguishes the two by type, and a column needs a word. `db.models` spells the same two
    words in `_ACCOUNTING_WARNING_KIND_CHECK`, and a test holds them together.
    """

    NEGATIVE_INVENTORY = "negative_inventory"
    UNATTRIBUTED_FEE = "unattributed_fee"


@dataclass(frozen=True, slots=True)
class SnapshotHeader:
    """One `accounting_snapshots` row, copied out of the session.

    A frozen copy rather than the ORM row, for the reason `ExchangeAccountState` is one: a
    rollback expires every persistent object on the session, and an expired attribute under an
    async session is a `MissingGreenlet` rather than a query.
    """

    id: int
    user_id: int
    method: str
    engine_version: int
    input_fingerprint: str
    event_count: int
    unallocated_costs: Decimal
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class SnapshotWarning:
    """One stored warning, in the shape the endpoint serves it. No `external_id`, by design.

    * `asset` -- the asset that fell short, or the asset a fee was paid in;
    * `quantity` -- the shortfall, or the part of the fee that could not be valued;
    * `charged_to` -- the position whose figures leave an unvalued fee out, and `None` for a
      shortfall and for a fee on a conversion between two cash assets.
    """

    seq: int
    kind: AccountingWarningKind
    occurred_at: datetime
    source: str
    asset: str
    quantity: Decimal
    charged_to: str | None


def _header_of(row: AccountingSnapshot) -> SnapshotHeader:
    """Copy a header row into its snapshot."""
    return SnapshotHeader(
        id=row.id,
        user_id=row.user_id,
        method=row.method,
        engine_version=row.engine_version,
        input_fingerprint=row.input_fingerprint,
        event_count=row.event_count,
        unallocated_costs=row.unallocated_costs,
        computed_at=row.computed_at,
    )


def _flags_text(flags: frozenset[PositionFlag]) -> str:
    """The flags as the column holds them: sorted values, comma-joined, empty for none."""
    return _FLAG_SEPARATOR.join(sorted(flag.value for flag in flags))


def _flags_of(text: str) -> frozenset[PositionFlag]:
    """The column's text back as flags. A value this module did not write raises."""
    return frozenset(PositionFlag(value) for value in text.split(_FLAG_SEPARATOR) if value)


def _position_values(snapshot_id: int, position: Position) -> dict[str, object]:
    """One position as the column values of its row."""
    return {
        "snapshot_id": snapshot_id,
        "asset": position.asset,
        "quantity": position.quantity,
        "unknown_basis_quantity": position.unknown_basis_quantity,
        "cost_basis": position.cost_basis,
        "average_cost": position.average_cost,
        "realized_pnl": position.realized_pnl,
        "unmatched_proceeds": position.unmatched_proceeds,
        "flags": _flags_text(position.flags),
    }


def _lot_values(snapshot_id: int, seq: int, lot: Lot, kind: str) -> dict[str, object]:
    """One lot as the column values of its row."""
    return {
        "snapshot_id": snapshot_id,
        "seq": seq,
        "asset": lot.asset,
        "occurred_at": lot.key.occurred_at,
        "source": lot.key.source,
        "external_id": lot.key.external_id,
        "kind": kind,
        "quantity": lot.quantity,
        "cost_basis": lot.cost_basis,
        "unknown_basis_quantity": lot.unknown_basis_quantity,
    }


def _warning_values(snapshot_id: int, seq: int, warning: DomainWarning) -> dict[str, object]:
    """One warning, flattened, as the column values of its row. The key's id is left out."""
    if isinstance(warning, NegativeInventory):
        kind = AccountingWarningKind.NEGATIVE_INVENTORY
        asset, quantity, charged_to = warning.asset, warning.shortfall, None
    else:
        kind = AccountingWarningKind.UNATTRIBUTED_FEE
        asset, quantity, charged_to = warning.fee_asset, warning.quantity, warning.charged_to
    return {
        "snapshot_id": snapshot_id,
        "seq": seq,
        "kind": kind.value,
        "occurred_at": warning.key.occurred_at,
        "source": warning.key.source,
        "asset": asset,
        "quantity": quantity,
        "charged_to": charged_to,
    }


class AccountingSnapshotRepository:
    """Every query this application makes against the four `accounting_*` tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_header(self, user_id: int, method: str) -> SnapshotHeader | None:
        """The owner's current snapshot header under `method`, read afresh, or `None`.

        `populate_existing`, so a header already in the identity map is refreshed from the
        database rather than handed back as it was the last time it was loaded.
        """
        row = await self._session.scalar(
            select(AccountingSnapshot)
            .where(AccountingSnapshot.user_id == user_id, AccountingSnapshot.method == method)
            .execution_options(populate_existing=True)
        )
        return None if row is None else _header_of(row)

    async def replace(
        self,
        user_id: int,
        result: AccountingResult,
        *,
        lot_kinds: Mapping[EventKey, str],
        computed_at: datetime,
    ) -> SnapshotHeader:
        """Delete the owner's snapshot under `result.method` and write `result` in its place.

        One header, then the positions, the lots and the warnings, each numbered by its place
        in the result from 0. The children are written with one `executemany` per table, which
        binds no more parameters per statement than one row has, whatever the history's size.

        Neither commits nor rolls back: the caller's transaction is what makes the delete and
        the inserts one change. A figure `NumericText(18)` refuses -- 10**20 or more, past the
        engine's range -- raises `ValueError` out of the insert (wrapped by SQLAlchemy in a
        `StatementError`), and the caller's rollback then restores the old snapshot whole.

        Args:
            user_id: the owner.
            result: what `replay` returned.
            lot_kinds: the kind of the event behind each lot's key, `trade` or `adjustment`.
                A lot whose key is missing raises `KeyError`, which the caller's rollback
                treats like any other failed write.
            computed_at: our clock, now.
        """
        await self._session.execute(
            delete(AccountingSnapshot).where(
                AccountingSnapshot.user_id == user_id,
                AccountingSnapshot.method == result.method,
            )
        )
        header = AccountingSnapshot(
            user_id=user_id,
            method=result.method,
            engine_version=result.engine_version,
            input_fingerprint=result.input_fingerprint,
            event_count=result.event_count,
            unallocated_costs=result.unallocated_costs,
            computed_at=computed_at,
        )
        self._session.add(header)
        await self._session.flush()
        snapshot_id = header.id
        if result.positions:
            await self._session.execute(
                insert(AccountingPosition),
                [_position_values(snapshot_id, position) for position in result.positions],
            )
        if result.lots:
            await self._session.execute(
                insert(AccountingLot),
                [
                    _lot_values(snapshot_id, seq, lot, lot_kinds[lot.key])
                    for seq, lot in enumerate(result.lots)
                ],
            )
        if result.warnings:
            await self._session.execute(
                insert(AccountingWarning),
                [
                    _warning_values(snapshot_id, seq, warning)
                    for seq, warning in enumerate(result.warnings)
                ],
            )
        return _header_of(header)

    async def list_positions(self, snapshot_id: int) -> tuple[Position, ...]:
        """A snapshot's positions, as the engine's own `Position`s, ordered by asset.

        `populate_existing`, because the ids are reused: a row loaded earlier in this session
        under the same id may belong to a snapshot that has since been replaced, and without it
        the identity map would hand back the old values (spec 021, R5).
        """
        rows = await self._session.scalars(
            select(AccountingPosition)
            .where(AccountingPosition.snapshot_id == snapshot_id)
            .order_by(AccountingPosition.asset)
            .execution_options(populate_existing=True)
        )
        return tuple(
            Position(
                asset=row.asset,
                quantity=row.quantity,
                unknown_basis_quantity=row.unknown_basis_quantity,
                cost_basis=row.cost_basis,
                average_cost=row.average_cost,
                realized_pnl=row.realized_pnl,
                unmatched_proceeds=row.unmatched_proceeds,
                flags=_flags_of(row.flags),
            )
            for row in rows
        )

    async def list_warnings(self, snapshot_id: int) -> tuple[SnapshotWarning, ...]:
        """A snapshot's warnings, in event order (`seq`). `populate_existing`, as for positions."""
        rows = await self._session.scalars(
            select(AccountingWarning)
            .where(AccountingWarning.snapshot_id == snapshot_id)
            .order_by(AccountingWarning.seq)
            .execution_options(populate_existing=True)
        )
        return tuple(
            SnapshotWarning(
                seq=row.seq,
                kind=AccountingWarningKind(row.kind),
                occurred_at=row.occurred_at,
                source=row.source,
                asset=row.asset,
                quantity=row.quantity,
                charged_to=row.charged_to,
            )
            for row in rows
        )
