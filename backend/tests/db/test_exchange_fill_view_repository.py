"""Spec 024's repository row: `list_fills_for_view` reads the owner's fills, and nothing else.

`ExchangeFillRepository.list_fills_for_view(user_id, exchanges)` is driven against a real
migrated file -- never `:memory:`, never `create_all` -- holding the book of
`tests/fill_view_harness.py` for the owner and a second history for another user.

What is pinned here, and why it cannot be seen from the endpoint alone:

* **`raw_payload` is never selected**, on the compiled statement and on every statement
  actually sent to SQLite during a read; nor is the venue's trade id, which is never served.
* **The venue is the only filter in SQL**, and it is an `IN` on the account's enum column. No
  `executed_at` and no money column appears in a `WHERE` or an `ORDER BY`: those are text in
  SQLite, and comparing them there is the float and string coercion rule 2 forbids.
* **The owner scope**: another user's fills on the same venue are never read.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import sqlite

from portfolio.db.models import FILL_SCALE
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.domain.fill_totals import TOTALS_SCALE
from portfolio.repositories.exchanges import (
    ExchangeFillRepository,
    FillViewRecord,
    select_fills_for_view,
)
from tests.accounting_harness import plant_owner
from tests.fill_view_harness import (
    PAYLOAD_SENTINEL,
    TRADE_ID_PREFIX,
    book_fill,
    minute,
    plant_history,
    row_ids,
    the_book,
    trade_id,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import NormalizedFill

#: The record's fields, exactly: what the view shows and totals, and nothing that identifies
#: the fill at the venue except the order id the owner needs to find it there.
RECORD_FIELDS: Final = frozenset(
    {
        "id",
        "exchange_key",
        "external_order_id",
        "symbol",
        "base_asset",
        "quote_asset",
        "side",
        "quantity",
        "price",
        "quote_quantity",
        "quote_quantity_derived",
        "fee_amount",
        "fee_asset",
        "executed_at",
    }
)

#: Columns that must never be compared or ordered in SQL: text in SQLite, so a comparison
#: there is a string or float comparison, not a comparison of instants or amounts.
UNORDERABLE_COLUMNS: Final = (
    "executed_at",
    "ingested_at",
    "quantity",
    "price",
    "quote_quantity",
    "fee_amount",
)

#: Numbers of the other user's fills: a range no book fill uses.
INTRUDER_FILLS: Final = (9001, 9002)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


async def planted(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """The owner with the book, and another user with two Bitget fills. Returns both ids."""
    async with factory() as session:
        owner = await plant_owner(session, "owner")
        intruder = await plant_owner(session, "intruder")
    await plant_history(factory, owner, the_book())
    await plant_history(
        factory,
        intruder,
        {
            ExchangeKey.BITGET: [
                book_fill(number, minute(15), order_id=f"ord-{number}") for number in INTRUDER_FILLS
            ]
        },
    )
    return owner, intruder


async def read(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    exchanges: Collection[ExchangeKey] | None,
) -> Sequence[FillViewRecord]:
    async with factory() as session:
        return await ExchangeFillRepository(session).list_fills_for_view(user_id, exchanges)


def numbers(records: Sequence[FillViewRecord], ids: dict[int, int]) -> list[int]:
    """The book numbers of `records`, in the order they came back."""
    by_row = {row: number for number, row in ids.items()}
    return [by_row[record.id] for record in records]


def compiled(user_id: int, exchanges: Collection[ExchangeKey] | None) -> str:
    """The statement as SQLite would receive it, with its parameters inlined."""
    return str(
        select_fills_for_view(user_id, exchanges).compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def clauses(statement: str) -> tuple[str, str]:
    """The `WHERE` and `ORDER BY` parts of a statement, as text; empty when absent."""
    ordered = statement.split("ORDER BY", 1)
    order_by = ordered[1] if len(ordered) == 2 else ""
    filtered = ordered[0].split("WHERE", 1)
    where = filtered[1] if len(filtered) == 2 else ""
    return where, order_by


# --------------------------------------------------------------------------------------
# `raw_payload` and the trade id are never selected
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exchanges",
    [None, (ExchangeKey.BITGET,), (ExchangeKey.BITGET, ExchangeKey.BINGX), ()],
    ids=["every venue", "one venue", "two venues", "no venue"],
)
def test_the_view_select_names_its_columns_and_never_the_payload(
    exchanges: Collection[ExchangeKey] | None,
) -> None:
    """The compiled statement, whatever the venue filter."""
    statement = compiled(7, exchanges)

    assert "raw_payload" not in statement
    assert "external_trade_id" not in statement, "the venue's trade id is never served"
    assert "*" not in statement, "the columns are selected explicitly"
    for column in (
        "exchange_fills.id",
        "exchange_accounts.exchange_key",
        "external_order_id",
        "base_asset",
        "quote_asset",
        "quote_quantity_derived",
        "fee_amount",
        "fee_asset",
        "executed_at",
    ):
        assert column in statement, column


@pytest.mark.parametrize(
    "exchanges",
    [None, (ExchangeKey.BINGX, ExchangeKey.BITGET)],
    ids=["every venue", "two venues"],
)
def test_nothing_but_integers_and_the_venue_is_compared_or_ordered_in_sql(
    exchanges: Collection[ExchangeKey] | None,
) -> None:
    """The owner by id, the venue by its enum text, the order by row id. Nothing else."""
    where, order_by = clauses(compiled(7, exchanges))

    assert order_by.strip() == "exchange_fills.id"
    assert "exchange_accounts.user_id = 7" in where
    for column in UNORDERABLE_COLUMNS:
        assert column not in where, f"{column} is compared in SQL"
        assert column not in order_by, f"{column} is ordered in SQL"


def test_the_venue_filter_is_an_in_over_the_account_key() -> None:
    """Sorted and deduplicated, so the same set of venues is the same statement."""
    where, _order_by = clauses(compiled(7, [ExchangeKey.BITGET, ExchangeKey.BINGX]))
    repeated, _ = clauses(compiled(7, [ExchangeKey.BINGX, ExchangeKey.BITGET, ExchangeKey.BINGX]))

    assert "exchange_accounts.exchange_key IN ('bingx', 'bitget')" in where
    assert repeated == where


def test_no_venue_filter_leaves_the_clause_out() -> None:
    where, _order_by = clauses(compiled(7, None))

    assert "exchange_key" not in where


async def test_no_statement_a_read_sends_touches_the_payload(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every statement sent to SQLite during a read, not only the one compiled above."""
    owner, _intruder = await planted(factory)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement)

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            records = await ExchangeFillRepository(session).list_fills_for_view(owner, None)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert len(records) == 7, "the control: the read returned the book"
    assert any("exchange_fills" in statement for statement in statements), statements
    assert [statement for statement in statements if "raw_payload" in statement] == []
    assert [statement for statement in statements if "external_trade_id" in statement] == []


async def test_no_record_carries_the_payload_or_the_trade_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The record type has no field for either, and no value read holds either."""
    owner, _intruder = await planted(factory)

    records = await read(factory, owner, None)

    assert {field.name for field in dataclasses.fields(FillViewRecord)} == RECORD_FIELDS
    rendered = repr(records)
    assert PAYLOAD_SENTINEL not in rendered
    assert TRADE_ID_PREFIX not in rendered


# --------------------------------------------------------------------------------------
# What is read, and whose
# --------------------------------------------------------------------------------------


async def test_every_column_the_view_needs_is_read_back_as_stored(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each record against the fill that was planted: types and values, field by field."""
    owner, _intruder = await planted(factory)
    ids = await row_ids(factory)
    planted_fills: dict[int, tuple[ExchangeKey, NormalizedFill]] = {
        int(fill.external_trade_id.removeprefix(TRADE_ID_PREFIX)): (venue, fill)
        for venue, fills in the_book().items()
        for fill in fills
    }

    records = await read(factory, owner, None)

    assert sorted(numbers(records, ids)) == sorted(planted_fills)
    for record in records:
        number = numbers([record], ids)[0]
        venue, fill = planted_fills[number]
        assert record.exchange_key is venue
        assert record.external_order_id == fill.external_order_id
        assert (record.symbol, record.base_asset, record.quote_asset) == (
            fill.symbol,
            fill.base_asset,
            fill.quote_asset,
        )
        assert isinstance(record.side, FillSide)
        assert record.side is fill.side
        for name in ("quantity", "price", "quote_quantity", "fee_amount"):
            value = getattr(record, name)
            assert isinstance(value, Decimal), name
            assert value == getattr(fill, name), name
        assert record.quote_quantity_derived is fill.quote_quantity_derived
        assert record.fee_asset == fill.fee_asset
        assert record.executed_at == fill.executed_at
        assert record.executed_at.tzinfo is not None
        assert record.executed_at.utcoffset() == timedelta(0)


async def test_a_missing_order_id_and_a_derived_quote_are_read_as_they_are(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """1004 has no order id; 2003's quote quantity was derived by the provider."""
    owner, _intruder = await planted(factory)
    ids = await row_ids(factory)

    read_back = await read(factory, owner, None)
    records = dict(zip(numbers(read_back, ids), read_back, strict=True))

    assert records[1004].external_order_id is None
    assert records[1003].external_order_id == "ord-1003"
    assert records[2003].quote_quantity_derived is True
    assert records[2002].quote_quantity_derived is False


async def test_the_records_come_back_in_row_id_order(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The read is deterministic; the view's own order is the service's."""
    owner, _intruder = await planted(factory)

    records = await read(factory, owner, None)

    assert [record.id for record in records] == sorted(record.id for record in records)


async def test_another_owners_fills_are_never_read(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The intruder trades on Bitget too; the owner's Bitget read holds none of it."""
    owner, intruder = await planted(factory)
    ids = await row_ids(factory)

    owners = numbers(await read(factory, owner, None), ids)
    owners_bitget = numbers(await read(factory, owner, [ExchangeKey.BITGET]), ids)
    theirs = numbers(await read(factory, intruder, None), ids)

    assert set(INTRUDER_FILLS).isdisjoint(owners)
    assert set(INTRUDER_FILLS).isdisjoint(owners_bitget)
    assert theirs == list(INTRUDER_FILLS)


async def test_a_user_with_no_account_reads_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await planted(factory)
    async with factory() as session:
        newcomer = await plant_owner(session, "newcomer")

    assert list(await read(factory, newcomer, None)) == []


@pytest.mark.parametrize(
    ("exchanges", "expected"),
    [
        (None, {1001, 1002, 1003, 1004, 2001, 2002, 2003}),
        ((ExchangeKey.BITGET,), {1001, 1002, 1003, 1004}),
        ((ExchangeKey.BINGX,), {2001, 2002, 2003}),
        ((ExchangeKey.BITGET, ExchangeKey.BINGX), {1001, 1002, 1003, 1004, 2001, 2002, 2003}),
        ((ExchangeKey.BINGX, ExchangeKey.BINGX), {2001, 2002, 2003}),
        ((), set()),
    ],
    ids=["none is all", "bitget", "bingx", "both", "a repeat counts once", "empty is none"],
)
async def test_the_venue_filter_selects_exactly_those_venues(
    factory: async_sessionmaker[AsyncSession],
    exchanges: Collection[ExchangeKey] | None,
    expected: set[int],
) -> None:
    owner, _intruder = await planted(factory)
    ids = await row_ids(factory)

    found = numbers(await read(factory, owner, exchanges), ids)

    assert sorted(found) == sorted(expected), "each fill exactly once"


async def test_a_venue_the_owner_has_no_account_at_reads_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The intruder has only Bitget: asking for BingX alone returns nothing, not an error."""
    _owner, intruder = await planted(factory)

    assert list(await read(factory, intruder, [ExchangeKey.BINGX])) == []


async def test_the_trade_id_helper_is_what_was_stored(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The harness's own control: the ids it maps back are the ones in the table."""
    await planted(factory)

    ids = await row_ids(factory)

    assert set(ids) == {1001, 1002, 1003, 1004, 2001, 2002, 2003, *INTRUDER_FILLS}
    assert trade_id(1001) == f"{TRADE_ID_PREFIX}1001"


def test_the_totals_scale_is_the_column_scale() -> None:
    """`domain` cannot import `db`, so the scale an empty total is spelled at is a copy.

    Pinned here, where both are importable: a column rescaled without the copy would serve
    empty totals at one scale and every other figure at another.
    """
    assert TOTALS_SCALE == FILL_SCALE == 18


def test_the_record_fields_follow_the_select_column_order() -> None:
    """`list_fills_for_view` unpacks each row by position, so the two orders are one.

    A column moved in the select without the record, or the reverse, would put a price in the
    quantity and a symbol in the base asset -- every value still a valid string or amount, and
    every total quietly wrong. The repository's docstring cites this test.
    """
    selected = [column.key for column in select_fills_for_view(1, None).selected_columns]

    assert selected == [field.name for field in dataclasses.fields(FillViewRecord)]
