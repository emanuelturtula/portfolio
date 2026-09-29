"""Manual adjustments planted the ways #18's tests need, and the table read back.

Five suites need the same pieces -- the migration and repository tests, the service tests,
the recompute tests, the endpoint tests and the log-capture test -- so they live here, the
arrangement `tests/accounting_harness.py` has for #19.

## Two ways an adjustment reaches the table

* **Through the application** -- the service, or `POST /api/accounting/adjustments` -- which
  is how every real row gets there, so it is the default wherever the test is about what
  the owner sees.
* **By raw SQL** (`plant_adjustment`): a row with an id the test chooses, for the ordering
  tests that need ids 9 and 10 side by side, and a row the service refuses since #18, as a
  hand-edited database would hold, for `UnconvertibleAdjustmentError`. The `CHECK` on the
  note is the only thing SQL cannot get past, which is what the migration test asserts.

## Nothing here is sensitive

Assets are the venues' public symbols, amounts are round synthetic figures, and the notes are
obviously invented text. No address, key or hostname appears.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from tests.accounting_harness import INGESTED_AT, fixed
from tests.balance_harness import sqlite_timestamp

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ADJUSTMENTS_PATH: Final = "/api/accounting/adjustments"
POSITIONS_PATH: Final = "/api/accounting/positions"

ADJUSTMENTS_SQL: Final = (
    "SELECT id, user_id, asset, quantity, unit_cost, occurred_at, note, created_at, "
    "updated_at FROM manual_adjustments ORDER BY id"
)

#: The fields an adjustment carries on the wire, as spec 023's *Endpoints* lists them.
ADJUSTMENT_FIELDS: Final = frozenset(
    {
        "id",
        "asset",
        "quantity",
        "unit_cost",
        "occurred_at",
        "note",
        "created_at",
        "updated_at",
    }
)

#: The five fields a `PUT` replaces, and a `POST` sends.
EDITABLE_FIELDS: Final = ("asset", "quantity", "unit_cost", "occurred_at", "note")


async def plant_adjustment(
    session: AsyncSession,
    user_id: int,
    *,
    asset: str = "BTC",
    quantity: str = "1",
    unit_cost: str | None = "20000",
    occurred_at: datetime,
    note: str = "Opening balance",
    adjustment_id: int | None = None,
    raw_quantity: str | None = None,
) -> int:
    """One `manual_adjustments` row, written by SQL. Committed. Returns its id.

    `raw_quantity` writes the column's text exactly as given, for a row no service would
    accept (a zero quantity, which the table has no `CHECK` against, spec 023). Otherwise the
    amounts are written the way `NumericText(18)` writes them.
    """
    stamp = sqlite_timestamp(INGESTED_AT)
    result = await session.execute(
        text(
            "INSERT INTO manual_adjustments (id, user_id, asset, quantity, unit_cost, "
            "occurred_at, note, created_at, updated_at) VALUES (:id, :user, :asset, "
            ":quantity, :unit_cost, :occurred_at, :note, :stamp, :stamp) RETURNING id"
        ),
        {
            "id": adjustment_id,
            "user": user_id,
            "asset": asset,
            "quantity": raw_quantity if raw_quantity is not None else fixed(Decimal(quantity)),
            "unit_cost": None if unit_cost is None else fixed(Decimal(unit_cost)),
            "occurred_at": sqlite_timestamp(occurred_at),
            "note": note,
            "stamp": stamp,
        },
    )
    created: int = result.scalar_one()
    await session.commit()
    return created


def body(
    *,
    asset: str = "BTC",
    quantity: Any = "1",
    unit_cost: Any = "20000",
    occurred_at: Any = "2026-01-01T00:00:00Z",
    note: Any = "Opening balance before the imported history",
    omit: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A request body for `POST` or `PUT`, every field present unless it is in `omit`."""
    built: dict[str, Any] = {
        "asset": asset,
        "quantity": quantity,
        "unit_cost": unit_cost,
        "occurred_at": occurred_at,
        "note": note,
    }
    for name in omit:
        del built[name]
    return built


def iso(moment: datetime) -> str:
    """An aware instant as the ISO 8601 text a client sends."""
    return moment.astimezone(UTC).isoformat()
