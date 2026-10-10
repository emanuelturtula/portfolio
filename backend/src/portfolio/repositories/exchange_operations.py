"""Reads and writes of `exchange_operations` and `exchange_imports` (spec 042).

Queries and nothing else: no clock, no policy beyond the one rule the table keeps -- a stored
`(source, external_id)` is never overwritten (R3) -- and no commit.

## Nothing here aggregates, orders or compares money

Rows are ordered by `executed_at`, an instant, and `id`. The amounts are `TEXT`, and every
sum of them is taken in Python by `domain.investment`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, select

from portfolio.db.models import ExchangeImport, ExchangeOperation

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["ExchangeOperationRepository", "NewOperation", "OperationFilter"]

_ID_BATCH: Final = 500
"""Ids per `IN (...)` when asking which are stored, well under SQLite's variable limit."""


@dataclass(frozen=True, slots=True)
class NewOperation:
    """An operation to store: every column but the owner, the import and the timestamps."""

    source: str
    venue: str
    external_id: str
    executed_at: datetime
    kind: str
    asset: str
    quantity: Decimal
    quote_currency: str | None
    quote_amount: Decimal | None
    fee_asset: str | None
    fee_amount: Decimal | None
    description: str


@dataclass(frozen=True, slots=True)
class OperationFilter:
    """Which operations a listing keeps. Each field narrows it; `None` keeps every value.

    `since` is inclusive and `until` exclusive, so consecutive days never share an instant.
    """

    asset: str | None = None
    venue: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    def apply(self, statement: Select[Any]) -> Select[Any]:
        """`statement` with this filter's conditions added."""
        if self.asset is not None:
            statement = statement.where(ExchangeOperation.asset == self.asset)
        if self.venue is not None:
            statement = statement.where(ExchangeOperation.venue == self.venue)
        if self.since is not None:
            statement = statement.where(ExchangeOperation.executed_at >= self.since)
        if self.until is not None:
            statement = statement.where(ExchangeOperation.executed_at < self.until)
        return statement


class ExchangeOperationRepository:
    """Every query this application makes against the two tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stored_ids(self, user_id: int, source: str, external_ids: Iterable[str]) -> set[str]:
        """Which of `external_ids` the owner already has from `source`."""
        wanted = sorted(set(external_ids))
        found: set[str] = set()
        for start in range(0, len(wanted), _ID_BATCH):
            batch = wanted[start : start + _ID_BATCH]
            found.update(
                await self._session.scalars(
                    select(ExchangeOperation.external_id).where(
                        ExchangeOperation.user_id == user_id,
                        ExchangeOperation.source == source,
                        ExchangeOperation.external_id.in_(batch),
                    )
                )
            )
        return found

    async def add_import(
        self,
        *,
        user_id: int,
        filename: str,
        sha256: str,
        stored: int,
        already_stored: int,
        imported_at: datetime,
    ) -> int:
        """Record one upload and return its id. Flushes and does not commit."""
        row = ExchangeImport(
            user_id=user_id,
            filename=filename,
            sha256=sha256,
            stored=stored,
            already_stored=already_stored,
            imported_at=imported_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def add(
        self,
        *,
        user_id: int,
        operations: Sequence[NewOperation],
        import_id: int | None,
        created_at: datetime,
    ) -> list[ExchangeOperation]:
        """Insert every operation and return the rows. Flushes and does not commit.

        The caller has already left out the stored ids; one that slips through anyway fails on
        the unique constraint rather than overwriting the row.
        """
        rows = [
            ExchangeOperation(
                user_id=user_id,
                source=operation.source,
                venue=operation.venue,
                external_id=operation.external_id,
                executed_at=operation.executed_at,
                kind=operation.kind,
                asset=operation.asset,
                quantity=operation.quantity,
                quote_currency=operation.quote_currency,
                quote_amount=operation.quote_amount,
                fee_asset=operation.fee_asset,
                fee_amount=operation.fee_amount,
                description=operation.description,
                import_id=import_id,
                created_at=created_at,
            )
            for operation in operations
        ]
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    async def count(self, user_id: int, filters: OperationFilter | None = None) -> int:
        """How many of the owner's operations `filters` keeps; all of them without one."""
        statement = (
            select(func.count())
            .select_from(ExchangeOperation)
            .where(ExchangeOperation.user_id == user_id)
        )
        total = await self._session.scalar((filters or OperationFilter()).apply(statement))
        return int(total or 0)

    async def page(
        self,
        user_id: int,
        *,
        limit: int,
        offset: int,
        filters: OperationFilter | None = None,
    ) -> list[ExchangeOperation]:
        """One page of the owner's operations that `filters` keeps, newest first; ties by
        id, newest first."""
        statement = select(ExchangeOperation).where(ExchangeOperation.user_id == user_id)
        rows = await self._session.scalars(
            (filters or OperationFilter())
            .apply(statement)
            .order_by(ExchangeOperation.executed_at.desc(), ExchangeOperation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows)

    async def assets(self, user_id: int) -> list[str]:
        """Every asset the owner has an operation in, alphabetically: a text column."""
        rows = await self._session.scalars(
            select(ExchangeOperation.asset)
            .where(ExchangeOperation.user_id == user_id)
            .distinct()
            .order_by(ExchangeOperation.asset)
        )
        return list(rows)

    async def venues(self, user_id: int) -> list[str]:
        """Every venue the owner has an operation from, alphabetically."""
        rows = await self._session.scalars(
            select(ExchangeOperation.venue)
            .where(ExchangeOperation.user_id == user_id)
            .distinct()
            .order_by(ExchangeOperation.venue)
        )
        return list(rows)

    async def all_for(self, user_id: int) -> list[ExchangeOperation]:
        """Every operation the owner has stored, oldest first."""
        rows = await self._session.scalars(
            select(ExchangeOperation)
            .where(ExchangeOperation.user_id == user_id)
            .order_by(ExchangeOperation.executed_at, ExchangeOperation.id)
        )
        return list(rows)

    async def get(self, user_id: int, operation_id: int) -> ExchangeOperation | None:
        """One of the owner's operations, or `None` -- for another owner's too."""
        return await self._session.scalar(
            select(ExchangeOperation).where(
                ExchangeOperation.id == operation_id, ExchangeOperation.user_id == user_id
            )
        )

    async def delete(self, operation: ExchangeOperation) -> None:
        """Remove one row. Flushes and does not commit."""
        await self._session.delete(operation)
        await self._session.flush()
