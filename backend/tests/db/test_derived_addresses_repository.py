"""`DerivedAddressRepository` against a real, migrated, file-backed SQLite database (spec 031).

The repository's three promises, each with the failure that would break it:

* `list_for_wallets` answers about every wallet it was asked about and no other, in tree
  order -- an `ORDER BY` that ran over text would put index 10 before index 2;
* `apply` inserts, marks used, and **never** marks unused -- there is no argument that can;
* `apply` flushes and does not commit, so the sync's per-chain commit is the only one, and a
  refused row raises from the flush for the sync to roll back.

Every address here is a test-network form from `tests/extended_key_vectors.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from portfolio.repositories.derived_addresses import (
    DerivedAddressRecord,
    DerivedAddressRepository,
)
from portfolio.repositories.wallets import WalletRepository
from tests.balance_harness import insert_user, sqlite_timestamp
from tests.extended_key_harness import insert_key_wallet
from tests.extended_key_vectors import BIP32_TV1_M, SCAN_CHANGE, SCAN_KEY, SCAN_RECEIVE
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CREATED_AT: Final = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 10, 3, 10, 15, tzinfo=UTC)

ROWS_SQL: Final = (
    "SELECT wallet_id, branch, child_index, address_canonical, used, created_at "
    "FROM derived_addresses ORDER BY wallet_id, branch, child_index"
)


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as factory:
        yield factory


@pytest.fixture
async def two_wallets(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with sessions() as session:
        user_id = await insert_user(session)
        first = await insert_key_wallet(session, user_id=user_id, created_at=CREATED_AT)
        second = await insert_key_wallet(
            session, user_id=user_id, created_at=CREATED_AT, key=BIP32_TV1_M
        )
    return first, second


def record(branch: int, index: int, *, used: bool = False) -> DerivedAddressRecord:
    vectors = SCAN_RECEIVE if branch == 0 else SCAN_CHANGE
    return DerivedAddressRecord(branch=branch, child_index=index, address=vectors[index], used=used)


async def rows(factory: async_sessionmaker[AsyncSession]) -> list[dict[str, object]]:
    async with factory() as session:
        result = await session.execute(text(ROWS_SQL))
        return [dict(row) for row in result.mappings().all()]


async def apply_and_commit(
    factory: async_sessionmaker[AsyncSession],
    wallet_id: int,
    *,
    new: tuple[DerivedAddressRecord, ...] = (),
    newly_used: tuple[tuple[int, int], ...] = (),
    created_at: datetime = CREATED_AT,
) -> None:
    async with factory() as session:
        await DerivedAddressRepository(session).apply(
            wallet_id, new=new, newly_used=newly_used, created_at=created_at
        )
        await session.commit()


# --------------------------------------------------------------------------------------
# list_for_wallets
# --------------------------------------------------------------------------------------


async def test_asking_about_no_wallet_is_an_empty_answer(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, _ = two_wallets
    await apply_and_commit(sessions, first, new=(record(0, 0),))

    async with sessions() as session:
        assert await DerivedAddressRepository(session).list_for_wallets([]) == {}


async def test_every_wallet_asked_about_is_a_key_even_with_nothing_derived(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, second = two_wallets
    await apply_and_commit(sessions, first, new=(record(0, 0, used=True),))

    async with sessions() as session:
        found = await DerivedAddressRepository(session).list_for_wallets([first, second])

    assert found == {first: (record(0, 0, used=True),), second: ()}


async def test_only_the_wallets_asked_about_are_answered(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, second = two_wallets
    await apply_and_commit(sessions, first, new=(record(0, 0),))
    await apply_and_commit(sessions, second, new=(record(0, 1),))

    async with sessions() as session:
        found = await DerivedAddressRepository(session).list_for_wallets([second])

    assert found == {second: (record(0, 1),)}


async def test_a_wallet_that_does_not_exist_is_answered_with_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        assert await DerivedAddressRepository(session).list_for_wallets({404}) == {404: ()}


async def test_records_come_back_by_branch_then_by_numeric_index(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    """Inserted out of order on purpose: change before receive, 44 before 5 before 1."""
    first, _ = two_wallets
    shuffled = (record(1, 22), record(0, 44), record(1, 3), record(0, 5), record(0, 1))
    await apply_and_commit(sessions, first, new=shuffled)

    async with sessions() as session:
        (found,) = (await DerivedAddressRepository(session).list_for_wallets([first])).values()

    assert [(entry.branch, entry.child_index) for entry in found] == [
        (0, 1),
        (0, 5),
        (0, 44),
        (1, 3),
        (1, 22),
    ]


# --------------------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------------------


async def test_new_records_are_stored_whole_with_the_given_instant(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, _ = two_wallets

    await apply_and_commit(sessions, first, new=(record(0, 0, used=True), record(1, 3)))

    assert await rows(sessions) == [
        {
            "wallet_id": first,
            "branch": 0,
            "child_index": 0,
            "address_canonical": SCAN_RECEIVE[0],
            "used": 1,
            "created_at": sqlite_timestamp(CREATED_AT),
        },
        {
            "wallet_id": first,
            "branch": 1,
            "child_index": 3,
            "address_canonical": SCAN_CHANGE[3],
            "used": 0,
            "created_at": sqlite_timestamp(CREATED_AT),
        },
    ]


async def test_newly_used_marks_that_position_of_that_wallet_and_nothing_else(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, second = two_wallets
    await apply_and_commit(sessions, first, new=(record(0, 0), record(0, 1), record(1, 0)))
    await apply_and_commit(sessions, second, new=(record(0, 1),))

    await apply_and_commit(sessions, first, newly_used=((0, 1),), created_at=LATER)

    stored = {
        (row["wallet_id"], row["branch"], row["child_index"]): row for row in await rows(sessions)
    }
    assert {key for key, row in stored.items() if row["used"]} == {(first, 0, 1)}
    assert stored[(first, 0, 1)]["created_at"] == sqlite_timestamp(CREATED_AT), "not re-dated"


async def test_marking_a_used_position_again_keeps_it_used(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, _ = two_wallets
    await apply_and_commit(sessions, first, new=(record(0, 5, used=True),))

    await apply_and_commit(sessions, first, newly_used=((0, 5),))

    (row,) = await rows(sessions)
    assert row["used"] == 1


async def test_no_call_can_mark_a_used_position_unused(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    """R5. The only way to say "unused" is a new record, and a stored position refuses one."""
    first, _ = two_wallets
    await apply_and_commit(sessions, first, new=(record(0, 5, used=True),))

    with pytest.raises(IntegrityError):
        await apply_and_commit(sessions, first, new=(record(0, 5, used=False),))

    (row,) = await rows(sessions)
    assert row["used"] == 1


async def test_a_newly_used_position_that_was_never_stored_creates_nothing(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, _ = two_wallets
    await apply_and_commit(sessions, first, new=(record(0, 0),))

    await apply_and_commit(sessions, first, newly_used=((0, 44),))

    assert [(row["child_index"], row["used"]) for row in await rows(sessions)] == [(0, 0)]


async def test_apply_flushes_and_leaves_the_commit_to_the_caller(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, _ = two_wallets
    await apply_and_commit(sessions, first, new=(record(0, 0),))

    async with sessions() as session:
        repository = DerivedAddressRepository(session)
        await repository.apply(first, new=(record(0, 1),), newly_used=((0, 0),), created_at=LATER)
        # Visible to its own session, so it was flushed...
        (mine,) = (await repository.list_for_wallets([first])).values()
        assert [(entry.child_index, entry.used) for entry in mine] == [(0, True), (1, False)]
        # ...and not committed: the database as anyone else sees it is unchanged.
        assert [(row["child_index"], row["used"]) for row in await rows(sessions)] == [(0, 0)]
        await session.rollback()

    assert [(row["child_index"], row["used"]) for row in await rows(sessions)] == [(0, 0)]


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        pytest.param(DerivedAddressRecord(2, 0, SCAN_RECEIVE[0], False), "branch", id="branch 2"),
        pytest.param(DerivedAddressRecord(0, -1, SCAN_RECEIVE[0], False), "index", id="index -1"),
        pytest.param(
            DerivedAddressRecord(0, 2**31, SCAN_RECEIVE[0], False), "index", id="hardened index"
        ),
    ],
)
async def test_a_record_outside_the_tree_is_refused_by_the_flush(
    sessions: async_sessionmaker[AsyncSession],
    two_wallets: tuple[int, int],
    bad: DerivedAddressRecord,
    reason: str,
) -> None:
    first, _ = two_wallets

    async with sessions() as session:
        with pytest.raises(IntegrityError):
            await DerivedAddressRepository(session).apply(
                first, new=(bad,), newly_used=(), created_at=CREATED_AT
            )
        await session.rollback()

    assert await rows(sessions) == [], reason


async def test_the_same_position_twice_in_one_call_is_refused(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    first, _ = two_wallets

    with pytest.raises(IntegrityError):
        await apply_and_commit(sessions, first, new=(record(0, 0), record(0, 0, used=True)))

    assert await rows(sessions) == []


async def test_a_record_for_a_wallet_that_does_not_exist_is_refused(
    sessions: async_sessionmaker[AsyncSession], two_wallets: tuple[int, int]
) -> None:
    del two_wallets

    with pytest.raises(IntegrityError):
        await apply_and_commit(sessions, 404, new=(record(0, 0),))


# --------------------------------------------------------------------------------------
# The wallet's kind, as the wallet repository stores it
# --------------------------------------------------------------------------------------


async def test_a_wallet_is_an_address_unless_it_is_added_as_a_key(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        user_id = await insert_user(session)
        repository = WalletRepository(session)
        address = await repository.add(
            user_id=user_id,
            chain_key="bitcoin",
            address_canonical=SCAN_RECEIVE[0],
            address_display=SCAN_RECEIVE[0],
            label=None,
            created_at=CREATED_AT,
        )
        key = await repository.add(
            user_id=user_id,
            chain_key="bitcoin",
            address_canonical=SCAN_KEY,
            address_display=SCAN_KEY,
            label=None,
            created_at=CREATED_AT,
            kind="extended_key",
        )
        await session.commit()
        assert (address.kind, key.kind) == ("address", "extended_key")

    async with sessions() as session:
        result = await session.execute(text("SELECT id, kind FROM wallets ORDER BY id"))
        assert [tuple(row) for row in result.all()] == [
            (address.id, "address"),
            (key.id, "extended_key"),
        ]
