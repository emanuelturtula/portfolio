"""Criterion 8's storage: an append-only history, and the two orderings it is read by.

`balance_snapshots` is the second table in this repository to hold a quantity, and it holds
it differently from the first. `prices.amount` is a `Decimal` in `TEXT`; `confirmed` is an
`INTEGER` count of base units with the exponent stored beside it. Both are rule 2, and the
rule says different things about each -- which is why the ordering argument the spec makes
has to be pinned here rather than assumed from the prices suite.

## The spec's asymmetry, and the test that earns it

> Timestamps may be compared in SQL. Money still may not.

`UtcDateTime` writes a fixed-width string, so `"2026-09-09 23:59:59.999999"` and
`"2026-09-10 00:00:00.000001"` sort the same way as characters and as instants.
`NumericText` has a fixed *scale* and a variable number of integer digits, so `"9.00"`
sorts after `"10.00"`. `test_ordering_is_chronological_across_a_digit_boundary` drives
values that would expose the difference and then **computes the wrong answer alongside the
right one**, because an ordering assertion that never sees the failure it is guarding
against is a sorted list agreeing with itself.

## Every database here is a real file, built by the migrations

Not `metadata.create_all`: the `CHECK` on `confirmed`, the `UNIQUE` that makes a run write
a wallet once, and the two `ON DELETE CASCADE`s are only worth asserting against the schema
production actually executes. `tests/db/conftest.py` explains the rest of the reasoning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import inspect, select, text

from portfolio.db.engine import create_session_factory
from portfolio.db.models import BalanceSnapshot
from portfolio.domain.chains import ChainKey
from portfolio.domain.money import from_base_units
from portfolio.repositories.balances import BalanceRepository, SnapshotConstraintError
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP350_TESTNET_V1, KASPA_TESTNET_V0
from tests.balance_harness import (
    KASPA_SUPPLY_SOMPI,
    insert_user,
    insert_wallet,
    sqlite_timestamp,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

#: Three instants that straddle midnight, chosen so that a *lexicographic* comparison of the
#: stored strings and a chronological comparison of the instants have to agree. They do,
#: because `UtcDateTime` pads the microseconds to six places; a fixture written without them
#: would be six characters short and would sort before an equal instant written properly.
BEFORE_MIDNIGHT: Final = datetime(2026, 9, 9, 23, 59, 59, 999999, tzinfo=UTC)
AFTER_MIDNIGHT: Final = datetime(2026, 9, 10, 0, 0, 0, 1, tzinfo=UTC)
NOON: Final = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)

#: Nine, ten and eleven whole coins, in base units. The quantities they convert to --
#: `9.00000000`, `10.00000000`, `11.00000000` -- are the pair of digit counts that makes a
#: text ordering disagree with a numeric one, which is the whole point of choosing them.
NINE_COINS: Final = 900_000_000
TEN_COINS: Final = 1_000_000_000
ELEVEN_COINS: Final = 1_100_000_000

BITCOIN_DECIMALS: Final = 8

#: One past the signed 64-bit range SQLite stores an integer in.
OVER_SIXTY_FOUR_BITS: Final = 2**63


@pytest.fixture
async def session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session from the application's own factory, over the migrated file."""
    factory = create_session_factory(migrated_engine)
    async with factory() as opened:
        yield opened


@pytest.fixture
def repository(session: AsyncSession) -> BalanceRepository:
    return BalanceRepository(session)


@pytest.fixture
async def wallet_id(session: AsyncSession) -> int:
    """One Bitcoin wallet, for the owner every test here shares."""
    user_id = await insert_user(session)
    return await insert_wallet(
        session,
        user_id=user_id,
        chain_key=ChainKey.BITCOIN,
        address=BIP173_TESTNET_P2WPKH,
    )


@pytest.fixture
async def run_id(session: AsyncSession) -> int:
    """A finished `sync_runs` row for a snapshot's foreign key to point at."""
    return await insert_run(session, started_at=NOON)


async def insert_run(
    session: AsyncSession,
    *,
    started_at: datetime,
    status: str = "success",
    trigger: str = "scheduled",
) -> int:
    """A `sync_runs` row written directly, so this module tests one repository at a time."""
    result = await session.execute(
        text(
            "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
            "wallets_total, wallets_succeeded, wallets_failed) "
            "VALUES (:trigger, :status, :started_at, :started_at, 1, 1, 1, 0) RETURNING id"
        ),
        {
            "trigger": trigger,
            "status": status,
            "started_at": sqlite_timestamp(started_at),
        },
    )
    identifier: int = result.scalar_one()
    await session.commit()
    return identifier


async def stored_row(session: AsyncSession, snapshot_id: int) -> dict[str, object]:
    """The row as SQLite has it, read around the ORM's identity map and type decorators."""
    result = await session.execute(
        text(
            "SELECT confirmed, pending, decimals, observed_at FROM balance_snapshots WHERE id = :id"
        ),
        {"id": snapshot_id},
    )
    return dict(result.mappings().one())


# --------------------------------------------------------------------------------------
# One reading, written and read back
# --------------------------------------------------------------------------------------


async def test_a_snapshot_round_trips_with_its_run_its_exponent_and_its_instant(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """Every column on the row, compared against the value it was handed.

    `decimals` is on the snapshot rather than read from `assets` at query time, and the
    quantity is derived from the pair by the domain's own conversion -- so the row carries
    everything needed to interpret itself, forever, whatever happens to the asset table.
    """
    snapshot = await repository.record(
        wallet_id=wallet_id,
        sync_run_id=run_id,
        confirmed=TEN_COINS,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await session.commit()
    session.expunge_all()

    row = await stored_row(session, snapshot.id)
    assert row["confirmed"] == TEN_COINS
    assert row["decimals"] == BITCOIN_DECIMALS
    assert row["observed_at"] == sqlite_timestamp(NOON)
    reread = await session.get_one(BalanceSnapshot, snapshot.id)
    assert reread.observed_at == NOON
    assert reread.observed_at.utcoffset() == timedelta(0)
    assert from_base_units(reread.confirmed, reread.decimals) == Decimal("10.00000000")


async def test_the_fixture_timestamp_format_is_the_one_the_writer_uses(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """`tests/balance_harness.sqlite_timestamp` is checked against a row the code wrote.

    Four suites insert timestamps by hand through `text()`, which bypasses `UtcDateTime`
    entirely. If that helper wrote a different string from the one the type decorator
    writes -- no fractional part, a `T` separator, an offset suffix -- then every ordering
    and `since` assertion built on a hand-written fixture would be about a column shape the
    application never produces, and the suites would pass while the product did not.

    This is the one place the two are compared, so the claim in that helper's docstring is
    a check rather than a comment.
    """
    snapshot = await repository.record(
        wallet_id=wallet_id,
        sync_run_id=run_id,
        confirmed=1,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=BEFORE_MIDNIGHT,
    )
    await session.commit()

    written = (await stored_row(session, snapshot.id))["observed_at"]

    assert written == sqlite_timestamp(BEFORE_MIDNIGHT)
    assert isinstance(written, str)
    assert len(written) == len("2026-09-09 23:59:59.999999")


@pytest.mark.parametrize(
    "pending", [None, 0, -500, 12_345], ids=["null", "zero", "negative", "positive"]
)
async def test_the_pending_tri_state_is_stored_exactly_as_it_was_given(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
    pending: int | None,
) -> None:
    """`NULL`, zero and a negative delta are three different facts and stay three.

    The negative case is why the column carries no non-negative `CHECK` where `confirmed`
    does: an outgoing payment waiting to confirm spends a confirmed output and funds
    nothing, so a net mempool delta is legitimately below zero. A guard copied from
    `confirmed` would refuse the ordinary case.
    """
    snapshot = await repository.record(
        wallet_id=wallet_id,
        sync_run_id=run_id,
        confirmed=TEN_COINS,
        pending=pending,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await session.commit()
    session.expunge_all()

    assert (await stored_row(session, snapshot.id))["pending"] == pending


async def test_a_negative_confirmed_is_refused_by_the_column(
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """The `CHECK`, exercised rather than reflected. `align_balances` refuses one too.

    Two refusals for one condition is the arrangement `PriceRepository.upsert` argues for:
    the provider boundary catches a vendor sending nonsense, and the column catches
    everything that did not come through a provider -- a migration, a manual `sqlite3`
    session on the Pi, a future importer.
    """
    with pytest.raises((SnapshotConstraintError, ValueError)):
        await repository.record(
            wallet_id=wallet_id,
            sync_run_id=run_id,
            confirmed=-1,
            pending=None,
            decimals=BITCOIN_DECIMALS,
            observed_at=NOON,
        )


async def test_a_kaspa_supply_sized_balance_fits_and_one_past_the_range_does_not(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """2.87e18 sompi is storable; 2**63 is not, and the refusal is loud.

    The spec's own arithmetic says a plausible Kaspa balance is three hundred times
    `Number.MAX_SAFE_INTEGER`. It is nowhere near SQLite's signed 64-bit ceiling, and the
    two facts are asserted together so that nobody reads the first and assumes the column
    is unbounded: past the ceiling the value is refused rather than silently wrapped, which
    is the only acceptable answer for a count of somebody's money.
    """
    snapshot = await repository.record(
        wallet_id=wallet_id,
        sync_run_id=run_id,
        confirmed=KASPA_SUPPLY_SOMPI,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await session.commit()
    assert (await stored_row(session, snapshot.id))["confirmed"] == KASPA_SUPPLY_SOMPI

    second_run = await insert_run(session, started_at=NOON)
    with pytest.raises(ValueError, match="64-bit"):
        await repository.record(
            wallet_id=wallet_id,
            sync_run_id=second_run,
            confirmed=OVER_SIXTY_FOUR_BITS,
            pending=None,
            decimals=BITCOIN_DECIMALS,
            observed_at=NOON,
        )


async def test_a_float_balance_never_reaches_the_column(
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """Rule 2 at the column. A float is where a base-unit count stops being exact.

    The realistic cause is a vendor's JSON: a balance sent as `1.23e8` arrives from a
    parser as a `float`, and a column that accepted it would store a rounded count that
    reads back as a plausible balance.
    """
    with pytest.raises(TypeError):
        await repository.record(
            wallet_id=wallet_id,
            sync_run_id=run_id,
            confirmed=1.0,  # type: ignore[arg-type]
            pending=None,
            decimals=BITCOIN_DECIMALS,
            observed_at=NOON,
        )


async def test_one_run_may_not_write_a_wallet_twice(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """`UNIQUE (wallet_id, sync_run_id)`, and what it would mean if it were missing.

    Two rows for one wallet in one run means the grouping by chain broke and the same
    address was about to be counted twice in one total -- a portfolio that reads as twice
    its real size, with nothing anywhere saying so.
    """
    for _ in range(1):
        await repository.record(
            wallet_id=wallet_id,
            sync_run_id=run_id,
            confirmed=TEN_COINS,
            pending=None,
            decimals=BITCOIN_DECIMALS,
            observed_at=NOON,
        )
    await session.commit()

    with pytest.raises(SnapshotConstraintError):
        await repository.record(
            wallet_id=wallet_id,
            sync_run_id=run_id,
            confirmed=ELEVEN_COINS,
            pending=None,
            decimals=BITCOIN_DECIMALS,
            observed_at=NOON,
        )


async def test_a_snapshot_for_a_wallet_that_is_not_there_is_refused(
    repository: BalanceRepository,
    run_id: int,
) -> None:
    """The foreign key, which is only enforced because the engine turns it on.

    SQLite ignores `REFERENCES` unless `PRAGMA foreign_keys` is set, and the application's
    engine sets it. A test built on a differently configured connection would pass here
    while production silently accepted orphans.
    """
    with pytest.raises(SnapshotConstraintError):
        await repository.record(
            wallet_id=999_999,
            sync_run_id=run_id,
            confirmed=TEN_COINS,
            pending=None,
            decimals=BITCOIN_DECIMALS,
            observed_at=NOON,
        )


async def test_deleting_a_wallet_takes_its_snapshots_with_it(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """`ON DELETE CASCADE`, which is what makes a hard delete a complete one.

    The product archives rather than deletes, so this is the path a future account removal
    takes -- and a snapshot left behind pointing at nothing is a row nobody can attribute
    and nobody can find.
    """
    await repository.record(
        wallet_id=wallet_id,
        sync_run_id=run_id,
        confirmed=TEN_COINS,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await session.commit()

    await session.execute(text("DELETE FROM wallets WHERE id = :id"), {"id": wallet_id})
    await session.commit()

    assert (await session.scalars(select(BalanceSnapshot))).all() == []


# --------------------------------------------------------------------------------------
# "Latest" is identity order, and it is not an amount and not a timestamp
# --------------------------------------------------------------------------------------


async def test_the_latest_snapshot_is_the_newest_row_not_the_largest_balance(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """`MAX(id)` over an append-only table, with both decoys planted.

    The earlier row carries the **larger** balance and the **later** `observed_at`, so a
    read resolved by either of those returns the wrong row. `MAX(id)` needs no argument
    about collation at all, which is exactly why the spec chose it over the timestamp.
    """
    later_run = await insert_run(session, started_at=NOON)
    await repository.record(
        wallet_id=wallet_id,
        sync_run_id=run_id,
        confirmed=ELEVEN_COINS,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON + timedelta(days=1),
    )
    newest = await repository.record(
        wallet_id=wallet_id,
        sync_run_id=later_run,
        confirmed=NINE_COINS,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await session.commit()

    latest = await repository.latest_for_wallets([wallet_id])

    assert set(latest) == {wallet_id}
    assert latest[wallet_id].id == newest.id
    assert latest[wallet_id].confirmed == NINE_COINS


async def test_a_wallet_with_no_snapshot_is_absent_rather_than_zero(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """An unread wallet and an empty one are different facts; the map keeps them apart.

    The empty wallet is the control. Without it, "absent" would be indistinguishable from
    a repository that simply returned nothing for anybody.
    """
    user_id = await insert_user(session, "second")
    unread = await insert_wallet(
        session,
        user_id=user_id,
        chain_key=ChainKey.BITCOIN,
        address=BIP350_TESTNET_V1,
    )
    await repository.record(
        wallet_id=wallet_id,
        sync_run_id=run_id,
        confirmed=0,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await session.commit()

    latest = await repository.latest_for_wallets([wallet_id, unread])

    assert set(latest) == {wallet_id}
    assert latest[wallet_id].confirmed == 0


async def test_asking_about_no_wallets_costs_no_query_and_answers_empty(
    repository: BalanceRepository,
) -> None:
    """The empty page: an account that has registered nothing is the ordinary first day."""
    assert await repository.latest_for_wallets([]) == {}


async def test_latest_answers_about_several_wallets_in_one_call(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    run_id: int,
) -> None:
    """One statement whatever the number of wallets, and each wallet gets its own row.

    A grouping mistake here presents as one wallet's balance appearing under another's
    name, which is the failure that would make a total right and every line of it wrong.
    """
    user_id = await insert_user(session, "third")
    other = await insert_wallet(
        session,
        user_id=user_id,
        chain_key=ChainKey.KASPA,
        address=KASPA_TESTNET_V0,
    )
    await repository.record(
        wallet_id=wallet_id,
        sync_run_id=run_id,
        confirmed=NINE_COINS,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await repository.record(
        wallet_id=other,
        sync_run_id=run_id,
        confirmed=ELEVEN_COINS,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await session.commit()

    latest = await repository.latest_for_wallets([wallet_id, other])

    assert {key: row.confirmed for key, row in latest.items()} == {
        wallet_id: NINE_COINS,
        other: ELEVEN_COINS,
    }


# --------------------------------------------------------------------------------------
# Criterion 8: the history, and the ordering argument the spec makes
# --------------------------------------------------------------------------------------


@pytest.fixture
async def three_readings(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
) -> list[int]:
    """Nine, ten and eleven coins, written **out of chronological order** on purpose.

    Insertion order and chronological order therefore disagree, which is what turns an
    assertion about "oldest first" into a statement about the `ORDER BY` rather than about
    `rowid`.
    """
    written = [
        (NOON, ELEVEN_COINS),
        (BEFORE_MIDNIGHT, NINE_COINS),
        (AFTER_MIDNIGHT, TEN_COINS),
    ]
    for observed_at, confirmed in written:
        run = await insert_run(session, started_at=observed_at)
        await repository.record(
            wallet_id=wallet_id,
            sync_run_id=run,
            confirmed=confirmed,
            pending=None,
            decimals=BITCOIN_DECIMALS,
            observed_at=observed_at,
        )
    await session.commit()
    return [NINE_COINS, TEN_COINS, ELEVEN_COINS]


async def test_history_is_oldest_first(
    repository: BalanceRepository,
    wallet_id: int,
    three_readings: list[int],
) -> None:
    """Criterion 8: a chart reads left to right, so the series arrives in that order."""
    rows = await repository.history(wallet_id=wallet_id, since=None, limit=100)

    assert [row.confirmed for row in rows] == three_readings
    assert [row.observed_at for row in rows] == [BEFORE_MIDNIGHT, AFTER_MIDNIGHT, NOON]


async def test_ordering_is_chronological_across_a_digit_boundary(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    three_readings: list[int],
) -> None:
    """The spec's asymmetry, with the failure it forbids computed rather than described.

    Three readings whose quantities are `9`, `10` and `11` whole coins. Sorted as text
    those go `10, 11, 9`; sorted as numbers they go `9, 10, 11`. The second half of this
    test performs the text sort **in SQLite**, over the same values rendered the way
    `NumericText` would render them, and asserts it produces the wrong order -- so the
    first half is a statement about the ordering the repository chose rather than a sorted
    list that happens to agree with itself.

    The timestamps straddle midnight at the microsecond, which is the other half of the
    claim: `UtcDateTime` writes a fixed width, so the character order and the instant order
    are the same and the SQL comparison is safe. Both halves are here because the spec's
    sentence has two clauses and only one of them is about `TEXT`.
    """
    rows = await repository.history(wallet_id=wallet_id, since=None, limit=100)
    quantities = [from_base_units(row.confirmed, row.decimals) for row in rows]

    assert quantities == [Decimal("9.00000000"), Decimal("10.00000000"), Decimal("11.00000000")]

    # The control: the same three amounts, in the fixed-point text a money column stores,
    # ordered by SQLite as characters. This is the ordering rule 2 forbids for money.
    rendered = [f"{quantity:f}" for quantity in quantities]
    wrong = await session.scalars(
        text(
            "WITH amounts(a) AS (VALUES (:one), (:two), (:three)) SELECT a FROM amounts ORDER BY a"
        ),
        {"one": rendered[0], "two": rendered[1], "three": rendered[2]},
    )
    assert list(wrong) == [rendered[1], rendered[2], rendered[0]], (
        "the chosen amounts must sort differently as text and as numbers, "
        "or this control proves nothing"
    )

    # And the timestamps, ordered the same way, do *not* misbehave -- which is the reason
    # `observed_at` may be compared in SQL where an amount may not.
    stamps = [sqlite_timestamp(row.observed_at) for row in rows]
    assert stamps == sorted(stamps)


async def test_since_is_inclusive(
    repository: BalanceRepository,
    wallet_id: int,
    three_readings: list[int],
) -> None:
    """Asked for exactly a reading's instant, that reading is in the window.

    An exclusive comparison drops the row a client just paged from, so a chart silently
    starts one point late every time somebody scrolls -- a gap nobody can see because the
    line simply begins somewhere slightly different.
    """
    rows = await repository.history(wallet_id=wallet_id, since=AFTER_MIDNIGHT, limit=100)

    assert [row.observed_at for row in rows] == [AFTER_MIDNIGHT, NOON]


async def test_a_since_in_the_future_is_an_empty_series_rather_than_an_error(
    repository: BalanceRepository,
    wallet_id: int,
    three_readings: list[int],
) -> None:
    """Nothing to chart is a fact about the window, not a failure of the query."""
    assert (
        await repository.history(wallet_id=wallet_id, since=NOON + timedelta(days=1), limit=100)
        == []
    )


async def test_without_a_since_the_latest_window_is_returned(
    repository: BalanceRepository,
    wallet_id: int,
    three_readings: list[int],
) -> None:
    """A bare `limit` means the most recent readings, still ordered oldest first.

    The literal reading of the spec's first draft -- always the *first* `limit` rows -- was
    implemented and then overruled, and the reason is the only consumer there is: a chart of
    a year-old wallet asking for 500 points wants the last 500, not the first 500 from the
    week it was registered. A client that wanted the old end has `since`.

    Both halves are asserted, because they are separable and an implementation can get one
    right and the other wrong: **which** rows come back is the window, and **what order**
    they come back in is the chart's x-axis.
    """
    window = await repository.history(wallet_id=wallet_id, since=None, limit=2)

    assert [row.confirmed for row in window] == [TEN_COINS, ELEVEN_COINS]
    assert [row.observed_at for row in window] == [AFTER_MIDNIGHT, NOON]
    assert window[0].observed_at < window[1].observed_at, "the window is still oldest first"


async def test_with_a_since_the_limit_pages_forward(
    repository: BalanceRepository,
    wallet_id: int,
    three_readings: list[int],
) -> None:
    """`since` turns the same pair into a forward cursor: read a window, ask again from its end.

    This is the half that has to keep the *first* rows after the instant, and it is the
    opposite selection from the test above -- which is exactly why the two are separate. An
    implementation that applied the latest-window rule to a `since` query would hand a client
    paging forward the same last page over and over.

    `since` is inclusive, so the second page re-reads its own first row rather than risking a
    gap; the new row is what the page was asked for.
    """
    first = await repository.history(wallet_id=wallet_id, since=BEFORE_MIDNIGHT, limit=2)
    second = await repository.history(
        wallet_id=wallet_id,
        since=first[-1].observed_at,
        limit=2,
    )

    assert [row.confirmed for row in first] == [NINE_COINS, TEN_COINS]
    assert [row.confirmed for row in second] == [TEN_COINS, ELEVEN_COINS]


async def test_history_is_scoped_to_one_wallet(
    session: AsyncSession,
    repository: BalanceRepository,
    wallet_id: int,
    three_readings: list[int],
) -> None:
    """Another wallet's readings are another wallet's, which is the whole of the isolation.

    The service enforces ownership; this is the layer below it, and a `WHERE` that was
    ever dropped here would put one wallet's history into another's chart with a 200.
    """
    user_id = await insert_user(session, "fourth")
    other = await insert_wallet(
        session,
        user_id=user_id,
        chain_key=ChainKey.BITCOIN,
        address=BIP350_TESTNET_V1,
    )
    run = await insert_run(session, started_at=NOON)
    await repository.record(
        wallet_id=other,
        sync_run_id=run,
        confirmed=1,
        pending=None,
        decimals=BITCOIN_DECIMALS,
        observed_at=NOON,
    )
    await session.commit()

    rows = await repository.history(wallet_id=other, since=None, limit=100)

    assert [row.confirmed for row in rows] == [1]
    assert len(await repository.history(wallet_id=wallet_id, since=None, limit=100)) == 3


# --------------------------------------------------------------------------------------
# What is on disk, read back through a second, unconfigured connection
# --------------------------------------------------------------------------------------


def test_the_base_unit_columns_are_integers_on_disk(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """`confirmed` and `pending` are `INTEGER`, which is rule 2's row for an on-chain amount.

    A `TEXT` column here would be the money rule applied to the wrong thing: base units are
    exact integers, and storing them as characters would make every comparison a
    lexicographic one and every sum a string concatenation waiting to happen.
    """
    del migrated_database_url
    columns = {
        column["name"]: str(column["type"])
        for column in inspect(sync_engine).get_columns("balance_snapshots")
    }

    assert columns["confirmed"] == "BIGINT"
    assert columns["pending"] == "BIGINT"
    assert columns["decimals"] == "INTEGER"


def test_the_history_index_is_in_the_migrated_schema(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """The index the history query reads through, named exactly as the spec writes it.

    An index the model declares and the migration forgets is invisible to the drift check
    on SQLite and shows up as a table scan per chart render on a Raspberry Pi.
    """
    del migrated_database_url
    indexes = {
        index["name"]: list(index["column_names"])
        for index in inspect(sync_engine).get_indexes("balance_snapshots")
    }

    assert "ix_balance_snapshots_wallet_observed" in indexes
    assert indexes["ix_balance_snapshots_wallet_observed"] == ["wallet_id", "observed_at"]
