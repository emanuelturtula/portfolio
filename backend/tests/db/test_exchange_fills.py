"""Criterion 7: the two exchange tables, against the schema the migrations actually build.

Every database here is a real file under `tmp_path`, migrated to head -- never
`metadata.create_all`, and never `:memory:`. The `UNIQUE`, the three `CHECK`s and the
`ON DELETE RESTRICT` are only worth asserting against the DDL the Raspberry Pi executes.

**Rows are written with raw SQL on purpose.** `NormalizedFill` refuses an empty trade id, a
wrong side and a non-boolean flag before any of them reaches a session; the constraints
here exist for the writer that bypasses it -- a backfill, a hand edit on the Pi, a future
repository with a bug. A test that inserted through the dataclass would be testing the
dataclass twice and the database not at all.

**SQLite names the columns of a failed `UNIQUE`, not the constraint.** Its message is
`UNIQUE constraint failed: exchange_fills.exchange_account_id, exchange_fills.external_trade_id`,
so the refusal is asserted by those two columns, and the reflection test beside it ties the
name `uq_exchange_fills_account_trade` to exactly that column pair. A `CHECK` failure does
carry its name, and those are asserted by name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.engine import create_session_factory
from portfolio.db.models import ExchangeFill
from portfolio.db.types import NumericText
from portfolio.domain.exchanges import ExchangeKey, FillSide
from tests.balance_harness import sqlite_timestamp

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.engine.interfaces import ReflectedUniqueConstraint
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

EXECUTED_AT: Final = datetime(2026, 9, 3, 15, 30, 0, 123000, tzinfo=UTC)
INGESTED_AT: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

#: A quantity distinctive enough that its absence from an error message means something.
MARKED_QUANTITY: Final = "0.000424242424242424"

INSERT_USER: Final = text(
    "INSERT INTO users (username, password_hash, created_at) "
    "VALUES (:username, 'not-a-hash', :created_at) RETURNING id"
)
INSERT_ACCOUNT: Final = text(
    "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
    "VALUES (:user_id, :exchange_key, :created_at) RETURNING id"
)
INSERT_FILL: Final = text(
    "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, external_order_id, "
    "symbol, base_asset, quote_asset, side, quantity, price, quote_quantity, "
    "quote_quantity_derived, fee_amount, fee_asset, executed_at, raw_payload, ingested_at) "
    "VALUES (:exchange_account_id, :external_trade_id, :external_order_id, :symbol, "
    ":base_asset, :quote_asset, :side, :quantity, :price, :quote_quantity, "
    ":quote_quantity_derived, :fee_amount, :fee_asset, :executed_at, :raw_payload, "
    ":ingested_at) RETURNING id"
)


def fill_row(account_id: int, **overrides: object) -> dict[str, object]:
    """A fill as raw column values, in the stored form `NumericText(18)` would write."""
    row: dict[str, object] = {
        "exchange_account_id": account_id,
        "external_trade_id": "1001",
        "external_order_id": "5001",
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "side": "buy",
        "quantity": MARKED_QUANTITY,
        "price": "86000.100000000000000000",
        "quote_quantity": "36.484273484273484273",
        "quote_quantity_derived": 0,
        "fee_amount": "0.036484273484273484",
        "fee_asset": "USDT",
        "executed_at": sqlite_timestamp(EXECUTED_AT),
        "raw_payload": '{"tradeId":"1001"}',
        "ingested_at": sqlite_timestamp(INGESTED_AT),
    }
    row.update(overrides)
    return row


async def insert_user(connection: AsyncConnection, username: str = "owner") -> int:
    result = await connection.execute(
        INSERT_USER, {"username": username, "created_at": sqlite_timestamp(INGESTED_AT)}
    )
    identifier: int = result.scalar_one()
    return identifier


async def insert_account(
    connection: AsyncConnection, user_id: int, exchange_key: str = ExchangeKey.BITGET.value
) -> int:
    result = await connection.execute(
        INSERT_ACCOUNT,
        {
            "user_id": user_id,
            "exchange_key": exchange_key,
            "created_at": sqlite_timestamp(INGESTED_AT),
        },
    )
    identifier: int = result.scalar_one()
    return identifier


async def insert_fill(connection: AsyncConnection, row: dict[str, object]) -> int:
    identifier: int = (await connection.execute(INSERT_FILL, row)).scalar_one()
    return identifier


async def count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as connection:
        # The table name is one of two literals in this module, never input.
        total: int = (await connection.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()  # noqa: S608
    return total


@pytest.fixture
async def accounts(migrated_engine: AsyncEngine) -> tuple[int, int]:
    """One owner with a Bitget and a BingX account, committed."""
    async with migrated_engine.begin() as connection:
        owner = await insert_user(connection)
        bitget = await insert_account(connection, owner, ExchangeKey.BITGET.value)
        bingx = await insert_account(connection, owner, ExchangeKey.BINGX.value)
    return bitget, bingx


async def refused(engine: AsyncEngine, statement: Any, row: dict[str, object]) -> str:
    """Run one insert that must fail, and hand back the rendered `IntegrityError`."""
    with pytest.raises(IntegrityError) as caught:
        async with engine.begin() as connection:
            await connection.execute(statement, row)
    return f"{caught.value}{caught.value!r}"


# --------------------------------------------------------------------------------------
# Criterion 7: one trade, once, per account
# --------------------------------------------------------------------------------------


async def test_the_same_trade_twice_on_one_account_is_refused_by_the_database(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    bitget, bingx = accounts
    async with migrated_engine.begin() as connection:
        await insert_fill(connection, fill_row(bitget))

    # A different order id, side and amount: the constraint is on the pair, not the row.
    rendered = await refused(
        migrated_engine,
        INSERT_FILL,
        fill_row(bitget, external_order_id="5002", side="sell", quantity="1.0"),
    )

    assert "UNIQUE constraint failed" in rendered
    assert "exchange_fills.exchange_account_id" in rendered
    assert "exchange_fills.external_trade_id" in rendered
    assert await count(migrated_engine, "exchange_fills") == 1

    # The positive companion: the same trade id on a different account is a different fill.
    async with migrated_engine.begin() as connection:
        await insert_fill(connection, fill_row(bingx))
    assert await count(migrated_engine, "exchange_fills") == 2


async def test_the_unique_constraint_is_named_and_covers_exactly_the_pair(
    migrated_engine: AsyncEngine,
) -> None:
    """Ties the name the spec gives to the two columns SQLite's message reports."""

    def reflect(connection: Connection) -> list[ReflectedUniqueConstraint]:
        return inspect(connection).get_unique_constraints("exchange_fills")

    async with migrated_engine.connect() as connection:
        uniques = await connection.run_sync(reflect)

    assert [(unique["name"], unique["column_names"]) for unique in uniques] == [
        ("uq_exchange_fills_account_trade", ["exchange_account_id", "external_trade_id"])
    ]


async def test_a_refused_duplicate_does_not_render_the_row(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    """A fill quantity is the owner's holdings; the refusal must stay legible without it."""
    bitget, _ = accounts
    async with migrated_engine.begin() as connection:
        await insert_fill(connection, fill_row(bitget))

    rendered = await refused(migrated_engine, INSERT_FILL, fill_row(bitget))

    assert "UNIQUE" in rendered
    assert MARKED_QUANTITY not in rendered
    assert "424242" not in rendered


# --------------------------------------------------------------------------------------
# Criterion 7: the constraint that makes the unique one mean anything
# --------------------------------------------------------------------------------------


async def test_an_empty_trade_id_is_refused_by_the_database(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    """Two empty ids would collide, and under `ON CONFLICT DO NOTHING` the second would vanish."""
    bitget, _ = accounts

    rendered = await refused(migrated_engine, INSERT_FILL, fill_row(bitget, external_trade_id=""))

    assert "CHECK constraint failed" in rendered
    assert "ck_exchange_fills_external_trade_id" in rendered
    assert await count(migrated_engine, "exchange_fills") == 0

    # The companion: a short id that is not empty inserts.
    async with migrated_engine.begin() as connection:
        await insert_fill(connection, fill_row(bitget, external_trade_id="0"))
    assert await count(migrated_engine, "exchange_fills") == 1


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param({"side": "long"}, "ck_exchange_fills_side", id="unknown side"),
        pytest.param({"side": "BUY"}, "ck_exchange_fills_side", id="side in the wrong case"),
        pytest.param({"side": ""}, "ck_exchange_fills_side", id="empty side"),
        pytest.param(
            {"quote_quantity_derived": 2},
            "ck_exchange_fills_quote_quantity_derived",
            id="flag of two",
        ),
        pytest.param(
            {"quote_quantity_derived": "true"},
            "ck_exchange_fills_quote_quantity_derived",
            id="flag as text",
        ),
    ],
)
async def test_side_and_derived_flag_are_constrained(
    migrated_engine: AsyncEngine,
    accounts: tuple[int, int],
    overrides: dict[str, object],
    constraint: str,
) -> None:
    bitget, _ = accounts

    rendered = await refused(migrated_engine, INSERT_FILL, fill_row(bitget, **overrides))

    assert "CHECK constraint failed" in rendered
    assert constraint in rendered


async def test_every_legal_side_and_flag_inserts(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    """The companion to the refusals above: the constraints admit exactly the enum's values."""
    bitget, _ = accounts
    legal = [(side.value, flag) for side in FillSide for flag in (0, 1)]

    async with migrated_engine.begin() as connection:
        for number, (side, flag) in enumerate(legal):
            await insert_fill(
                connection,
                fill_row(
                    bitget,
                    external_trade_id=str(number),
                    side=side,
                    quote_quantity_derived=flag,
                ),
            )

    assert await count(migrated_engine, "exchange_fills") == len(legal) == 4


async def test_an_unknown_exchange_key_is_refused(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    del accounts
    async with migrated_engine.connect() as connection:
        owner = (await connection.execute(text("SELECT id FROM users"))).scalar_one()

    rendered = await refused(
        migrated_engine,
        INSERT_ACCOUNT,
        {"user_id": owner, "exchange_key": "binance", "created_at": sqlite_timestamp(INGESTED_AT)},
    )

    assert "ck_exchange_accounts_exchange_key" in rendered


async def test_one_account_per_venue_per_owner(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    """Credentials come from the environment, one set per venue, so one account per venue."""
    del accounts
    async with migrated_engine.connect() as connection:
        owner = (await connection.execute(text("SELECT id FROM users"))).scalar_one()

    rendered = await refused(
        migrated_engine,
        INSERT_ACCOUNT,
        {
            "user_id": owner,
            "exchange_key": ExchangeKey.BITGET.value,
            "created_at": sqlite_timestamp(INGESTED_AT),
        },
    )

    assert "UNIQUE constraint failed" in rendered
    assert "exchange_accounts.user_id" in rendered
    assert "exchange_accounts.exchange_key" in rendered
    assert await count(migrated_engine, "exchange_accounts") == 2


# --------------------------------------------------------------------------------------
# Criterion 7: fills are an immutable event log
# --------------------------------------------------------------------------------------


async def test_an_account_with_fills_cannot_be_deleted(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    bitget, bingx = accounts
    async with migrated_engine.begin() as connection:
        await insert_fill(connection, fill_row(bitget))

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        async with migrated_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM exchange_accounts WHERE id = :id"), {"id": bitget}
            )

    assert await count(migrated_engine, "exchange_fills") == 1
    # The companion: an account with no history is deletable, so the refusal above is
    # about the fills and not about accounts being undeletable altogether.
    async with migrated_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM exchange_accounts WHERE id = :id"), {"id": bingx}
        )
    assert await count(migrated_engine, "exchange_accounts") == 1


async def test_a_fill_for_an_account_that_does_not_exist_is_refused(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    del accounts

    rendered = await refused(migrated_engine, INSERT_FILL, fill_row(424242))

    assert "FOREIGN KEY constraint failed" in rendered


# --------------------------------------------------------------------------------------
# Criterion 7: amounts survive exactly
# --------------------------------------------------------------------------------------


def test_the_four_money_columns_are_numeric_text_at_eighteen_places() -> None:
    """Pinned by hand at 18: a fill column built at another scale would round what it stores."""
    columns = ExchangeFill.__table__.columns

    for name in ("quantity", "price", "quote_quantity", "fee_amount"):
        column_type = columns[name].type
        assert isinstance(column_type, NumericText), name
        assert column_type.scale == 18, name


async def test_fill_amounts_round_trip_exactly(
    migrated_engine: AsyncEngine, accounts: tuple[int, int]
) -> None:
    """Written through the ORM; read back as text off the file, and through a fresh session.

    The 18-place values come back with the same spelling; `0.00012300` comes back equal,
    padded to the column's 18 places, which is what declaring a scale means. The widest
    value is 20 integer digits and 18 fractional ones -- 38, all of the money precision.
    """
    bitget, _ = accounts
    widest = Decimal("9" * 20 + "." + "9" * 18)
    values = {
        "quantity": Decimal("0.00012300"),
        "price": widest,
        "quote_quantity": Decimal("0.000000000000000001"),
        "fee_amount": Decimal("-0.012345678901234567"),
    }
    factory = create_session_factory(migrated_engine)

    async with factory() as session:
        fill = ExchangeFill(
            exchange_account_id=bitget,
            external_trade_id="1001",
            external_order_id=None,
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            side=FillSide.SELL,
            quote_quantity_derived=True,
            fee_asset="USDT",
            executed_at=EXECUTED_AT,
            raw_payload='{"tradeId":"1001"}',
            ingested_at=INGESTED_AT,
            **values,
        )
        session.add(fill)
        await session.commit()
        fill_id = fill.id

    async with migrated_engine.connect() as connection:
        raw = (
            await connection.execute(
                text(
                    "SELECT quantity, price, quote_quantity, fee_amount, typeof(quantity), "
                    "typeof(price), side, quote_quantity_derived FROM exchange_fills "
                    "WHERE id = :id"
                ),
                {"id": fill_id},
            )
        ).one()

    assert raw[0] == "0.000123000000000000"
    assert raw[1] == "99999999999999999999.999999999999999999"
    assert raw[2] == "0.000000000000000001"
    assert raw[3] == "-0.012345678901234567"
    assert (raw[4], raw[5]) == ("text", "text")
    assert (raw[6], raw[7]) == ("sell", 1)

    async with factory() as session:
        reread = (
            await session.execute(select(ExchangeFill).where(ExchangeFill.id == fill_id))
        ).scalar_one()

    # `format(..., "f")`, because `str()` renders 1E-18 in exponent form.
    assert reread.quantity == Decimal("0.00012300")
    assert format(reread.price, "f") == "99999999999999999999.999999999999999999"
    assert format(reread.quote_quantity, "f") == "0.000000000000000001"
    assert format(reread.fee_amount, "f") == "-0.012345678901234567"
    assert reread.quote_quantity_derived is True
    assert reread.executed_at == EXECUTED_AT
    assert reread.external_order_id is None


async def test_the_money_columns_are_declared_text(migrated_engine: AsyncEngine) -> None:
    """`TEXT` in the DDL itself, so SQLite never applies numeric affinity to a fill amount."""
    async with migrated_engine.connect() as connection:
        rows = (await connection.execute(text("PRAGMA table_info(exchange_fills)"))).all()

    declared = {row[1]: row[2] for row in rows}

    for name in ("quantity", "price", "quote_quantity", "fee_amount"):
        assert declared[name] == "TEXT", name
