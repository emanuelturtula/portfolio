"""Criterion 2 at the repository: idempotency is the constraint, and a revision is a conflict.

`ExchangeFillRepository.insert_page` is `INSERT ... ON CONFLICT DO NOTHING RETURNING` on
`uq_exchange_fills_account_trade`, and nothing else decides whether a fill is new. So the
tests here drive the real statement against a real migrated file -- never `:memory:`, never
`create_all` -- and read the result back over a **second session**, which sees only what
was committed. A repository never commits; the caller's transaction is the unit.

**A same-id fill that differs is a conflict, on every accounting field.** The collision check
compares twelve fields, and a field dropped from it is a revised trade silently kept in its
first version. So the refusal is parametrised over all twelve, each with the smallest
difference that field can carry -- an amount at its eighteenth decimal place, an instant
one millisecond later. `raw_payload` is the one field that is **not** compared, and that is
asserted too: a venue adding a key to its response must not stall every overlap re-read.

The account, window and run-log repositories are driven here as well, for what the service
tests cannot see from outside: that `ensure` is idempotent, that `set_since` forgets the
cursor, that `latest_attempted_outcome` skips a skipped outcome.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import text

from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey, FillSide
from portfolio.repositories.exchange_sync_runs import (
    AccountOutcome,
    AccountOutcomeStatus,
    ExchangeSyncErrorKind,
    ExchangeSyncRunRepository,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.repositories.exchanges import (
    ExchangeAccountRepository,
    ExchangeFillRepository,
    ExchangeSyncWindowRepository,
    FillConflictError,
    FillInsertResult,
)
from tests.balance_harness import insert_user
from tests.exchange_sync_harness import (
    ACCOUNTS_SQL,
    FILLS_SQL,
    OUTCOMES_SQL,
    RUNS_SQL,
    WINDOWS_SQL,
    changed,
    make_fill,
    rows,
    sqlite_timestamp,
    trade_ids,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import NormalizedFill

EXECUTED: Final = datetime(2026, 9, 20, 8, 30, 15, 250000, tzinfo=UTC)
INGESTED: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

#: Distinctive, so their absence from a message means something.
MARKED_TRADE_ID: Final = 7_777_777_771
MARKED_ORDER_ID: Final = "8888888881"
MARKED_QUANTITY: Final = "0.123456789123456789"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


async def an_account(
    factory: async_sessionmaker[AsyncSession],
    exchange_key: ExchangeKey = ExchangeKey.BITGET,
    *,
    username: str = "owner",
) -> int:
    """An owner and one account, committed, through the repository under test."""
    async with factory() as session:
        existing = await session.scalar(
            text("SELECT id FROM users WHERE username = :name"), {"name": username}
        )
        user_id = existing if existing is not None else await insert_user(session, username)
        account = await ExchangeAccountRepository(session).ensure(
            user_id=user_id, exchange_key=exchange_key, created_at=INGESTED
        )
        await session.commit()
    return account.id


async def insert(
    factory: async_sessionmaker[AsyncSession],
    account_id: int,
    fills: list[NormalizedFill],
) -> FillInsertResult:
    """One page through the repository, committed."""
    async with factory() as session:
        result = await ExchangeFillRepository(session).insert_page(
            account_id, fills, ingested_at=INGESTED
        )
        await session.commit()
    return result


def a_fill(trade_id: int = 1001, **overrides: object) -> NormalizedFill:
    return changed(make_fill(trade_id, EXECUTED), **overrides)


# --------------------------------------------------------------------------------------
# Idempotency is the constraint
# --------------------------------------------------------------------------------------


async def test_inserting_a_page_twice_inserts_nothing_the_second_time(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    account = await an_account(factory)
    page = [a_fill(1001), a_fill(1002), a_fill(1003)]

    first = await insert(factory, account, page)
    second = await insert(factory, account, page)

    assert first == FillInsertResult(seen=3, inserted=3)
    assert second == FillInsertResult(seen=3, inserted=0)
    assert await trade_ids(factory) == ["1001", "1002", "1003"]


async def test_a_page_of_new_and_stored_fills_counts_both(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`seen` is the page, `inserted` is what the constraint let through."""
    account = await an_account(factory)
    await insert(factory, account, [a_fill(1001), a_fill(1002)])

    result = await insert(factory, account, [a_fill(1001), a_fill(1003), a_fill(1002)])

    assert result == FillInsertResult(seen=3, inserted=1)
    assert sorted(await trade_ids(factory)) == ["1001", "1002", "1003"]


async def test_an_empty_page_is_nothing_seen_and_nothing_inserted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A venue's empty page is the ordinary case, and `VALUES ()` is not valid SQL."""
    account = await an_account(factory)

    assert await insert(factory, account, []) == FillInsertResult(seen=0, inserted=0)
    assert await trade_ids(factory) == []


async def test_a_full_bitget_page_is_one_statement_that_fits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A hundred fills, Bitget's page size: sixteen hundred bound parameters in one insert."""
    account = await an_account(factory)
    page = [a_fill(10_000 + index) for index in range(100)]

    assert await insert(factory, account, page) == FillInsertResult(seen=100, inserted=100)
    assert await insert(factory, account, page) == FillInsertResult(seen=100, inserted=0)


async def test_a_page_larger_than_one_statement_is_inserted_in_chunks_with_nothing_lost(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """1001 fills: two full statements and one of a single row, every row stored once.

    A venue with a larger page than Bitget's goes through the chunking loop, and a slice off
    by one at a chunk boundary would lose a fill -- or insert one twice and be refused by the
    constraint -- without any other test noticing. A revision in the last chunk is still
    found.
    """
    account = await an_account(factory)
    page = [a_fill(20_000 + index) for index in range(1001)]

    assert await insert(factory, account, page) == FillInsertResult(seen=1001, inserted=1001)
    assert len(set(await trade_ids(factory))) == 1001
    assert await insert(factory, account, page) == FillInsertResult(seen=1001, inserted=0)

    revised = [*page[:-1], changed(page[-1], side=FillSide.SELL)]
    async with factory() as session:
        with pytest.raises(FillConflictError) as caught:
            await ExchangeFillRepository(session).insert_page(
                account, revised, ingested_at=INGESTED
            )
        await session.rollback()
    assert caught.value.conflicts == 1


async def test_the_same_trade_id_on_another_account_is_a_different_fill(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The constraint is per account: two venues can hand out the same number."""
    bitget = await an_account(factory, ExchangeKey.BITGET)
    bingx = await an_account(factory, ExchangeKey.BINGX)
    await insert(factory, bitget, [a_fill(1001)])

    result = await insert(factory, bingx, [a_fill(1001, quantity=Decimal("9"))])

    assert result == FillInsertResult(seen=1, inserted=1)
    stored = await rows(factory, FILLS_SQL)
    assert sorted(row["exchange_account_id"] for row in stored) == sorted([bitget, bingx])


async def test_the_repository_does_not_commit(factory: async_sessionmaker[AsyncSession]) -> None:
    """The caller commits the fills and the cursor together; the repository must not."""
    account = await an_account(factory)

    async with factory() as session:
        result = await ExchangeFillRepository(session).insert_page(
            account, [a_fill(1001)], ingested_at=INGESTED
        )
        assert result.inserted == 1
        assert await trade_ids(factory) == [], "insert_page committed on its own"
        await session.rollback()

    assert await trade_ids(factory) == []


async def test_every_column_is_stored_as_the_fill_carried_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    account = await an_account(factory)
    fill = a_fill(
        MARKED_TRADE_ID,
        external_order_id=MARKED_ORDER_ID,
        quantity=Decimal(MARKED_QUANTITY),
        fee_amount=Decimal("-0.000000000000000001"),
    )

    await insert(factory, account, [fill])

    (row,) = await rows(factory, FILLS_SQL)
    assert row["external_trade_id"] == str(MARKED_TRADE_ID)
    assert row["external_order_id"] == MARKED_ORDER_ID
    assert Decimal(row["quantity"]) == Decimal(MARKED_QUANTITY)
    assert Decimal(row["fee_amount"]) == Decimal("-0.000000000000000001")
    assert row["side"] == "buy"
    assert row["executed_at"] == sqlite_timestamp(EXECUTED)
    assert row["ingested_at"] == sqlite_timestamp(INGESTED)
    assert row["raw_payload"] == fill.raw_payload


# --------------------------------------------------------------------------------------
# A revised trade is a conflict
# --------------------------------------------------------------------------------------

#: One revision per accounting field, each the smallest difference the field can carry.
REVISIONS: Final[dict[str, dict[str, object]]] = {
    "external_order_id": {"external_order_id": "92002"},
    "external_order_id absent": {"external_order_id": None},
    "symbol": {"symbol": "ETHUSDT"},
    "base_asset": {"base_asset": "ETH"},
    "quote_asset": {"quote_asset": "USDC"},
    "side": {"side": FillSide.SELL},
    "quantity": {"quantity": Decimal("0.5") + Decimal("1e-18")},
    "price": {"price": Decimal("60000.000000000000000001")},
    "quote_quantity": {"quote_quantity": Decimal("30000.000000000000000001")},
    "quote_quantity_derived": {"quote_quantity_derived": True},
    "fee_amount": {"fee_amount": Decimal("0.000500000000000001")},
    "fee_asset": {"fee_asset": "USDT"},
    "fee_asset absent": {"fee_asset": None},
    "executed_at": {"executed_at": EXECUTED + timedelta(milliseconds=1)},
}

#: What the stored fill looks like before a revision, where the default would not let the
#: revision differ in one field alone: a `None` fee asset needs a zero fee on both sides.
ORIGINALS: Final[dict[str, dict[str, object]]] = {
    "fee_asset absent": {"fee_amount": Decimal(0)},
}


def test_the_revisions_cover_every_compared_field() -> None:
    """The twelve fields the spec lists, and `raw_payload` deliberately not among them."""
    compared = {
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
    touched = {name for revision in REVISIONS.values() for name in revision}
    every_field = {field.name for field in dataclass_fields(make_fill(1, EXECUTED))}

    assert compared <= touched
    assert every_field - touched == {"external_trade_id", "raw_payload"}


@pytest.mark.parametrize("revision", list(REVISIONS), ids=list(REVISIONS))
async def test_a_same_id_fill_that_differs_raises_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession], revision: str
) -> None:
    """Refused, and the page's *new* fill is not left behind once the caller rolls back.

    The page carries the revision and an unrelated new fill. `DO NOTHING` has already
    inserted the new one when the comparison fails, so "writes nothing" is a property of
    the transaction, not of the statement: nothing reaches a second session before the
    rollback, and nothing is there after it. The stored row keeps its first version.
    """
    account = await an_account(factory)
    original = a_fill(1001, **ORIGINALS.get(revision, {}))
    await insert(factory, account, [original])
    before = await rows(factory, FILLS_SQL)

    async with factory() as session:
        with pytest.raises(FillConflictError) as caught:
            await ExchangeFillRepository(session).insert_page(
                account,
                [a_fill(1002), changed(original, **REVISIONS[revision])],
                ingested_at=INGESTED + timedelta(hours=1),
            )
        assert await rows(factory, FILLS_SQL) == before, "something was committed"
        await session.rollback()

    assert caught.value.conflicts == 1
    assert await rows(factory, FILLS_SQL) == before


async def test_a_same_id_fill_whose_payload_alone_differs_is_not_a_conflict(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A venue that adds a field to its response re-reads its overlap without a stall.

    The first payload is the one kept: nothing is updated, and the database would refuse it.
    """
    account = await an_account(factory)
    original = a_fill(1001, raw_payload='{"tradeId":"1001"}')
    await insert(factory, account, [original])

    result = await insert(
        factory, account, [changed(original, raw_payload='{"newField":"x","tradeId":"1001"}')]
    )

    assert result == FillInsertResult(seen=1, inserted=0)
    (row,) = await rows(factory, FILLS_SQL)
    assert row["raw_payload"] == '{"tradeId":"1001"}'


async def test_trailing_zeros_are_not_a_difference(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The column pads to eighteen places; `0.5` and `0.500000` are one amount."""
    account = await an_account(factory)
    await insert(factory, account, [a_fill(1001)])

    result = await insert(
        factory,
        account,
        [
            a_fill(
                1001,
                quantity=Decimal("0.500000"),
                price=Decimal("60000.00"),
                quote_quantity=Decimal("30000.000000000000000000"),
                fee_amount=Decimal("0.00050"),
            )
        ],
    )

    assert result == FillInsertResult(seen=1, inserted=0)


async def test_the_same_instant_in_another_offset_is_not_a_difference(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    account = await an_account(factory)
    await insert(factory, account, [a_fill(1001)])
    elsewhere = EXECUTED.astimezone(timezone(timedelta(hours=-3)))

    result = await insert(factory, account, [a_fill(1001, executed_at=elsewhere)])

    assert result == FillInsertResult(seen=1, inserted=0)


async def test_the_conflict_message_states_a_count_and_never_an_id_or_an_amount(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The message reaches `detail`, which an endpoint serves: counts only."""
    account = await an_account(factory)
    first = a_fill(
        MARKED_TRADE_ID, external_order_id=MARKED_ORDER_ID, quantity=Decimal(MARKED_QUANTITY)
    )
    second = a_fill(MARKED_TRADE_ID + 1)
    await insert(factory, account, [first, second])

    async with factory() as session:
        with pytest.raises(FillConflictError) as caught:
            await ExchangeFillRepository(session).insert_page(
                account,
                [changed(first, side=FillSide.SELL), changed(second, price=Decimal("1"))],
                ingested_at=INGESTED,
            )
        await session.rollback()

    message = f"{caught.value}{caught.value!r}"
    assert caught.value.conflicts == 2
    assert "2" in str(caught.value)
    for forbidden in (str(MARKED_TRADE_ID), str(MARKED_TRADE_ID + 1), MARKED_ORDER_ID):
        assert forbidden not in message
    assert MARKED_QUANTITY not in message
    assert "BTCUSDT" not in message


# --------------------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------------------


async def test_ensuring_an_account_twice_returns_the_same_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await an_account(factory)
    second = await an_account(factory)

    assert first == second
    (account,) = await rows(factory, ACCOUNTS_SQL)
    assert account["sync_status"] == "never_synced"
    assert account["requested_since"] is None
    assert account["effective_since"] is None
    assert account["planned_until"] is None
    assert account["last_synced_at"] is None


async def test_the_account_state_round_trips_every_column(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await an_account(factory)
    requested = datetime(2009, 1, 3, tzinfo=UTC)
    effective = datetime(2026, 6, 27, 12, 5, tzinfo=UTC)
    planned = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    synced = datetime(2026, 9, 25, 12, 0, 4, tzinfo=UTC)

    async with factory() as session:
        repository = ExchangeAccountRepository(session)
        await repository.set_history(
            account_id, requested_since=requested, effective_since=effective, planned_until=planned
        )
        await repository.set_status(account_id, AccountSyncStatus.AUTH_FAILED)
        await session.commit()
    async with factory() as session:
        state = await ExchangeAccountRepository(session).get(account_id)
    assert state is not None
    assert state.sync_status is AccountSyncStatus.AUTH_FAILED
    assert (state.requested_since, state.effective_since, state.planned_until) == (
        requested,
        effective,
        planned,
    )

    async with factory() as session:
        repository = ExchangeAccountRepository(session)
        await repository.set_effective_since(account_id, effective + timedelta(days=1))
        await repository.mark_synced(account_id, synced_at=synced)
        await session.commit()
    async with factory() as session:
        state = await ExchangeAccountRepository(session).get(account_id)
    assert state is not None
    assert state.sync_status is AccountSyncStatus.OK
    assert state.last_synced_at == synced
    assert state.effective_since == effective + timedelta(days=1)
    assert state.exchange_key is ExchangeKey.BITGET


async def test_accounts_are_listed_per_owner_by_exchange_key(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    bitget = await an_account(factory, ExchangeKey.BITGET)
    bingx = await an_account(factory, ExchangeKey.BINGX)
    await an_account(factory, ExchangeKey.BITGET, username="someone-else")

    async with factory() as session:
        owner = await session.scalar(text("SELECT id FROM users WHERE username = 'owner'"))
        listed = await ExchangeAccountRepository(session).list_for_user(owner)
        missing = await ExchangeAccountRepository(session).get(10_000)

    assert [account.id for account in listed] == [bingx, bitget]
    assert missing is None


# --------------------------------------------------------------------------------------
# The pending-window queue
# --------------------------------------------------------------------------------------


async def test_a_window_is_added_advanced_moved_and_deleted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The checkpoint's whole life: `NULL` cursor, a cursor, a moved `since`, gone."""
    account = await an_account(factory)
    since = datetime(2026, 9, 1, tzinfo=UTC)
    until = datetime(2026, 9, 8, tzinfo=UTC)

    async with factory() as session:
        repository = ExchangeSyncWindowRepository(session)
        window_id = await repository.add(account, since=since, until=until, symbol="BTCUSDT")
        await session.commit()
    (row,) = await rows(factory, WINDOWS_SQL)
    assert (row["id"], row["symbol"], row["cursor"]) == (window_id, "BTCUSDT", None)
    assert (row["since"], row["until"]) == (sqlite_timestamp(since), sqlite_timestamp(until))

    async with factory() as session:
        await ExchangeSyncWindowRepository(session).advance(window_id, "12345")
        await session.commit()
    assert [row["cursor"] for row in await rows(factory, WINDOWS_SQL)] == ["12345"]

    async with factory() as session:
        repository = ExchangeSyncWindowRepository(session)
        await repository.set_since(window_id, since + timedelta(hours=6), keep_cursor=True)
        await session.commit()
        listed = await repository.list_for_account(account)
    assert [(window.id, window.since, window.cursor) for window in listed] == [
        (window_id, since + timedelta(hours=6), "12345")
    ], "a trade-id cursor is kept when asked: the row moves in place"

    async with factory() as session:
        repository = ExchangeSyncWindowRepository(session)
        await repository.set_since(window_id, since + timedelta(days=1))
        await session.commit()
        listed = await repository.list_for_account(account)
    assert [(window.since, window.cursor) for window in listed] == [
        (since + timedelta(days=1), None)
    ], "by default moving since keeps no cursor: the range it paged through has changed"

    async with factory() as session:
        repository = ExchangeSyncWindowRepository(session)
        assert await repository.count_for_account(account) == 1
        await repository.delete(window_id)
        await session.commit()
        assert await repository.count_for_account(account) == 0
    assert await rows(factory, WINDOWS_SQL) == []


async def test_windows_belong_to_their_account(factory: async_sessionmaker[AsyncSession]) -> None:
    bitget = await an_account(factory, ExchangeKey.BITGET)
    bingx = await an_account(factory, ExchangeKey.BINGX)
    since = datetime(2026, 9, 1, tzinfo=UTC)

    async with factory() as session:
        repository = ExchangeSyncWindowRepository(session)
        await repository.add(bitget, since=since, until=since + timedelta(days=1), symbol=None)
        await repository.add(bingx, since=since, until=since + timedelta(days=2), symbol=None)
        await session.commit()
        bitget_windows = await repository.list_for_account(bitget)

    assert [window.until for window in bitget_windows] == [since + timedelta(days=1)]


# --------------------------------------------------------------------------------------
# The run log
# --------------------------------------------------------------------------------------


def outcome(
    account_id: int,
    status: AccountOutcomeStatus,
    *,
    exchange_key: ExchangeKey = ExchangeKey.BITGET,
    kind: ExchangeSyncErrorKind | None = None,
    seen: int = 0,
    inserted: int = 0,
) -> AccountOutcome:
    return AccountOutcome(
        exchange_account_id=account_id,
        exchange_key=exchange_key,
        status=status,
        windows_completed=1,
        pages=2,
        fills_seen=seen,
        fills_inserted=inserted,
        error_kind=kind,
        detail=None if kind is None else "a detail",
    )


async def test_a_run_is_opened_recorded_finished_and_listed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    bitget = await an_account(factory, ExchangeKey.BITGET)
    bingx = await an_account(factory, ExchangeKey.BINGX)
    started = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

    async with factory() as session:
        runs = ExchangeSyncRunRepository(session)
        run_id = await runs.open_run(
            trigger=SyncTrigger.MANUAL, started_at=started, accounts_total=2
        )
        await session.commit()
    (opened,) = await rows(factory, RUNS_SQL)
    assert opened["status"] == "running"
    assert opened["finished_at"] is None

    async with factory() as session:
        runs = ExchangeSyncRunRepository(session)
        await runs.record_outcome(
            run_id, outcome(bitget, AccountOutcomeStatus.SUCCESS, seen=3, inserted=1)
        )
        await runs.record_outcome(
            run_id,
            outcome(
                bingx,
                AccountOutcomeStatus.FAILED,
                exchange_key=ExchangeKey.BINGX,
                kind=ExchangeSyncErrorKind.UNAVAILABLE,
                seen=4,
                inserted=4,
            ),
        )
        await runs.finish_run(
            run_id,
            status=SyncRunStatus.PARTIAL,
            finished_at=started + timedelta(seconds=2),
            duration_ms=1840,
            accounts_total=2,
            accounts_succeeded=1,
            accounts_failed=1,
            accounts_skipped=0,
        )
        await session.commit()
        (summary,) = await runs.list_runs(limit=20)

    assert summary.run_id == run_id
    assert summary.status is SyncRunStatus.PARTIAL
    assert summary.trigger is SyncTrigger.MANUAL
    assert summary.duration_ms == 1840
    assert [account.exchange_key for account in summary.accounts] == [
        ExchangeKey.BINGX,
        ExchangeKey.BITGET,
    ]
    assert (summary.fills_seen, summary.fills_inserted) == (7, 5)
    assert summary.accounts[0].error_kind is ExchangeSyncErrorKind.UNAVAILABLE
    stored = await rows(factory, OUTCOMES_SQL)
    assert [row["status"] for row in stored] == ["failed", "success"]


async def test_runs_are_listed_newest_first_and_limited(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    started = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    async with factory() as session:
        runs = ExchangeSyncRunRepository(session)
        # Started out of order on purpose: the list is by id, never by a TEXT datetime.
        ids = [
            await runs.open_run(
                trigger=SyncTrigger.SCHEDULED,
                started_at=started - timedelta(hours=offset),
                accounts_total=0,
            )
            for offset in (0, 5, 1)
        ]
        await session.commit()
        listed = await runs.list_runs(limit=2)
        latest = await runs.latest_started_at()

    assert [summary.run_id for summary in listed] == [ids[2], ids[1]]
    assert latest == started - timedelta(hours=1), "the newest by id, not the latest instant"


async def test_the_sweep_marks_only_running_runs_and_counts_them(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    started = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    async with factory() as session:
        runs = ExchangeSyncRunRepository(session)
        assert await runs.sweep_interrupted() == 0
        done = await runs.open_run(
            trigger=SyncTrigger.STARTUP, started_at=started, accounts_total=0
        )
        await runs.finish_run(
            done,
            status=SyncRunStatus.SUCCESS,
            finished_at=started,
            duration_ms=1,
            accounts_total=0,
            accounts_succeeded=0,
            accounts_failed=0,
            accounts_skipped=0,
        )
        for _ in range(2):
            await runs.open_run(trigger=SyncTrigger.SCHEDULED, started_at=started, accounts_total=1)
        await session.commit()
        assert await runs.sweep_interrupted() == 2
        await session.commit()

    statuses = [row["status"] for row in await rows(factory, RUNS_SQL)]
    assert statuses == ["success", "interrupted", "interrupted"]
    assert [row["finished_at"] for row in await rows(factory, RUNS_SQL)][1:] == [None, None]


async def test_the_latest_attempted_outcome_skips_a_skipped_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`last_error` reads this: a skipped run says nothing about the key."""
    account = await an_account(factory)
    started = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    async with factory() as session:
        runs = ExchangeSyncRunRepository(session)
        assert await runs.latest_attempted_outcome(account) is None
        failed_run = await runs.open_run(
            trigger=SyncTrigger.SCHEDULED, started_at=started, accounts_total=1
        )
        await runs.record_outcome(
            failed_run,
            outcome(account, AccountOutcomeStatus.FAILED, kind=ExchangeSyncErrorKind.AUTH),
        )
        skipped_run = await runs.open_run(
            trigger=SyncTrigger.SCHEDULED, started_at=started, accounts_total=1
        )
        await runs.record_outcome(skipped_run, outcome(account, AccountOutcomeStatus.SKIPPED))
        await session.commit()
        latest = await runs.latest_attempted_outcome(account)

    assert latest is not None
    assert latest.status is AccountOutcomeStatus.FAILED
    assert latest.error_kind is ExchangeSyncErrorKind.AUTH
