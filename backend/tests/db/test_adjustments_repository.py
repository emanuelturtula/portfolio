"""Criterion 1 of #18: `ManualAdjustmentRepository` reads and writes the table, and only that.

Over a real SQLite file migrated to head, through the application's own engine. What the
repository wrote is read back over a **second** session, so an assertion is about what was
committed and not about a row pending in an identity map.

## What is pinned

* **Every lookup is scoped by owner**: another owner's id is `None`, exactly as a missing id is.
* **The repository never commits**: the service owns the unit of work (spec 023), so a flush
  without a commit is invisible to another connection and a rollback discards it.
* **Amounts round-trip exactly**, at eighteen places, and `NULL` stays `NULL`: an unknown
  cost is never read back as zero.
* **The accounting read never loads the note**, and orders by nothing but the integer id. The
  note is free text the owner wrote; a column never loaded cannot reach a snapshot or a log.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import sqlite

from portfolio.repositories.adjustments import (
    AdjustmentRecord,
    ManualAdjustmentRepository,
    select_adjustments_for_accounting,
)
from tests.accounting_harness import plant_owner, rows
from tests.adjustments_harness import ADJUSTMENTS_SQL
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CREATED: Final = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
ACQUIRED: Final = datetime(2025, 6, 1, 12, 30, tzinfo=UTC)

#: The smallest and the largest amounts the column holds: 1E-18 and twenty integer digits.
DUST: Final = Decimal("0.000000000000000001")
WHALE: Final = Decimal("99999999999999999999.999999999999999999")


@pytest.fixture
async def factory(tmp_path: Any) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


async def owners(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with factory() as session:
        return await plant_owner(session, "owner"), await plant_owner(session, "stranger")


async def add(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    *,
    asset: str = "BTC",
    quantity: Decimal = Decimal("0.5"),
    unit_cost: Decimal | None = Decimal(30000),
    occurred_at: datetime = ACQUIRED,
    note: str = "Opening balance",
) -> int:
    """One adjustment through the repository, committed by the test as the service would."""
    async with factory() as session:
        added = await ManualAdjustmentRepository(session).add(
            user_id=user_id,
            asset=asset,
            quantity=quantity,
            unit_cost=unit_cost,
            occurred_at=occurred_at,
            note=note,
            created_at=CREATED,
        )
        identifier = added.id
        await session.commit()
    return identifier


# --------------------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------------------


async def test_add_stores_every_column_as_the_types_write_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, _stranger = await owners(factory)

    identifier = await add(factory, owner, note="  Kept as typed  ")

    (row,) = await rows(factory, ADJUSTMENTS_SQL)
    assert row == {
        "id": identifier,
        "user_id": owner,
        "asset": "BTC",
        "quantity": "0.500000000000000000",
        "unit_cost": "30000.000000000000000000",
        "occurred_at": "2025-06-01 12:30:00.000000",
        "note": "  Kept as typed  ",
        "created_at": "2026-09-29 09:00:00.000000",
        "updated_at": "2026-09-29 09:00:00.000000",
    }


async def test_add_assigns_the_id_at_the_flush_and_does_not_commit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The service logs and returns the id before it commits, so the flush must assign it."""
    owner, _stranger = await owners(factory)

    async with factory() as session:
        added = await ManualAdjustmentRepository(session).add(
            user_id=owner,
            asset="BTC",
            quantity=Decimal(1),
            unit_cost=None,
            occurred_at=ACQUIRED,
            note="Pending",
            created_at=CREATED,
        )
        assert added.id is not None
        assert await rows(factory, ADJUSTMENTS_SQL) == [], "nothing committed yet"
        await session.rollback()

    assert await rows(factory, ADJUSTMENTS_SQL) == []


async def test_an_unknown_cost_stays_null_and_zero_stays_zero(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`None` and `Decimal(0)` are different entries, and the table keeps them apart."""
    owner, _stranger = await owners(factory)
    unknown = await add(factory, owner, unit_cost=None)
    free = await add(factory, owner, unit_cost=Decimal(0))

    async with factory() as session:
        repository = ManualAdjustmentRepository(session)
        read_unknown = await repository.get(owner, unknown)
        read_free = await repository.get(owner, free)

    assert read_unknown is not None
    assert read_free is not None
    assert read_unknown.unit_cost is None
    assert read_free.unit_cost == 0
    assert read_free.unit_cost is not None


@pytest.mark.parametrize("value", [DUST, WHALE, Decimal("1.50000000000000000000")])
async def test_amounts_at_the_edges_round_trip_exactly(
    factory: async_sessionmaker[AsyncSession], value: Decimal
) -> None:
    owner, _stranger = await owners(factory)
    identifier = await add(factory, owner, quantity=value, unit_cost=value)

    async with factory() as session:
        (record,) = await ManualAdjustmentRepository(session).list_adjustments_for_accounting(owner)

    assert record.id == identifier
    assert record.quantity == value
    assert record.unit_cost == value


# --------------------------------------------------------------------------------------
# get, list, update, delete: scoped by owner
# --------------------------------------------------------------------------------------


async def test_get_is_scoped_by_owner(factory: async_sessionmaker[AsyncSession]) -> None:
    """Another owner's id and a missing id are the same answer: `None`."""
    owner, stranger = await owners(factory)
    theirs = await add(factory, stranger)
    mine = await add(factory, owner)

    async with factory() as session:
        repository = ManualAdjustmentRepository(session)
        found = await repository.get(owner, mine)
        other = await repository.get(owner, theirs)
        missing = await repository.get(owner, mine + 1000)

    assert found is not None
    assert found.id == mine
    assert (other, missing) == (None, None)


async def test_list_for_user_is_the_owners_rows_by_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """By id, whatever the acquisition dates: ordering by date is the service's, in Python."""
    owner, stranger = await owners(factory)
    first = await add(factory, owner, occurred_at=ACQUIRED + timedelta(days=3))
    await add(factory, stranger)
    second = await add(factory, owner, occurred_at=ACQUIRED)

    async with factory() as session:
        listed = await ManualAdjustmentRepository(session).list_for_user(owner)

    assert [row.id for row in listed] == [first, second]


async def test_update_replaces_the_five_fields_and_stamps_updated_at(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, _stranger = await owners(factory)
    identifier = await add(factory, owner)
    later = CREATED + timedelta(hours=2)

    async with factory() as session:
        repository = ManualAdjustmentRepository(session)
        found = await repository.get(owner, identifier)
        assert found is not None
        await repository.update(
            found,
            asset="KAS",
            quantity=Decimal(1000),
            unit_cost=None,
            occurred_at=ACQUIRED - timedelta(days=30),
            note="Corrected",
            updated_at=later,
        )
        await session.commit()

    (row,) = await rows(factory, ADJUSTMENTS_SQL)
    assert row == {
        "id": identifier,
        "user_id": owner,
        "asset": "KAS",
        "quantity": "1000.000000000000000000",
        "unit_cost": None,
        "occurred_at": "2025-05-02 12:30:00.000000",
        "note": "Corrected",
        "created_at": "2026-09-29 09:00:00.000000",
        "updated_at": "2026-09-29 11:00:00.000000",
    }


async def test_delete_removes_only_that_row_and_waits_for_the_commit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, _stranger = await owners(factory)
    doomed = await add(factory, owner)
    kept = await add(factory, owner)

    async with factory() as session:
        repository = ManualAdjustmentRepository(session)
        found = await repository.get(owner, doomed)
        assert found is not None
        await repository.delete(found)
        still_there = [row["id"] for row in await rows(factory, ADJUSTMENTS_SQL)]
        await session.commit()

    assert still_there == [doomed, kept], "the repository did not commit"
    assert [row["id"] for row in await rows(factory, ADJUSTMENTS_SQL)] == [kept]


# --------------------------------------------------------------------------------------
# The accounting read
# --------------------------------------------------------------------------------------


async def test_the_accounting_read_is_plain_records_of_the_owner_only(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, stranger = await owners(factory)
    first = await add(factory, owner, unit_cost=None)
    await add(factory, stranger, asset="KAS")
    second = await add(factory, owner, asset="KAS", quantity=Decimal(7), unit_cost=Decimal(0))

    async with factory() as session:
        records = await ManualAdjustmentRepository(session).list_adjustments_for_accounting(owner)

    assert records == [
        AdjustmentRecord(
            id=first,
            asset="BTC",
            quantity=Decimal("0.5"),
            unit_cost=None,
            occurred_at=ACQUIRED,
        ),
        AdjustmentRecord(
            id=second,
            asset="KAS",
            quantity=Decimal(7),
            unit_cost=Decimal(0),
            occurred_at=ACQUIRED,
        ),
    ]
    assert all(record.occurred_at.utcoffset() == timedelta(0) for record in records)
    assert not hasattr(records[0], "note")


def test_the_accounting_select_names_its_columns_and_never_the_note() -> None:
    compiled = str(
        select_adjustments_for_accounting(1).compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "note" not in compiled
    assert "*" not in compiled
    for column in ("id", "asset", "quantity", "unit_cost", "occurred_at"):
        assert f"manual_adjustments.{column}" in compiled
    # Nothing ordered in SQL but the integer id, and nothing summed (rule 2).
    assert compiled.split("ORDER BY", 1)[1].strip() == "manual_adjustments.id"
    assert "SUM(" not in compiled.upper()


async def test_no_statement_the_accounting_read_runs_touches_the_note(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every statement sent to SQLite, not only the one compiled above."""
    owner, _stranger = await owners(factory)
    await add(factory, owner, note="note-sentinel-" + "Kp3" * 5)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement)

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            records = await ManualAdjustmentRepository(session).list_adjustments_for_accounting(
                owner
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert len(records) == 1
    assert any("manual_adjustments" in statement for statement in statements), statements
    assert [statement for statement in statements if "note" in statement] == []


async def test_the_accounting_read_of_an_owner_with_nothing_is_empty(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    owner, _stranger = await owners(factory)

    async with factory() as session:
        repository = ManualAdjustmentRepository(session)
        assert await repository.list_adjustments_for_accounting(owner) == []
        assert await repository.list_for_user(owner) == []
