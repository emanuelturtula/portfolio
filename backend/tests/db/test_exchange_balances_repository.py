"""`ExchangeBalanceRepository` against a real migrated file: #104's storage rules (spec 025).

Driven through the repository and read back over a **second session**, or with plain SQL, so
what is asserted is what was committed. A repository never commits: each test owns the
transaction, which is how "replace is one change" can be shown by rolling one back.

What must fail if the behaviour is removed, each with a test below named after it:

* **A reading is replaced whole.** An asset the venue no longer lists is gone, not left at the
  amount it last had; an amount that changed is the new one; another account's rows are not
  touched. An empty reading is a reading: no rows, and a fresh `balances_read_at`.
* **A success records itself**: `balances_read_at` is the instant given and `balances_error`
  is cleared.
* **A failure keeps the last good reading.** `record_failure` writes the kind and nothing
  else: the rows and `balances_read_at` stay.
* **The delete and the inserts are one transaction**, the caller's. Rolled back, the old
  reading is intact.
* **`list_for_user` names every account**, read or not, by `exchange_key`, with its rows by
  asset, and only the owner's.
* **Amounts are exact.** A 38-digit quantity and one unit at eighteen places come back as the
  `Decimal`s that went in, and the column holds fixed-point text.
* **No SQL aggregate, comparison or ordering touches `quantity`.** Every statement the
  repository issues is recorded and inspected: a `SUM()` or an `ORDER BY quantity` on a
  `TEXT` money column coerces it to a float.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event, text

from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey
from portfolio.providers.exchanges.base import AssetBalance
from portfolio.repositories.exchange_balances import (
    AccountBalances,
    ExchangeBalanceRepository,
    StoredBalance,
)
from portfolio.repositories.exchange_sync_runs import ExchangeSyncErrorKind
from portfolio.repositories.exchanges import ExchangeAccountRepository
from tests.balance_harness import insert_user
from tests.exchange_sync_harness import (
    BALANCE_STATE_SQL,
    BALANCES_SQL,
    held,
    rows,
    sqlite_timestamp,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CREATED: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
FIRST_READ: Final = datetime(2026, 10, 1, 9, 59, 0, 123456, tzinfo=UTC)
SECOND_READ: Final = datetime(2026, 10, 1, 10, 59, tzinfo=UTC)

LARGEST: Final = "99999999999999999999.999999999999999999"
ONE_UNIT: Final = "0.000000000000000001"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


async def an_account(
    factory: async_sessionmaker[AsyncSession],
    exchange_key: ExchangeKey = ExchangeKey.BITGET,
    *,
    username: str = "owner",
) -> tuple[int, int]:
    """An owner and one account, committed. Returns `(user_id, account_id)`."""
    async with factory() as session:
        existing = await session.scalar(
            text("SELECT id FROM users WHERE username = :name"), {"name": username}
        )
        user_id = existing if existing is not None else await insert_user(session, username)
        account = await ExchangeAccountRepository(session).ensure(
            user_id=user_id, exchange_key=exchange_key, created_at=CREATED
        )
        await session.commit()
    return int(user_id), account.id


async def replace(
    factory: async_sessionmaker[AsyncSession],
    account_id: int,
    balances: Sequence[AssetBalance],
    read_at: datetime = FIRST_READ,
) -> None:
    """One reading through the repository, committed."""
    async with factory() as session:
        await ExchangeBalanceRepository(session).replace(account_id, balances, read_at)
        await session.commit()


async def fail(
    factory: async_sessionmaker[AsyncSession],
    account_id: int,
    kind: ExchangeSyncErrorKind,
) -> None:
    async with factory() as session:
        await ExchangeBalanceRepository(session).record_failure(account_id, kind)
        await session.commit()


async def stored(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str, str]]:
    """Every balance on disk as `(exchange_key, asset, quantity as the column's text)`."""
    return [
        (str(row["exchange_key"]), str(row["asset"]), str(row["quantity"]))
        for row in await rows(factory, BALANCES_SQL)
    ]


async def state(factory: async_sessionmaker[AsyncSession], key: str = "bitget") -> dict[str, Any]:
    (row,) = [row for row in await rows(factory, BALANCE_STATE_SQL) if row["exchange_key"] == key]
    return row


async def listed(
    factory: async_sessionmaker[AsyncSession], user_id: int
) -> tuple[AccountBalances, ...]:
    async with factory() as session:
        return await ExchangeBalanceRepository(session).list_for_user(user_id)


FIRST_READING: Final = (held("BTC", "0.25"), held("KAS", "1500"), held("ETH", "2.5"))


# --------------------------------------------------------------------------------------
# `replace`: a reading is stored, and records its own success
# --------------------------------------------------------------------------------------


async def test_a_reading_is_stored_as_fixed_point_text_at_eighteen_places(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _user, account = await an_account(factory)

    await replace(factory, account, FIRST_READING)

    assert await stored(factory) == [
        ("bitget", "BTC", "0.250000000000000000"),
        ("bitget", "ETH", "2.500000000000000000"),
        ("bitget", "KAS", "1500.000000000000000000"),
    ]


async def test_a_reading_records_when_it_was_read_and_clears_the_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _user, account = await an_account(factory)
    await fail(factory, account, ExchangeSyncErrorKind.UNAVAILABLE)

    await replace(factory, account, FIRST_READING, FIRST_READ)

    found = await state(factory)
    assert found["balances_read_at"] == sqlite_timestamp(FIRST_READ)
    assert found["balances_error"] is None
    assert found["sync_status"] == "never_synced", "a balance read is no part of the fill status"


async def test_the_instant_is_stored_in_utc_whatever_offset_it_was_given_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user, account = await an_account(factory)
    elsewhere = FIRST_READ.astimezone(timezone(timedelta(hours=-3)))

    await replace(factory, account, (), elsewhere)

    assert (await state(factory))["balances_read_at"] == sqlite_timestamp(FIRST_READ)
    (found,) = await listed(factory, user)
    assert found.balances_read_at == FIRST_READ
    assert found.balances_read_at is not None
    assert found.balances_read_at.utcoffset() == timedelta(0)


async def test_replace_does_not_commit(factory: async_sessionmaker[AsyncSession]) -> None:
    """The caller's transaction is the unit: nothing is on disk until the caller says so."""
    _user, account = await an_account(factory)

    async with factory() as session:
        await ExchangeBalanceRepository(session).replace(account, FIRST_READING, FIRST_READ)
        assert await stored(factory) == [], "a second connection saw an uncommitted reading"
        assert (await state(factory))["balances_read_at"] is None
        await session.rollback()

    assert await stored(factory) == []
    assert (await state(factory))["balances_read_at"] is None


# --------------------------------------------------------------------------------------
# `replace` is whole
# --------------------------------------------------------------------------------------


async def test_a_second_reading_replaces_the_first_whole(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """KAS is gone, BTC changed, ETH unchanged, BGB new: the rows are the second answer."""
    _user, account = await an_account(factory)
    await replace(factory, account, FIRST_READING, FIRST_READ)

    await replace(
        factory,
        account,
        (held("BTC", "0.75"), held("ETH", "2.5"), held("BGB", "40")),
        SECOND_READ,
    )

    assert await stored(factory) == [
        ("bitget", "BGB", "40.000000000000000000"),
        ("bitget", "BTC", "0.750000000000000000"),
        ("bitget", "ETH", "2.500000000000000000"),
    ]
    assert (await state(factory))["balances_read_at"] == sqlite_timestamp(SECOND_READ)


async def test_an_empty_reading_is_a_reading(factory: async_sessionmaker[AsyncSession]) -> None:
    """The spot account holds nothing: no rows, and a `balances_read_at` that says it was read."""
    user, account = await an_account(factory)
    await replace(factory, account, FIRST_READING, FIRST_READ)
    await fail(factory, account, ExchangeSyncErrorKind.SCHEMA)

    await replace(factory, account, (), SECOND_READ)

    assert await stored(factory) == []
    found = await state(factory)
    assert found["balances_read_at"] == sqlite_timestamp(SECOND_READ)
    assert found["balances_error"] is None
    assert await listed(factory, user) == (
        AccountBalances(
            exchange_key=ExchangeKey.BITGET,
            sync_status=AccountSyncStatus.NEVER_SYNCED,
            balances_read_at=SECOND_READ,
            balances_error=None,
            balances=(),
        ),
    )


async def test_replacing_one_accounts_reading_leaves_the_other_accounts_alone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _user, bitget = await an_account(factory, ExchangeKey.BITGET)
    _user, bingx = await an_account(factory, ExchangeKey.BINGX)
    await replace(factory, bingx, (held("BTC", "0.1"), held("KAS", "7")), FIRST_READ)
    await fail(factory, bingx, ExchangeSyncErrorKind.RATE_LIMITED)
    await replace(factory, bitget, FIRST_READING, FIRST_READ)

    await replace(factory, bitget, (held("BTC", "9"),), SECOND_READ)

    assert await stored(factory) == [
        ("bingx", "BTC", "0.100000000000000000"),
        ("bingx", "KAS", "7.000000000000000000"),
        ("bitget", "BTC", "9.000000000000000000"),
    ]
    other = await state(factory, "bingx")
    assert other["balances_read_at"] == sqlite_timestamp(FIRST_READ)
    assert other["balances_error"] == "rate_limited"


async def test_the_delete_and_the_inserts_are_one_transaction(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rolled back after `replace`, the first reading is intact: nothing was half-replaced."""
    _user, account = await an_account(factory)
    await replace(factory, account, FIRST_READING, FIRST_READ)
    before = await stored(factory)

    async with factory() as session:
        await ExchangeBalanceRepository(session).replace(account, (held("BTC", "9"),), SECOND_READ)
        await session.rollback()

    assert await stored(factory) == before
    assert (await state(factory))["balances_read_at"] == sqlite_timestamp(FIRST_READ)


async def test_a_reading_the_table_refuses_leaves_the_last_one_after_a_rollback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An asset named twice breaks the unique constraint *after* the delete has run.

    `assemble_balances` refuses such a reading before it gets here; this is the storage
    side's own answer, and what the sync relies on when it rolls back a failed write.
    """
    _user, account = await an_account(factory)
    await replace(factory, account, FIRST_READING, FIRST_READ)
    before = await stored(factory)

    async with factory() as session:
        with pytest.raises(Exception, match="UNIQUE constraint failed") as caught:
            await ExchangeBalanceRepository(session).replace(
                account, (held("ZZMARKED", "4242.4242"), held("ZZMARKED", "1")), SECOND_READ
            )
        await session.rollback()

    assert "4242" not in str(caught.value), "the engine hides statement parameters"
    assert "ZZMARKED" not in str(caught.value)
    assert await stored(factory) == before
    assert (await state(factory))["balances_read_at"] == sqlite_timestamp(FIRST_READ)


# --------------------------------------------------------------------------------------
# `record_failure`: the kind, and nothing else
# --------------------------------------------------------------------------------------


async def test_a_failure_keeps_the_rows_and_when_they_were_read(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An old reading that says how old it is, is information."""
    user, account = await an_account(factory)
    await replace(factory, account, FIRST_READING, FIRST_READ)
    before = await stored(factory)

    await fail(factory, account, ExchangeSyncErrorKind.UNAVAILABLE)

    assert await stored(factory) == before
    found = await state(factory)
    assert found["balances_read_at"] == sqlite_timestamp(FIRST_READ)
    assert found["balances_error"] == "unavailable"
    (account_balances,) = await listed(factory, user)
    assert account_balances.balances_read_at == FIRST_READ
    assert account_balances.balances_error is ExchangeSyncErrorKind.UNAVAILABLE
    assert [balance.asset for balance in account_balances.balances] == ["BTC", "ETH", "KAS"]


async def test_a_failure_before_any_reading_has_an_error_and_no_reading(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user, account = await an_account(factory)

    await fail(factory, account, ExchangeSyncErrorKind.AUTH)

    assert await listed(factory, user) == (
        AccountBalances(
            exchange_key=ExchangeKey.BITGET,
            sync_status=AccountSyncStatus.NEVER_SYNCED,
            balances_read_at=None,
            balances_error=ExchangeSyncErrorKind.AUTH,
            balances=(),
        ),
    )


@pytest.mark.parametrize("kind", list(ExchangeSyncErrorKind))
async def test_every_kind_is_stored_as_its_own_value(
    factory: async_sessionmaker[AsyncSession], kind: ExchangeSyncErrorKind
) -> None:
    user, account = await an_account(factory)

    await fail(factory, account, kind)

    assert (await state(factory))["balances_error"] == kind.value
    (found,) = await listed(factory, user)
    assert found.balances_error is kind


async def test_a_later_failure_replaces_the_kind_and_a_later_success_clears_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _user, account = await an_account(factory)
    await fail(factory, account, ExchangeSyncErrorKind.AUTH)

    await fail(factory, account, ExchangeSyncErrorKind.RATE_LIMITED)
    assert (await state(factory))["balances_error"] == "rate_limited"

    await replace(factory, account, FIRST_READING, FIRST_READ)
    assert (await state(factory))["balances_error"] is None


async def test_a_failure_touches_only_its_own_account_and_only_one_column(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _user, bitget = await an_account(factory, ExchangeKey.BITGET)
    _user, bingx = await an_account(factory, ExchangeKey.BINGX)
    await replace(factory, bingx, (held("KAS", "7"),), FIRST_READ)
    before = await rows(factory, "SELECT * FROM exchange_accounts ORDER BY id")

    await fail(factory, bitget, ExchangeSyncErrorKind.SCHEMA)

    after = await rows(factory, "SELECT * FROM exchange_accounts ORDER BY id")
    changed = [
        (row["exchange_key"], name)
        for row, was in zip(after, before, strict=True)
        for name in row
        if row[name] != was[name]
    ]
    assert changed == [("bitget", "balances_error")]


async def test_record_failure_does_not_commit(factory: async_sessionmaker[AsyncSession]) -> None:
    _user, account = await an_account(factory)

    async with factory() as session:
        await ExchangeBalanceRepository(session).record_failure(
            account, ExchangeSyncErrorKind.INTERNAL
        )
        assert (await state(factory))["balances_error"] is None
        await session.rollback()

    assert (await state(factory))["balances_error"] is None


# --------------------------------------------------------------------------------------
# `list_for_user`
# --------------------------------------------------------------------------------------


async def test_every_account_is_listed_by_exchange_key_read_or_not(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bitget is created first and listed second: the order is the key's, not the id's."""
    user, bitget = await an_account(factory, ExchangeKey.BITGET)
    _user, _bingx = await an_account(factory, ExchangeKey.BINGX)
    await replace(factory, bitget, (held("KAS", "1500"), held("BTC", "0.25")), FIRST_READ)

    found = await listed(factory, user)

    assert found == (
        AccountBalances(
            exchange_key=ExchangeKey.BINGX,
            sync_status=AccountSyncStatus.NEVER_SYNCED,
            balances_read_at=None,
            balances_error=None,
            balances=(),
        ),
        AccountBalances(
            exchange_key=ExchangeKey.BITGET,
            sync_status=AccountSyncStatus.NEVER_SYNCED,
            balances_read_at=FIRST_READ,
            balances_error=None,
            balances=(
                StoredBalance(asset="BTC", quantity=Decimal("0.25")),
                StoredBalance(asset="KAS", quantity=Decimal("1500")),
            ),
        ),
    )
    assert isinstance(found, tuple)
    assert all(isinstance(account.exchange_key, ExchangeKey) for account in found)


@pytest.mark.parametrize("status", list(AccountSyncStatus))
async def test_each_account_is_listed_with_where_its_fill_sync_stands(
    factory: async_sessionmaker[AsyncSession], status: AccountSyncStatus
) -> None:
    """R9: balances are read only after a successful fill sync, so the caller has to know
    whether an account's fill sync is still `ok` to know whether its reading is being
    refreshed. This layer reports the status as it is stored and decides nothing from it:
    the rows are listed whatever it says."""
    user, bitget = await an_account(factory, ExchangeKey.BITGET)
    _user, _bingx = await an_account(factory, ExchangeKey.BINGX)
    await replace(factory, bitget, (held("BTC", "0.25"),), FIRST_READ)
    async with factory() as session:
        await ExchangeAccountRepository(session).set_status(bitget, status)
        await session.commit()

    bingx_listed, bitget_listed = await listed(factory, user)

    assert bitget_listed.sync_status is status
    assert bitget_listed.balances == (StoredBalance("BTC", Decimal("0.25")),)
    assert bitget_listed.balances_read_at == FIRST_READ
    assert bingx_listed.sync_status is AccountSyncStatus.NEVER_SYNCED, "the other is untouched"


async def test_each_accounts_rows_are_its_own_and_sorted_by_asset(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same asset at two venues is two rows, one under each, never mixed or merged."""
    user, bitget = await an_account(factory, ExchangeKey.BITGET)
    _user, bingx = await an_account(factory, ExchangeKey.BINGX)
    await replace(
        factory, bitget, (held("ZEC", "3"), held("BTC", "0.25"), held("1INCH", "8")), FIRST_READ
    )
    await replace(factory, bingx, (held("kas", "7"), held("BTC", "0.1")), SECOND_READ)

    by_key = {account.exchange_key: account for account in await listed(factory, user)}

    assert [(b.asset, b.quantity) for b in by_key[ExchangeKey.BITGET].balances] == [
        ("1INCH", Decimal(8)),
        ("BTC", Decimal("0.25")),
        ("ZEC", Decimal(3)),
    ]
    assert [(b.asset, b.quantity) for b in by_key[ExchangeKey.BINGX].balances] == [
        ("BTC", Decimal("0.1")),
        ("kas", Decimal(7)),
    ]


async def test_an_owner_without_an_account_has_nothing_listed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        user = await insert_user(session)

    assert await listed(factory, user) == ()
    assert await listed(factory, user + 4242) == ()


async def test_only_the_owners_accounts_are_listed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scoped by `user_id`: another owner's venue and its balances never appear."""
    owner, account = await an_account(factory, ExchangeKey.BITGET)
    other, others = await an_account(factory, ExchangeKey.BITGET, username="second")
    await replace(factory, account, (held("BTC", "0.25"),), FIRST_READ)
    await replace(factory, others, (held("ZZOTHER", "4242"),), SECOND_READ)

    (mine,) = await listed(factory, owner)
    (theirs,) = await listed(factory, other)

    assert [balance.asset for balance in mine.balances] == ["BTC"]
    assert mine.balances_read_at == FIRST_READ
    assert [balance.asset for balance in theirs.balances] == ["ZZOTHER"]
    assert theirs.balances_read_at == SECOND_READ


async def test_what_is_listed_is_what_is_on_disk_not_what_the_session_remembers(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session that listed once lists the newer reading after another session committed it."""
    user, account = await an_account(factory)
    await replace(factory, account, (held("BTC", "0.25"),), FIRST_READ)

    async with factory() as session:
        repository = ExchangeBalanceRepository(session)
        (first,) = await repository.list_for_user(user)
        await replace(factory, account, (held("BTC", "0.75"),), SECOND_READ)
        await fail(factory, account, ExchangeSyncErrorKind.SCHEMA)
        (second,) = await repository.list_for_user(user)

    assert first.balances == (StoredBalance("BTC", Decimal("0.25")),)
    assert second.balances == (StoredBalance("BTC", Decimal("0.75")),)
    assert second.balances_read_at == SECOND_READ
    assert second.balances_error is ExchangeSyncErrorKind.SCHEMA


# --------------------------------------------------------------------------------------
# Amounts are exact
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param(LARGEST, id="38 digits"),
        pytest.param(ONE_UNIT, id="one unit at eighteen places"),
        pytest.param("12345678901234567890.123456789012345678", id="past a double"),
        pytest.param("0.1", id="a tenth"),
        pytest.param("1E+5", id="an exponent"),
    ],
)
async def test_a_quantity_comes_back_as_the_decimal_that_went_in(
    factory: async_sessionmaker[AsyncSession], quantity: str
) -> None:
    """A double holds about 17 significant digits; these would not survive one."""
    user, account = await an_account(factory)

    await replace(factory, account, (held("KAS", quantity),))

    (found,) = await listed(factory, user)
    (balance,) = found.balances
    assert type(balance.quantity) is Decimal
    assert balance.quantity == Decimal(quantity)
    assert balance.quantity.as_tuple().exponent == -18
    ((_key, _asset, on_disk),) = await stored(factory)
    assert re.fullmatch(r"\d+\.\d{18}", on_disk), on_disk
    assert Decimal(on_disk) == Decimal(quantity)


async def test_the_column_holds_text_and_never_a_number(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _user, account = await an_account(factory)
    await replace(factory, account, (held("KAS", LARGEST), held("BTC", "1")))

    async with factory() as session:
        kinds = (
            await session.execute(text("SELECT DISTINCT typeof(quantity) FROM exchange_balances"))
        ).all()

    assert kinds == [("text",)]


# --------------------------------------------------------------------------------------
# Nothing is aggregated, compared or ordered in SQL on `quantity`
# --------------------------------------------------------------------------------------

AGGREGATE: Final = re.compile(r"\b(SUM|AVG|TOTAL|MIN|MAX|GROUP_CONCAT|COUNT)\s*\(", re.IGNORECASE)
AFTER_THE_SELECT_LIST: Final = re.compile(
    r"\b(WHERE|ORDER\s+BY|GROUP\s+BY|HAVING)\b(.*)", re.IGNORECASE | re.DOTALL
)


def touches_quantity_outside_a_column_list(statement: str) -> bool:
    """Whether `quantity` appears in a `WHERE`, `ORDER BY`, `GROUP BY` or `HAVING` clause."""
    found = AFTER_THE_SELECT_LIST.search(statement)
    return found is not None and "quantity" in found.group(2).lower()


async def test_no_statement_aggregates_compares_or_orders_by_quantity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every statement `replace`, `record_failure` and `list_for_user` issue, inspected.

    `quantity` and `balances_read_at` are text in SQLite: summed, compared or sorted there
    they would be coerced, the first to a float. The sums are the service's, in Python.
    """
    user, account = await an_account(factory)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement)

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        sync_engine = engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", record)
        try:
            repository = ExchangeBalanceRepository(session)
            await repository.replace(account, FIRST_READING, FIRST_READ)
            await repository.record_failure(account, ExchangeSyncErrorKind.SCHEMA)
            await repository.replace(account, (held("BTC", "9"),), SECOND_READ)
            found = await repository.list_for_user(user)
            await session.commit()
        finally:
            event.remove(sync_engine, "before_cursor_execute", record)

    assert found[0].balances == (StoredBalance("BTC", Decimal(9)),)
    about_balances = [statement for statement in statements if "exchange_balances" in statement]
    assert len(about_balances) >= 5, statements
    assert [statement for statement in statements if AGGREGATE.search(statement)] == []
    assert [
        statement for statement in statements if touches_quantity_outside_a_column_list(statement)
    ] == []
    assert [
        statement
        for statement in statements
        if (clause := AFTER_THE_SELECT_LIST.search(statement)) is not None
        and "balances_read_at" in clause.group(2)
    ] == []
    ordered = [statement for statement in statements if "ORDER BY" in statement.upper()]
    assert ordered, "the reads are ordered, by the two text keys"
    for statement in ordered:
        ordering = statement.upper().split("ORDER BY", 1)[1]
        assert "EXCHANGE_KEY" in ordering or "ASSET" in ordering, statement


def test_the_statement_check_can_fail() -> None:
    """The control: each kind of statement the check exists to refuse is refused."""
    assert AGGREGATE.search("SELECT SUM(quantity) FROM exchange_balances")
    assert AGGREGATE.search("SELECT max (quantity) FROM exchange_balances")
    assert touches_quantity_outside_a_column_list(
        "SELECT asset FROM exchange_balances ORDER BY quantity DESC"
    )
    assert touches_quantity_outside_a_column_list(
        "SELECT asset FROM exchange_balances WHERE quantity > '0'"
    )
    assert touches_quantity_outside_a_column_list(
        "DELETE FROM exchange_balances WHERE exchange_balances.quantity = ?"
    )
    assert not touches_quantity_outside_a_column_list(
        "SELECT exchange_balances.asset, exchange_balances.quantity FROM exchange_balances "
        "WHERE exchange_balances.exchange_account_id IN (?) ORDER BY exchange_balances.asset"
    )
    assert not touches_quantity_outside_a_column_list(
        "INSERT INTO exchange_balances (exchange_account_id, asset, quantity) VALUES (?, ?, ?)"
    )


# --------------------------------------------------------------------------------------
# The account's state, as the sync reads it
# --------------------------------------------------------------------------------------


async def test_the_account_state_the_sync_reads_carries_the_two_columns(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`ExchangeAccountState.balances_error` is what decides whether a scheduled run asks."""
    user, account = await an_account(factory)

    async with factory() as session:
        (fresh,) = await ExchangeAccountRepository(session).list_for_user(user)
    await replace(factory, account, FIRST_READING, FIRST_READ)
    await fail(factory, account, ExchangeSyncErrorKind.INSUFFICIENT_SCOPE)
    async with factory() as session:
        (failed,) = await ExchangeAccountRepository(session).list_for_user(user)
        fetched = await ExchangeAccountRepository(session).get(account)

    assert (fresh.balances_read_at, fresh.balances_error) == (None, None)
    assert failed.balances_read_at == FIRST_READ
    assert failed.balances_error is ExchangeSyncErrorKind.INSUFFICIENT_SCOPE
    assert fetched == failed


def test_an_asset_balance_is_a_balance_record() -> None:
    """`replace` takes the provider's own type: `mypy --strict` checks the calls above.

    This is the runtime half: the two attributes the repository reads are the two an
    `AssetBalance` has.
    """
    balance = AssetBalance(asset="BTC", quantity=Decimal("0.25"))

    assert (balance.asset, balance.quantity) == ("BTC", Decimal("0.25"))
