"""Spec 024's service row: `ExchangeService.list_fills` filters, orders, totals, then pages.

The real repository over a real migrated file, holding the book of
`tests/fill_view_harness.py` for the owner and two fills of another user's. Every expected
total is `BOOK_TOTALS`, worked by hand there.

What each block pins, against the issue's backend criteria:

* **newest first, ties broken by id descending** -- the book has a tie within a venue and one
  across venues, inserted so that row-id order and time order disagree;
* **the venue filter**: none is every venue, one, several, a repeat counting once, and an empty
  selection selecting nothing;
* **the half-open range**: `from` inclusive, `to` exclusive, so each boundary fill lands in
  exactly one of two adjacent ranges; bounds in any offset are the same instants;
* **refusals**: a naive bound, one UTC cannot represent, and `from` not before `to`, each an
  `InvalidFillRangeError` naming the parameter and never the value, raised before any read;
* **totals over the whole filtered set**, whatever `limit` and `offset` are, and pages that
  concatenate to the whole;
* **the owner scope**: another user's fills on the same venue are never listed or totalled.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event

import portfolio.services.exchanges as exchanges_service
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.repositories.exchanges import ExchangeFillRepository, decode_fill_view_rows
from portfolio.services.exchanges import (
    DEFAULT_FILLS_LIMIT,
    INVERTED_RANGE_RULE,
    MAX_FILLS_LIMIT,
    NAIVE_BOUND_RULE,
    UNREPRESENTABLE_BOUND_RULE,
    FillsPage,
    FillView,
    InvalidFillRangeError,
    build_exchange_service,
)
from tests.accounting_harness import FillRow, plant_owner
from tests.fill_view_harness import (
    BOOK_ORDER,
    BOOK_TOTALS,
    EMPTY_TOTALS,
    book_fill,
    minute,
    plant_history,
    row_ids,
    the_book,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.domain.fill_totals import FillTotals

#: The other user's fills: Bitget, like the owner's, and in the middle of the book's range.
INTRUDER_FILLS: Final = (9001, 9002)

#: How long a cancelled request may take to raise, and how long a held thread waits at most.
CANCEL_BOUND: Final = 2
HELD_THREAD_SECONDS: Final = 5

#: Every book fill, as numbers.
EVERY_FILL: Final = frozenset(BOOK_ORDER)

EVERYTHING: Final = 10_000
"""A limit no page here reaches, clamped to `MAX_FILLS_LIMIT` by the service anyway."""


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


@pytest.fixture
async def book(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int, dict[int, int]]:
    """The owner with the book, the intruder with two Bitget fills; both ids and the row ids."""
    async with factory() as session:
        owner = await plant_owner(session, "owner")
        intruder = await plant_owner(session, "intruder")
    await plant_history(factory, owner, the_book())
    await plant_history(
        factory,
        intruder,
        {
            ExchangeKey.BITGET: [
                book_fill(number, minute(10), quantity="7", quote_quantity="420000")
                for number in INTRUDER_FILLS
            ]
        },
    )
    return owner, intruder, await row_ids(factory)


async def list_fills(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    *,
    exchanges: Collection[ExchangeKey] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = EVERYTHING,
    offset: int = 0,
) -> FillsPage:
    async with factory() as session:
        service = build_exchange_service(session, configured=frozenset(), syncing=False)
        return await service.list_fills(
            user_id, exchanges=exchanges, from_=from_, to=to, limit=limit, offset=offset
        )


def numbers(page: FillsPage, ids: dict[int, int]) -> list[int]:
    """The book numbers of the page's fills, in the order served."""
    by_row = {row: number for number, row in ids.items()}
    return [by_row[view.id] for view in page.fills]


def plain(totals: FillTotals) -> dict[str, Any]:
    """The totals as the nested dictionaries `BOOK_TOTALS` is written in."""

    def lists(node: object) -> object:
        if isinstance(node, dict):
            return {key: lists(value) for key, value in node.items()}
        if isinstance(node, list | tuple):
            return [lists(item) for item in node]
        return node

    converted = lists(dataclasses.asdict(totals))
    assert isinstance(converted, dict)
    return converted


# --------------------------------------------------------------------------------------
# Order, and what a row carries
# --------------------------------------------------------------------------------------


async def test_the_fills_are_newest_first_with_ties_broken_by_id_descending(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """The book's two ties, one within Bitget and one across venues, come out id-descending."""
    owner, _intruder, ids = book

    page = await list_fills(factory, owner)

    assert numbers(page, ids) == list(BOOK_ORDER)
    assert ids[1004] > ids[1003], "the control: the tie really is broken by the higher id"
    assert ids[2001] > ids[1002]
    served = [(view.executed_at, view.id) for view in page.fills]
    assert served == sorted(served, reverse=True)


async def test_each_row_carries_the_stored_fill_and_its_usdt_value(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """USDT value is the stored quote quantity for a USDT fill and `None` for any other."""
    owner, _intruder, ids = book

    page = await list_fills(factory, owner)

    by_number = dict(zip(numbers(page, ids), page.fills, strict=True))
    eth_sale = by_number[2002]
    assert eth_sale == FillView(
        id=ids[2002],
        executed_at=minute(5),
        exchange_key=ExchangeKey.BINGX,
        symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        side=FillSide.SELL,
        quantity=Decimal("1"),
        price=Decimal("3000"),
        quote_quantity=Decimal("3000.123456789012345678"),
        quote_quantity_derived=False,
        usdt_value=Decimal("3000.123456789012345678"),
        fee_amount=Decimal("-0.01"),
        fee_asset="BNB",
        order_id="ord-2002",
    )
    assert eth_sale.usdt_value != eth_sale.quantity * eth_sale.price, "never recomputed"
    assert by_number[1003].usdt_value is None, "quoted in BTC"
    assert by_number[1004].usdt_value is None, "quoted in USDC"
    assert (by_number[1004].order_id, by_number[1004].fee_asset) == (None, None)
    assert by_number[2003].quote_quantity_derived is True
    assert by_number[2003].usdt_value == Decimal("100")


# --------------------------------------------------------------------------------------
# The venue filter
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exchanges", "expected"),
    [
        (None, EVERY_FILL),
        ((ExchangeKey.BITGET,), {1001, 1002, 1003, 1004}),
        ((ExchangeKey.BINGX,), {2001, 2002, 2003}),
        ((ExchangeKey.BINGX, ExchangeKey.BITGET), EVERY_FILL),
        ((ExchangeKey.BITGET, ExchangeKey.BITGET), {1001, 1002, 1003, 1004}),
    ],
    ids=["none is every venue", "bitget", "bingx", "both", "a repeat counts once"],
)
async def test_filtering_by_venue_returns_exactly_the_matching_fills(
    factory: async_sessionmaker[AsyncSession],
    book: tuple[int, int, dict[int, int]],
    exchanges: Collection[ExchangeKey] | None,
    expected: Collection[int],
) -> None:
    owner, _intruder, ids = book

    page = await list_fills(factory, owner, exchanges=exchanges)

    served = numbers(page, ids)
    assert sorted(served) == sorted(expected), "each matching fill exactly once"
    assert page.total_count == page.totals.fill_count == len(expected)


async def test_one_venue_totals_only_that_venue(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """BingX alone: BTC 0.3 bought for 18000; ETH 1 sold for 3000.12...; KAS 1000 for 100."""
    owner, _intruder, _ids = book

    page = await list_fills(factory, owner, exchanges=[ExchangeKey.BINGX])

    totals = page.totals
    assert {row.asset: (row.bought, row.sold, row.net) for row in totals.by_asset} == {
        "BTC": (Decimal("0.3"), Decimal(0), Decimal("0.3")),
        "ETH": (Decimal(0), Decimal(1), Decimal(-1)),
        "KAS": (Decimal(1000), Decimal(0), Decimal(1000)),
    }
    assert (totals.usdt.spent, totals.usdt.received) == (
        Decimal("18100"),
        Decimal("3000.123456789012345678"),
    )
    assert totals.not_valued_in_usdt.fill_count == 0
    assert {fee.asset: fee.amount for fee in totals.fees} == {"BNB": 0, "USDT": Decimal("0.1")}


async def test_an_empty_venue_selection_is_an_empty_page_with_zero_totals(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    owner, _intruder, _ids = book

    page = await list_fills(factory, owner, exchanges=())

    assert (page.fills, page.total_count) == ((), 0)
    assert plain(page.totals) == EMPTY_TOTALS


# --------------------------------------------------------------------------------------
# The half-open range
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (0, 10, {1001, 2002}),
        (10, 20, {1002, 2001}),
        (20, 30, {1003, 1004}),
        (30, 40, {2003}),
    ],
    ids=["[0, 10)", "[10, 20)", "[20, 30)", "[30, 40)"],
)
async def test_from_is_inclusive_and_to_is_exclusive(
    factory: async_sessionmaker[AsyncSession],
    book: tuple[int, int, dict[int, int]],
    start: int,
    end: int,
    expected: set[int],
) -> None:
    """A fill at `minute(10)` is in `[10, 20)` and not in `[0, 10)`."""
    owner, _intruder, ids = book

    page = await list_fills(factory, owner, from_=minute(start), to=minute(end))

    assert set(numbers(page, ids)) == expected
    assert page.totals.fill_count == len(expected)


async def test_a_fill_on_a_boundary_is_in_exactly_one_of_two_adjacent_ranges(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """Adjacent ranges meeting at every boundary the book has: a partition, nothing twice."""
    owner, _intruder, ids = book
    cuts = [minute(-1), minute(5), minute(10), minute(20), minute(30), minute(31)]

    ranges = [
        set(numbers(await list_fills(factory, owner, from_=since, to=until), ids))
        for since, until in pairwise(cuts)
    ]

    assert sum(len(found) for found in ranges) == len(EVERY_FILL), "no fill counted twice"
    assert set().union(*ranges) == EVERY_FILL, "no fill lost"
    assert ranges[1] == {2002}, "the fill at minute 5 opens its range"
    assert ranges[2] == {1002, 2001}, "the two fills at minute 10 open theirs"
    assert ranges[3] == {1003, 1004}
    assert ranges[4] == {2003}


async def test_one_microsecond_either_side_of_a_boundary(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """`to` a microsecond after the fill includes it; `from` a microsecond after excludes it."""
    owner, _intruder, ids = book
    tick = timedelta(microseconds=1)

    ending_after = await list_fills(factory, owner, from_=minute(5), to=minute(5) + tick)
    starting_after = await list_fills(factory, owner, from_=minute(5) + tick, to=minute(10))
    ending_on = await list_fills(factory, owner, from_=minute(5) - tick, to=minute(5))

    assert numbers(ending_after, ids) == [2002]
    assert numbers(starting_after, ids) == []
    assert numbers(ending_on, ids) == []


async def test_either_bound_alone_is_open_on_the_other_side(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    owner, _intruder, ids = book

    from_only = await list_fills(factory, owner, from_=minute(20))
    to_only = await list_fills(factory, owner, to=minute(10))

    assert numbers(from_only, ids) == [2003, 1004, 1003]
    assert numbers(to_only, ids) == [2002, 1001]


async def test_a_bound_in_another_offset_is_the_same_instant(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """11:10 at +02:00 is 09:10 UTC: the same range, whatever offset the client wrote."""
    owner, _intruder, ids = book
    plus_two = timezone(timedelta(hours=2))
    minus_five = timezone(timedelta(hours=-5))

    in_utc = await list_fills(factory, owner, from_=minute(10), to=minute(20))
    shifted = await list_fills(
        factory,
        owner,
        from_=minute(10).astimezone(plus_two),
        to=minute(20).astimezone(minus_five),
    )

    assert numbers(shifted, ids) == numbers(in_utc, ids) == [2001, 1002]
    assert shifted.totals == in_utc.totals


# --------------------------------------------------------------------------------------
# Refused ranges, before anything is read
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["from", "to"])
async def test_a_naive_bound_is_refused_naming_the_parameter(
    factory: async_sessionmaker[AsyncSession],
    book: tuple[int, int, dict[int, int]],
    field: str,
) -> None:
    owner, _intruder, _ids = book
    naive = datetime(2026, 3, 1, 9, 10)  # noqa: DTZ001 - the naive bound under test
    since, until = (naive, None) if field == "from" else (None, naive)

    with pytest.raises(InvalidFillRangeError) as refused:
        await list_fills(factory, owner, from_=since, to=until)

    assert refused.value.field == field
    assert refused.value.rule == f"{field} {NAIVE_BOUND_RULE}"
    assert "2026" not in refused.value.rule.split("such as")[0], "the value is not quoted"


@pytest.mark.parametrize(
    ("since", "until"),
    [(minute(10), minute(10)), (minute(20), minute(10))],
    ids=["from equals to", "from after to"],
)
async def test_from_not_before_to_is_refused_against_to(
    factory: async_sessionmaker[AsyncSession],
    book: tuple[int, int, dict[int, int]],
    since: datetime,
    until: datetime,
) -> None:
    owner, _intruder, _ids = book

    with pytest.raises(InvalidFillRangeError) as refused:
        await list_fills(factory, owner, from_=since, to=until)

    assert (refused.value.field, refused.value.rule) == ("to", INVERTED_RANGE_RULE)


async def test_an_inverted_range_in_different_offsets_is_compared_as_instants(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """10:00 at +02:00 is before 09:00 UTC; as wall-clock text it would look later."""
    owner, _intruder, _ids = book
    since = datetime(2026, 3, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))

    with pytest.raises(InvalidFillRangeError):
        await list_fills(factory, owner, from_=minute(0), to=since)


@pytest.mark.parametrize(
    ("field", "bound"),
    [
        ("from", datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))),
        ("to", datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))),
    ],
    ids=["an hour before the first instant", "an hour after the last"],
)
async def test_a_bound_utc_cannot_represent_is_refused(
    factory: async_sessionmaker[AsyncSession],
    book: tuple[int, int, dict[int, int]],
    field: str,
    bound: datetime,
) -> None:
    owner, _intruder, _ids = book
    since, until = (bound, None) if field == "from" else (None, bound)

    with pytest.raises(InvalidFillRangeError) as refused:
        await list_fills(factory, owner, from_=since, to=until)

    assert (refused.value.field, refused.value.rule) == (
        field,
        f"{field} {UNREPRESENTABLE_BOUND_RULE}",
    )


async def test_a_refused_range_reads_nothing(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """The bounds are checked first: no statement reaches `exchange_fills`."""
    owner, _intruder, _ids = book
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement)

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        service = build_exchange_service(session, configured=frozenset(), syncing=False)
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            with pytest.raises(InvalidFillRangeError):
                await service.list_fills(
                    owner,
                    exchanges=None,
                    from_=minute(20),
                    to=minute(10),
                    limit=DEFAULT_FILLS_LIMIT,
                    offset=0,
                )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert [statement for statement in statements if "exchange_fills" in statement] == []


# --------------------------------------------------------------------------------------
# Totals over the whole filtered set, whatever the page
# --------------------------------------------------------------------------------------


async def test_the_totals_are_the_books_worked_totals(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    owner, _intruder, _ids = book

    page = await list_fills(factory, owner)

    assert plain(page.totals) == BOOK_TOTALS
    assert page.total_count == 7


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 6, 7])
async def test_paging_through_every_page_gives_the_whole_and_the_same_totals(
    factory: async_sessionmaker[AsyncSession],
    book: tuple[int, int, dict[int, int]],
    limit: int,
) -> None:
    """Every page's totals are the whole's, and the pages concatenate to the whole list."""
    owner, _intruder, ids = book
    pages: list[FillsPage] = []
    offset = 0
    while offset < len(EVERY_FILL):
        pages.append(await list_fills(factory, owner, limit=limit, offset=offset))
        offset += limit

    assert [number for page in pages for number in numbers(page, ids)] == list(BOOK_ORDER)
    assert all(len(page.fills) <= limit for page in pages)
    assert all(plain(page.totals) == BOOK_TOTALS for page in pages)
    assert all(page.total_count == 7 for page in pages)


async def test_a_filtered_page_totals_the_filtered_set_not_the_page(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """Bitget from minute 10: 1002, 1003, 1004. A page of one still totals all three."""
    owner, _intruder, ids = book

    page = await list_fills(
        factory, owner, exchanges=[ExchangeKey.BITGET], from_=minute(10), limit=1, offset=1
    )

    assert numbers(page, ids) == [1003]
    assert page.total_count == 3
    btc, eth = page.totals.by_asset
    assert (btc.asset, btc.fill_count, btc.sold) == ("BTC", 2, Decimal("0.3"))
    assert (eth.asset, eth.fill_count, eth.bought) == ("ETH", 1, Decimal("2"))


async def test_an_offset_past_the_end_is_an_empty_page_with_the_same_totals(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    owner, _intruder, _ids = book

    page = await list_fills(factory, owner, limit=DEFAULT_FILLS_LIMIT, offset=2**63 - 1)

    assert page.fills == ()
    assert page.total_count == 7
    assert plain(page.totals) == BOOK_TOTALS


async def test_a_range_with_nothing_in_it_is_empty_with_zero_totals(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    owner, _intruder, _ids = book

    page = await list_fills(factory, owner, from_=minute(1), to=minute(2))

    assert (page.fills, page.total_count) == ((), 0)
    assert plain(page.totals) == EMPTY_TOTALS


async def test_a_range_of_sales_has_negative_nets(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """Bitget over [10, 11): only 1002, a sale. Every net is buys minus sells: negative."""
    owner, _intruder, _ids = book

    page = await list_fills(
        factory, owner, exchanges=[ExchangeKey.BITGET], from_=minute(10), to=minute(11)
    )

    (btc,) = page.totals.by_asset
    assert (btc.net, btc.usdt_net) == (Decimal("-0.2"), Decimal("-13000"))
    assert page.totals.usdt.net == Decimal("-13000")


# --------------------------------------------------------------------------------------
# Limits, and the owner scope
# --------------------------------------------------------------------------------------


async def test_the_limit_and_offset_are_clamped_for_a_caller_that_skips_the_schema(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """`1..200` and `0..`, as `list_runs` clamps its own limit."""
    owner, _intruder, ids = book

    too_few = await list_fills(factory, owner, limit=0)
    negative = await list_fills(factory, owner, limit=-3, offset=-5)

    assert numbers(too_few, ids) == [BOOK_ORDER[0]]
    assert numbers(negative, ids) == [BOOK_ORDER[0]]


async def test_no_page_is_longer_than_the_maximum(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """201 fills, a limit of a thousand: 200 served, 201 counted and totalled."""
    async with factory() as session:
        owner = await plant_owner(session, "owner")
    many = [
        FillRow(
            external_trade_id=f"many-{index}",
            base_asset="BTC",
            quote_asset="USDT",
            side=FillSide.BUY,
            quantity=Decimal(1),
            quote_quantity=Decimal(2),
            fee_amount=Decimal(0),
            fee_asset=None,
            executed_at=minute(index),
        )
        for index in range(MAX_FILLS_LIMIT + 1)
    ]
    await plant_history(factory, owner, {ExchangeKey.BITGET: many})  # type: ignore[dict-item]

    page = await list_fills(factory, owner, limit=1000)

    assert len(page.fills) == MAX_FILLS_LIMIT
    assert page.total_count == MAX_FILLS_LIMIT + 1
    (btc,) = page.totals.by_asset
    assert btc.bought == MAX_FILLS_LIMIT + 1
    assert page.fills[0].executed_at == minute(MAX_FILLS_LIMIT)


async def test_another_owners_fills_are_never_listed_or_totalled(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """The intruder's two fills sit at minute 10 on Bitget, inside every range asked here."""
    owner, intruder, ids = book

    owners = await list_fills(factory, owner, exchanges=[ExchangeKey.BITGET], from_=minute(10))
    theirs = await list_fills(factory, intruder)

    assert set(INTRUDER_FILLS).isdisjoint(numbers(owners, ids))
    assert {row.asset: row.fill_count for row in owners.totals.by_asset} == {"BTC": 2, "ETH": 1}
    assert sorted(numbers(theirs, ids)) == sorted(INTRUDER_FILLS)
    (btc,) = theirs.totals.by_asset
    assert btc.bought == Decimal("14")


async def test_an_owner_with_nothing_imported_gets_an_empty_page(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        owner = await plant_owner(session, "owner")

    page = await list_fills(factory, owner)

    assert (page.fills, page.total_count) == ((), 0)
    assert plain(page.totals) == EMPTY_TOTALS


def test_the_page_sizes_are_the_specs() -> None:
    assert (DEFAULT_FILLS_LIMIT, MAX_FILLS_LIMIT) == (50, 200)


# --------------------------------------------------------------------------------------
# Spec 024, R5 (N3): only the read runs on the event loop; the rest in a worker thread
# --------------------------------------------------------------------------------------


async def test_the_rows_are_turned_into_the_page_in_a_worker_thread(
    factory: async_sessionmaker[AsyncSession],
    book: tuple[int, int, dict[int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fills_page` -- decode, range, order, totals, slice -- runs off the event loop's thread.

    At 20,000 fills that work is most of the request; on the loop, every other request would
    wait for it. The page it builds is the page the caller gets, unchanged.
    """
    owner, _intruder, ids = book
    loop_thread = threading.get_ident()
    seen: list[tuple[str, int]] = []
    build_page = exchanges_service._fills_page
    decode = decode_fill_view_rows

    def spied_page(*arguments: Any) -> FillsPage:
        seen.append(("page", threading.get_ident()))
        return build_page(*arguments)

    def spied_decode(rows: Any) -> list[Any]:
        seen.append(("decode", threading.get_ident()))
        return decode(rows)

    monkeypatch.setattr(exchanges_service, "_fills_page", spied_page)
    monkeypatch.setattr(exchanges_service, "decode_fill_view_rows", spied_decode)

    page = await list_fills(factory, owner)

    assert [name for name, _thread in seen] == ["page", "decode"]
    assert all(thread != loop_thread for _name, thread in seen), "the work ran on the loop"
    assert numbers(page, ids) == list(BOOK_ORDER), "the control: the page is the book's"
    assert plain(page.totals) == BOOK_TOTALS


async def test_the_read_hands_the_thread_plain_values_that_outlive_the_session(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """The fetched rows are tuples of converted values, decodable after the session closes.

    `NumericText` and `UtcDateTime` have already run: an amount is a `Decimal` and an instant
    an aware `datetime`, so nothing in the rows reaches back to a session, a connection or a
    cursor from the worker thread.
    """
    owner, _intruder, _ids = book
    async with factory() as session:
        repository = ExchangeFillRepository(session)
        rows = await repository.fetch_fill_view_rows(owner, None)
        expected = await repository.list_fills_for_view(owner, None)

    assert rows, "the control: the book was read"
    assert all(len(row) == 14 for row in rows), "one value per selected column"
    for row in rows:
        assert all(isinstance(value, Decimal) for value in row[7:10]), row
        assert isinstance(row[11], Decimal), row
        assert isinstance(row[13], datetime), row
        assert row[13].tzinfo is not None, row
    assert decode_fill_view_rows(rows) == expected, "decoded after close, identical"


async def test_the_page_built_in_the_thread_equals_the_one_built_from_the_records(
    factory: async_sessionmaker[AsyncSession], book: tuple[int, int, dict[int, int]]
) -> None:
    """Moving the work changed nothing: the records, filtered and totalled by hand, agree."""
    owner, _intruder, ids = book
    async with factory() as session:
        records = await ExchangeFillRepository(session).list_fills_for_view(
            owner, frozenset({ExchangeKey.BITGET})
        )
    kept = sorted(
        (record for record in records if minute(10) <= record.executed_at < minute(21)),
        key=lambda record: (record.executed_at, record.id),
        reverse=True,
    )

    page = await list_fills(
        factory, owner, exchanges=[ExchangeKey.BITGET], from_=minute(10), to=minute(21), limit=2
    )

    assert [view.id for view in page.fills] == [record.id for record in kept][:2]
    assert page.total_count == len(kept) == 3
    assert numbers(page, ids) == [1004, 1003]


async def test_a_cancelled_request_stops_waiting_while_the_thread_still_runs(
    factory: async_sessionmaker[AsyncSession],
    book: tuple[int, int, dict[int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation propagates at once: the caller is not held until the thread finishes.

    The thread is held on an event the test controls; the request is cancelled while it is
    held, and must raise its cancellation within `CANCEL_BOUND` seconds, with the thread still
    held. The thread holds no session, so letting it finish afterwards is harmless.
    """
    owner, _intruder, _ids = book
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    build_page = exchanges_service._fills_page

    def held_page(*arguments: Any) -> FillsPage:
        entered.set()
        release.wait(timeout=HELD_THREAD_SECONDS)
        try:
            return build_page(*arguments)
        finally:
            finished.set()

    monkeypatch.setattr(exchanges_service, "_fills_page", held_page)
    request = asyncio.create_task(list_fills(factory, owner))
    try:
        await asyncio.wait_for(until(entered.is_set), timeout=CANCEL_BOUND)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=CANCEL_BOUND)
        assert not finished.is_set(), "the cancellation waited for the thread"
    finally:
        release.set()
    await asyncio.wait_for(until(finished.is_set), timeout=HELD_THREAD_SECONDS)


async def until(condition: Callable[[], bool]) -> None:
    """Wait until `condition` holds, polling every 10 ms. Every caller bounds it."""
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)
