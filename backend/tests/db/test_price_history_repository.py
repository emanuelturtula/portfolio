"""Spec 037's `price_history` repository: one row per day, and R2 deciding which price wins.

Every database here is a real file under `tmp_path`, built by running the migrations, for the
reason `test_prices_repository.py` gives at length.

## R2, which is the reason `record` reads before it writes

`UNIQUE (asset_id, quote_currency, day)` keeps one row per day; which price that row holds
depends on the row already there:

| Already there | Written | Outcome |
|---|---|---|
| nothing | either | inserted |
| `observed` | `observed` | replaced: the latest price seen that day is the nearer to a close |
| `observed` | `close` | replaced: the close is final |
| `close` | `close` | replaced: the backfill rewrites what it wrote |
| `close` | `observed` | **refused, and `False`**: an hourly price must never undo a close |

Each row of that table is a test below, asserted on the row read back through a second
session -- not on the object the writing session still holds, which would be the verifier
sharing state with its subject.

**Nothing here sums, orders or compares money in SQL.** `series` orders by `asset_id` and
`day`, and `latest_close_recorded_at` takes a `MAX()` over an instant.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError

from portfolio.db.engine import create_session_factory
from portfolio.db.models import Asset, PriceHistory
from portfolio.repositories.price_history import CLOSE, OBSERVED, PriceHistoryRepository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

USD: Final = "USD"
EUR: Final = "EUR"
BTC: Final = "BTC"
KAS: Final = "KAS"

KRAKEN: Final = "kraken"
COINBASE: Final = "coinbase"

DAY: Final = date(2026, 10, 7)
RECORDED_AT: Final = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
LATER: Final = RECORDED_AT + timedelta(hours=1)

#: The digits a vendor sent, and what `NumericText(12)` must write for them on disk.
BTC_DIGITS: Final = "68440.30000"
BTC_STORED: Final = "68440.300000000000"


@pytest.fixture
def factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """The application's own session factory over the migrated file."""
    return create_session_factory(migrated_engine)


@pytest.fixture
async def session(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as opened:
        yield opened


@pytest.fixture
def repository(session: AsyncSession) -> PriceHistoryRepository:
    return PriceHistoryRepository(session)


async def asset_id(session: AsyncSession, symbol: str) -> int:
    """The seeded asset's primary key, read back rather than assumed."""
    found = (await session.scalars(select(Asset).where(Asset.symbol == symbol))).one()
    return found.id


async def record(
    repository: PriceHistoryRepository,
    asset: int,
    *,
    amount: str,
    basis: str,
    source: str = KRAKEN,
    day: date = DAY,
    currency: str = USD,
    recorded_at: datetime = RECORDED_AT,
) -> bool:
    """One `record` call with the fields a test does not care about filled in."""
    return await repository.record(
        asset_id=asset,
        quote_currency=currency,
        day=day,
        amount=Decimal(amount),
        basis=basis,
        source=source,
        recorded_at=recorded_at,
    )


async def stored_rows(factory: async_sessionmaker[AsyncSession]) -> list[PriceHistory]:
    """Every `price_history` row, read through a session of its own after the write."""
    async with factory() as reader:
        return list(await reader.scalars(select(PriceHistory).order_by(PriceHistory.id)))


async def stored_text(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """The raw `TEXT` of every amount, without the type decorator in the way."""
    async with factory() as reader:
        return list((await reader.execute(text("SELECT amount FROM price_history"))).scalars())


# --------------------------------------------------------------------------------------
# R2, row by row
# --------------------------------------------------------------------------------------


async def test_a_first_price_for_a_day_is_inserted_with_every_field(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing there: the row is inserted, `True`, and its amount keeps the vendor's digits."""
    btc = await asset_id(session, BTC)

    written = await record(repository, btc, amount=BTC_DIGITS, basis=OBSERVED)
    await session.commit()

    assert written is True
    (row,) = await stored_rows(factory)
    assert (row.asset_id, row.quote_currency, row.day) == (btc, USD, DAY)
    assert row.amount == Decimal(BTC_DIGITS)
    assert (row.basis, row.source) == (OBSERVED, KRAKEN)
    assert row.recorded_at == RECORDED_AT
    assert row.recorded_at.tzinfo is not None, "an aware instant, read back aware"
    assert await stored_text(factory) == [BTC_STORED]


async def test_a_later_observed_price_replaces_an_earlier_one_for_the_same_day(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The hourly refresh's case: the latest price seen that day is the one kept."""
    btc = await asset_id(session, BTC)
    await record(repository, btc, amount="68000", basis=OBSERVED)
    await session.commit()

    written = await record(
        repository, btc, amount="68500.5", basis=OBSERVED, source=COINBASE, recorded_at=LATER
    )
    await session.commit()

    assert written is True
    (row,) = await stored_rows(factory)
    assert (row.amount, row.basis, row.source) == (Decimal("68500.5"), OBSERVED, COINBASE)
    assert row.recorded_at == LATER


async def test_an_observed_price_never_replaces_a_close_and_says_so(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R2's whole point: `False`, and every field of the close exactly as it was.

    The refresh runs every hour; a close the backfill stored for today's date -- or any day a
    clock skew puts the refresh on -- must not be replaced by the price at some hour of it.
    """
    btc = await asset_id(session, BTC)
    await record(repository, btc, amount=BTC_DIGITS, basis=CLOSE)
    await session.commit()

    written = await record(
        repository, btc, amount="1", basis=OBSERVED, source=COINBASE, recorded_at=LATER
    )
    await session.commit()

    assert written is False
    (row,) = await stored_rows(factory)
    assert (row.amount, row.basis, row.source) == (Decimal(BTC_DIGITS), CLOSE, KRAKEN)
    assert row.recorded_at == RECORDED_AT
    assert await stored_text(factory) == [BTC_STORED]


async def test_a_close_replaces_an_observed_price(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The backfill's case: yesterday's last hourly price gives way to yesterday's close."""
    btc = await asset_id(session, BTC)
    await record(repository, btc, amount="68000", basis=OBSERVED, source=COINBASE)
    await session.commit()

    written = await record(repository, btc, amount=BTC_DIGITS, basis=CLOSE, recorded_at=LATER)
    await session.commit()

    assert written is True
    (row,) = await stored_rows(factory)
    assert (row.amount, row.basis, row.source) == (Decimal(BTC_DIGITS), CLOSE, KRAKEN)
    assert row.recorded_at == LATER


async def test_a_close_replaces_a_close(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The daily backfill rewrites what it wrote, so the newest answer stands, still one row."""
    btc = await asset_id(session, BTC)
    await record(repository, btc, amount="68000", basis=CLOSE)
    await session.commit()

    written = await record(repository, btc, amount=BTC_DIGITS, basis=CLOSE, recorded_at=LATER)
    await session.commit()

    assert written is True
    (row,) = await stored_rows(factory)
    assert (row.amount, row.basis) == (Decimal(BTC_DIGITS), CLOSE)
    assert row.recorded_at == LATER


async def test_another_day_another_currency_or_another_asset_is_another_row(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The unique key is the triple, so a close on one never refuses an observed on another."""
    btc = await asset_id(session, BTC)
    kas = await asset_id(session, KAS)
    await record(repository, btc, amount="1", basis=CLOSE)

    assert await record(repository, btc, amount="2", basis=OBSERVED, day=DAY + timedelta(1))
    assert await record(repository, btc, amount="3", basis=OBSERVED, currency=EUR)
    assert await record(repository, kas, amount="4", basis=OBSERVED)
    await session.commit()

    rows = await stored_rows(factory)
    assert [(row.asset_id, row.quote_currency, row.day, row.amount) for row in rows] == [
        (btc, USD, DAY, Decimal(1)),
        (btc, USD, DAY + timedelta(1), Decimal(2)),
        (btc, EUR, DAY, Decimal(3)),
        (kas, USD, DAY, Decimal(4)),
    ]


async def test_record_flushes_and_leaves_the_commit_to_the_caller(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A rollback after `record` leaves nothing: the services own their transactions."""
    btc = await asset_id(session, BTC)

    await record(repository, btc, amount="1", basis=CLOSE)
    assert await stored_rows(factory) == [], "flushed but not committed: not visible elsewhere"
    await session.rollback()

    assert await stored_rows(factory) == []


# --------------------------------------------------------------------------------------
# Refusals: before the query, and from the column
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        pytest.param(68440.3, TypeError, id="a float"),
        pytest.param(True, TypeError, id="a bool"),
        pytest.param(68440, TypeError, id="an int"),
        pytest.param("68440.3", TypeError, id="a string"),
        pytest.param(Decimal("NaN"), ValueError, id="NaN"),
        pytest.param(Decimal("Infinity"), ValueError, id="an infinity"),
    ],
)
async def test_an_amount_that_is_not_a_finite_decimal_is_refused_before_anything_is_read(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    amount: object,
    expected: type[Exception],
) -> None:
    """`require_amount` decides, first: no row is read, added or flushed."""
    btc = await asset_id(session, BTC)

    with pytest.raises(expected, match=r"price_history\.amount"):
        await repository.record(
            asset_id=btc,
            quote_currency=USD,
            day=DAY,
            amount=amount,  # type: ignore[arg-type]
            basis=CLOSE,
            source=KRAKEN,
            recorded_at=RECORDED_AT,
        )

    assert not session.new, "nothing was staged for a refused amount"
    await session.commit()
    assert await stored_rows(factory) == []


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("1" + "0" * 26, id="27 digits before the point"),
        pytest.param("1E+300", id="an exponent far past the column"),
        pytest.param("0.0000000000001", id="an amount that rounds away to zero"),
    ],
)
async def test_a_value_the_column_refuses_arrives_as_its_own_value_error(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    digits: str,
) -> None:
    """`NumericText` refuses at bind time, inside the flush; `_flush` unwraps it.

    `MONEY_PRECISION - PRICE_SCALE` is 26 digits before the point, so 27 cannot be stored,
    and neither can a positive amount twelve places cannot hold. The caller gets the
    column's `ValueError`, never SQLAlchemy's `StatementError`, which `services/` may not
    name -- and the message carries no value.
    """
    btc = await asset_id(session, BTC)

    with pytest.raises(ValueError, match="NumericText") as caught:
        await record(repository, btc, amount=digits, basis=CLOSE)

    assert not isinstance(caught.value, StatementError)
    assert isinstance(caught.value.__cause__, StatementError), "unwrapped from the flush"
    assert digits not in str(caught.value)
    await session.rollback()


async def test_a_value_the_column_refuses_on_an_overwrite_is_refused_too(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The update arm flushes through the same `_flush`, and the stored row survives it."""
    btc = await asset_id(session, BTC)
    await record(repository, btc, amount=BTC_DIGITS, basis=OBSERVED)
    await session.commit()

    with pytest.raises(ValueError, match="NumericText"):
        await record(repository, btc, amount="1" + "0" * 26, basis=CLOSE)
    await session.rollback()

    (row,) = await stored_rows(factory)
    assert (row.amount, row.basis) == (Decimal(BTC_DIGITS), OBSERVED)


@pytest.mark.parametrize(
    ("currency", "basis", "constraint"),
    [
        pytest.param("GBP", CLOSE, "ck_price_history_quote_currency", id="a third currency"),
        pytest.param(USD, "guessed", "ck_price_history_basis", id="a basis that is neither"),
    ],
)
async def test_a_database_refusal_is_left_as_the_drivers_own(
    repository: PriceHistoryRepository,
    session: AsyncSession,
    currency: str,
    basis: str,
    constraint: str,
) -> None:
    """The other half of `_flush`: a `CHECK` violation is still an `IntegrityError`.

    Without this, a `_flush` that unwrapped every `StatementError` would satisfy the test
    above and deliver a constraint refusal as whatever the driver held underneath.
    """
    btc = await asset_id(session, BTC)

    with pytest.raises(IntegrityError) as caught:
        await record(repository, btc, amount="1", basis=basis, currency=currency)

    assert constraint in str(caught.value)
    await session.rollback()


# --------------------------------------------------------------------------------------
# `series`
# --------------------------------------------------------------------------------------


async def test_a_series_of_no_assets_is_empty(repository: PriceHistoryRepository) -> None:
    """No asset asked about, nothing answered -- and no `IN ()` sent to SQLite."""
    assert (
        await repository.series(
            asset_ids=[], quote_currency=USD, first_day=DAY, last_day=DAY + timedelta(30)
        )
        == {}
    )


async def test_a_series_maps_each_asset_to_its_days_inclusive_of_both_bounds(
    repository: PriceHistoryRepository,
    session: AsyncSession,
) -> None:
    """Both ends of the range are in it; the day either side is not; a gap stays a gap.

    The other currency of the same asset and day is not in a USD series, and an asset not
    asked about is not in it at all. Both bases are served alike: which price a day holds
    was R2's decision at write time, not the reader's.
    """
    btc = await asset_id(session, BTC)
    kas = await asset_id(session, KAS)
    first, last = date(2026, 10, 1), date(2026, 10, 3)
    for day, amount in (
        (first - timedelta(1), "1"),
        (first, "2"),
        (first + timedelta(1), "3"),
        (last, "4"),
        (last + timedelta(1), "5"),
    ):
        await record(repository, btc, amount=amount, basis=CLOSE, day=day)
    await record(repository, btc, amount="99", basis=CLOSE, day=first, currency=EUR)
    await record(repository, kas, amount="0.042", basis=OBSERVED, day=last)
    await session.commit()

    usd = await repository.series(
        asset_ids=[btc, kas], quote_currency=USD, first_day=first, last_day=last
    )
    only_btc = await repository.series(
        asset_ids=[btc], quote_currency=USD, first_day=first, last_day=last
    )

    assert usd == {
        btc: {first: Decimal(2), first + timedelta(1): Decimal(3), last: Decimal(4)},
        kas: {last: Decimal("0.042")},
    }
    assert only_btc == {btc: usd[btc]}


async def test_a_series_in_the_other_currency_and_an_asset_with_no_rows_are_absent(
    repository: PriceHistoryRepository,
    session: AsyncSession,
) -> None:
    """EUR rows only in an EUR series; an asset with nothing in range has no key, not `{}`."""
    btc = await asset_id(session, BTC)
    kas = await asset_id(session, KAS)
    await record(repository, btc, amount="1", basis=CLOSE)
    await record(repository, btc, amount="99", basis=CLOSE, currency=EUR)
    await session.commit()

    eur = await repository.series(
        asset_ids=[btc, kas], quote_currency=EUR, first_day=DAY, last_day=DAY
    )

    assert eur == {btc: {DAY: Decimal(99)}}


# --------------------------------------------------------------------------------------
# `latest_close_recorded_at`
# --------------------------------------------------------------------------------------


async def test_the_latest_close_is_none_on_an_empty_table(
    repository: PriceHistoryRepository,
) -> None:
    assert await repository.latest_close_recorded_at() is None


async def test_the_latest_close_is_the_newest_close_and_ignores_observed_rows(
    repository: PriceHistoryRepository,
    session: AsyncSession,
) -> None:
    """The backfill timer's `last_run_at`: only a `close` counts as a backfill having run.

    An `observed` row written later -- the hourly refresh, every hour -- must not make the
    backfill look recent, or a fresh install would never backfill at all.
    """
    btc = await asset_id(session, BTC)
    kas = await asset_id(session, KAS)
    await record(repository, btc, amount="1", basis=CLOSE, day=DAY - timedelta(1))
    await record(repository, kas, amount="1", basis=CLOSE, recorded_at=LATER)
    await record(
        repository, btc, amount="2", basis=OBSERVED, recorded_at=LATER + timedelta(hours=5)
    )
    await session.commit()

    latest = await repository.latest_close_recorded_at()

    assert latest == LATER
    assert latest is not None
    assert latest.tzinfo is not None


async def test_only_observed_rows_mean_no_backfill_has_run(
    repository: PriceHistoryRepository,
    session: AsyncSession,
) -> None:
    btc = await asset_id(session, BTC)
    await record(repository, btc, amount="1", basis=OBSERVED)
    await session.commit()

    assert await repository.latest_close_recorded_at() is None
