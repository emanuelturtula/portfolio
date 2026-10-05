"""Reads and writes of the `derived_addresses` table (spec 031).

Queries and nothing else, like every repository here: no derivation, no clock, no policy
about when an address counts as used. It is handed an `AsyncSession` and never commits --
the balance sync writes these rows in the same per-chain commit as the wallet's snapshot
(R6), and that commit is the sync's to make.

**Rows are returned as frozen records, never as ORM objects.** The sync loads them before
its `gather` and hands them to a provider inside it, and an ORM instance read in one place
and touched after a rollback is an implicit lazy load, which an async session refuses with
an error rather than a query. A record cannot do that.

Nothing here logs. Every row is an address, which is the owner's holdings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from portfolio.db.models import DerivedAddress

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["DerivedAddressRecord", "DerivedAddressRepository"]


@dataclass(frozen=True, slots=True)
class DerivedAddressRecord:
    """One derived address: where it sits in the tree, what it is, and whether it was used."""

    branch: int
    child_index: int
    address: str
    used: bool


class DerivedAddressRepository:
    """Every query this application makes against `derived_addresses`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_wallets(
        self, wallet_ids: Collection[int]
    ) -> dict[int, tuple[DerivedAddressRecord, ...]]:
        """Every derived address of each wallet, by branch, then by index.

        Every requested id is a key, with an empty tuple for a wallet nothing has been
        derived for yet, so a caller never has to tell "no rows" from "not asked about".

        Ordered by two `INTEGER` columns, which is safe: nothing here is money, and the
        ordering rule this codebase keeps out of SQL is about `TEXT` money columns.
        """
        found: dict[int, list[DerivedAddressRecord]] = {wallet_id: [] for wallet_id in wallet_ids}
        if not found:
            return {}
        rows = await self._session.scalars(
            select(DerivedAddress)
            .where(DerivedAddress.wallet_id.in_(found))
            .order_by(DerivedAddress.wallet_id, DerivedAddress.branch, DerivedAddress.child_index)
        )
        for row in rows:
            found[row.wallet_id].append(
                DerivedAddressRecord(
                    branch=row.branch,
                    child_index=row.child_index,
                    address=row.address_canonical,
                    used=row.used,
                )
            )
        return {wallet_id: tuple(records) for wallet_id, records in found.items()}

    async def apply(
        self,
        wallet_id: int,
        *,
        new: Sequence[DerivedAddressRecord],
        newly_used: Collection[tuple[int, int]],
        created_at: datetime,
    ) -> None:
        """Insert the addresses a scan derived, and mark the ones it found newly used.

        `newly_used` names positions as `(branch, child_index)` pairs. **Only ever
        `used = true`**: there is no way to ask this method to set it back, because an
        address once seen used stays used (R5) and the gap limit is counted after it for
        good.

        Flushes and does not commit. A refused insert -- a position stored twice, a branch
        outside the `CHECK` -- raises from the flush, and the sync's per-chain handler rolls
        the whole chain back with it.
        """
        self._session.add_all(
            DerivedAddress(
                wallet_id=wallet_id,
                branch=record.branch,
                child_index=record.child_index,
                address_canonical=record.address,
                used=record.used,
                created_at=created_at,
            )
            for record in new
        )
        for branch, child_index in newly_used:
            await self._session.execute(
                update(DerivedAddress)
                .where(
                    DerivedAddress.wallet_id == wallet_id,
                    DerivedAddress.branch == branch,
                    DerivedAddress.child_index == child_index,
                )
                .values(used=True)
            )
        await self._session.flush()
