"""The fills #19's recompute reads, planted the ways a test needs, and its tables read back.

Four suites need the same pieces -- the service tests, the performance test, the trigger
tests and the endpoint tests -- so they live here, the arrangement
`tests/exchange_sync_harness.py` has for #15.

## Three ways a fill reaches the table

* **Through the repository** (`plant_fills`): `ExchangeFillRepository.insert_page`, the one
  write path the application has, with `NormalizedFill`s built by `make_fill`. That is how
  every stored row got there, so it is the default.
* **As a structural record** (`FillRow`): the same write path with a plain dataclass instead
  of a `NormalizedFill`, for the performance test, whose 5,000 rows and golden trades need
  no venue validation to be meaningful and would only pay for it.
* **By raw SQL** (`plant_unconvertible_fill`): a row `NormalizedFill` refuses since #99, as a
  row stored before that check would be. Spec 020's *For #19*: such a row must fail the
  recompute loudly, and nothing but SQL can still write one.

## Nothing here is a real credential, address or hostname

Trade ids are small integers or obviously synthetic text, and no address appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.repositories.exchanges import ExchangeFillRepository
from tests.balance_harness import insert_user, sqlite_timestamp

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.repositories.exchanges import FillRecord

#: When the fixture fills were ingested. Our clock, and no assertion reads it.
INGESTED_AT: Final = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

#: The first fill of a planted history, a whole second so every bound built from it is exact.
HISTORY_START: Final = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

HEADER_SQL: Final = (
    "SELECT id, user_id, method, engine_version, input_fingerprint, event_count, "
    "unallocated_costs, computed_at FROM accounting_snapshots ORDER BY id"
)
POSITIONS_SQL: Final = (
    "SELECT id, snapshot_id, asset, quantity, unknown_basis_quantity, cost_basis, "
    "average_cost, realized_pnl, unmatched_proceeds, flags FROM accounting_positions "
    "ORDER BY asset"
)
LOTS_SQL: Final = (
    "SELECT id, snapshot_id, seq, asset, occurred_at, source, external_id, kind, quantity, "
    "cost_basis, unknown_basis_quantity FROM accounting_lots ORDER BY seq"
)
WARNINGS_SQL: Final = (
    "SELECT id, snapshot_id, seq, kind, occurred_at, source, asset, quantity, charged_to "
    "FROM accounting_warnings ORDER BY seq"
)
ACCOUNTING_TABLES: Final = (
    "accounting_snapshots",
    "accounting_positions",
    "accounting_lots",
    "accounting_warnings",
)


@dataclass(frozen=True, slots=True)
class FillRow:
    """A structural `FillRecord`, unvalidated: what `insert_page` stores, and nothing more."""

    external_trade_id: str
    base_asset: str
    quote_asset: str
    side: FillSide
    quantity: Decimal
    quote_quantity: Decimal
    fee_amount: Decimal
    fee_asset: str | None
    executed_at: datetime
    price: Decimal = Decimal(1)
    external_order_id: str | None = None
    quote_quantity_derived: bool = False
    raw_payload: str = "{}"

    @property
    def symbol(self) -> str:
        return f"{self.base_asset}{self.quote_asset}"


async def plant_owner(session: AsyncSession, username: str = "owner") -> int:
    """A `users` row. Committed."""
    return await insert_user(session, username)


async def plant_account(
    session: AsyncSession,
    user_id: int,
    exchange_key: ExchangeKey = ExchangeKey.BITGET,
    *,
    account_id: int | None = None,
) -> int:
    """An `exchange_accounts` row for `user_id` at `exchange_key`. Committed.

    `account_id` chooses the id, for a test that searches a message for it: a six-digit id
    is a search that means something, where `1` would match any digit anywhere.
    """
    result = await session.execute(
        text(
            "INSERT INTO exchange_accounts (id, user_id, exchange_key, created_at) "
            "VALUES (:id, :user_id, :key, :at) RETURNING id"
        ),
        {
            "id": account_id,
            "user_id": user_id,
            "key": str(exchange_key),
            "at": sqlite_timestamp(INGESTED_AT),
        },
    )
    created: int = result.scalar_one()
    await session.commit()
    return created


async def plant_fills(session: AsyncSession, account_id: int, fills: Sequence[FillRecord]) -> int:
    """Store `fills` through the application's own insert, and commit. Returns how many were new."""
    outcome = await ExchangeFillRepository(session).insert_page(
        account_id, fills, ingested_at=INGESTED_AT
    )
    await session.commit()
    return outcome.inserted


async def plant_unconvertible_fill(
    session: AsyncSession,
    account_id: int,
    *,
    trade_id: str,
    shape: str,
    executed_at: datetime = HISTORY_START,
) -> None:
    """One row of a shape `Trade` refuses, written by SQL as a pre-#99 row would have been.

    `shape` is a `TradeShapeProblem` value: `same_asset`, `fee_consumes_received` or
    `rebate_exceeds_given`. Every other column is legal, so the row passes every `CHECK`.
    """
    base, quote, side, quantity, quote_quantity, fee, fee_asset = {
        # BTC for BTC: nothing received that is not also given.
        "same_asset": ("BTC", "BTC", "buy", "1", "1", "0", None),
        # A buy of 0.5 BTC whose fee in BTC is all of it.
        "fee_consumes_received": ("BTC", "USDT", "buy", "0.5", "30000", "0.5", "BTC"),
        # A buy paying 100 USDT with a 100 USDT rebate: nothing given.
        "rebate_exceeds_given": ("BTC", "USDT", "buy", "0.001", "100", "-100", "USDT"),
    }[shape]
    await session.execute(
        text(
            "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, "
            "external_order_id, symbol, base_asset, quote_asset, side, quantity, price, "
            "quote_quantity, quote_quantity_derived, fee_amount, fee_asset, executed_at, "
            "raw_payload, ingested_at) VALUES (:account, :trade, NULL, :symbol, :base, "
            ":quote, :side, :quantity, :price, :quote_quantity, 0, :fee, :fee_asset, :at, "
            "'{}', :ingested)"
        ),
        {
            "account": account_id,
            "trade": trade_id,
            "symbol": f"{base}{quote}",
            "base": base,
            "quote": quote,
            "side": side,
            "quantity": fixed(Decimal(quantity)),
            "price": fixed(Decimal(1)),
            "quote_quantity": fixed(Decimal(quote_quantity)),
            "fee": fixed(Decimal(fee)),
            "fee_asset": fee_asset,
            "at": sqlite_timestamp(executed_at),
            "ingested": sqlite_timestamp(INGESTED_AT),
        },
    )
    await session.commit()


async def plant_price(
    session: AsyncSession,
    *,
    symbol: str,
    amount: Decimal,
    as_of: datetime,
    currency: str = "USD",
    source: str = "coinbase",
) -> None:
    """A `prices` row in the fixed-point text `NumericText(12)` writes. Committed."""
    await session.execute(
        text(
            "INSERT INTO prices (asset_id, quote_currency, amount, source, as_of, fetched_at) "
            "VALUES ((SELECT id FROM assets WHERE symbol = :symbol), "
            ":currency, :amount, :source, :as_of, :as_of)"
        ),
        {
            "symbol": symbol,
            "currency": currency,
            "amount": f"{amount:.12f}",
            "source": source,
            "as_of": sqlite_timestamp(as_of),
        },
    )
    await session.commit()


def fixed(value: Decimal) -> str:
    """An amount as `NumericText(18)` writes it: fixed notation, exactly eighteen places."""
    return f"{value:.18f}"


def at(minutes: int) -> datetime:
    """`HISTORY_START` plus `minutes`: an ordered, collision-free instant per fill."""
    return HISTORY_START + timedelta(minutes=minutes)


async def rows(factory: async_sessionmaker[AsyncSession], sql: str) -> list[dict[str, Any]]:
    """A statement over a session of its own, as plain dictionaries: what was **committed**."""
    async with factory() as session:
        result = await session.execute(text(sql))
        return [dict(row) for row in result.mappings().all()]


async def snapshot_tables(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Every row of the four accounting tables, keyed by table."""
    return {
        "header": await rows(factory, HEADER_SQL),
        "positions": await rows(factory, POSITIONS_SQL),
        "lots": await rows(factory, LOTS_SQL),
        "warnings": await rows(factory, WARNINGS_SQL),
    }


async def dump_accounting_tables(factory: async_sessionmaker[AsyncSession]) -> str:
    """Every column of every accounting row, as one string: what a sentinel search reads."""
    dumped: list[str] = []
    for table in ACCOUNTING_TABLES:
        # The table name is one of four literals above, never input.
        dumped.extend(repr(row) for row in await rows(factory, f"SELECT * FROM {table}"))  # noqa: S608
    return "\n".join(dumped)
