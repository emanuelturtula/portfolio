"""Spec 037's price backfill: every daily close a source keeps, into `price_history`, as `close`.

Driven with a fake `DailyCloseSource` over a migrated SQLite file, so the assertions are on
rows rather than on calls. What is pinned:

* **closes are stored as `close`**, with the source's name and the run's one clock read;
* **idempotent**: a second run leaves one row per day, each the same number;
* **R2 from the backfill's side**: an `observed` price for the same day is replaced;
* **one pair failing does not stop the other**, and what the first stored is **committed**
  before the second is asked -- read through a second session, which sees only what is on
  disk;
* **a pair nothing could answer costs no request**: outside the source's pairs, or with no
  `assets` row, it is `UnsupportedPair` and the source is never called;
* **the report**: days and bounds per pair, `None` bounds for an empty answer, and both
  tuples sorted by pair whatever order they were asked in.

Spec 038 adds an **older** source, driven with a fake `HistoricalCloseSource` that answers
only inside the window it is asked for:

* it is asked for **exactly** the days from its first day to the day before the earliest
  close stored for the pair (R8), and they are stored as `close` under its name;
* **once filled it is asked nothing**, and has no line in the report;
* a pair with **no close stored** is `NoRecentClose`, without a request;
* its failure is a line of its own and takes nothing the recent source stored with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import select

from portfolio.db.models import Asset, PriceHistory
from portfolio.providers.errors import (
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.prices.base import BTC, EUR, KAS, USD, DailyClose
from portfolio.repositories.price_history import CLOSE, OBSERVED, PriceHistoryRepository
from portfolio.services.price_backfill import (
    NO_RECENT_CLOSE,
    UNSUPPORTED_PAIR,
    BackfilledPair,
    BackfillReport,
    FailedPair,
    PriceBackfillService,
    build_price_backfill_service,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.prices.base import (
        DailyCloseSource,
        HistoricalCloseSource,
        PricePair,
    )

RUN_AT: Final = datetime(2026, 10, 8, 0, 30, tzinfo=UTC)
VENDOR: Final = "a-vendor"

BTC_USD: Final[PricePair] = (BTC, USD)
KAS_USD: Final[PricePair] = (KAS, USD)

FIRST: Final = date(2026, 10, 5)
SECOND: Final = date(2026, 10, 6)
THIRD: Final = date(2026, 10, 7)

BTC_CLOSES: Final = (
    DailyClose(day=FIRST, close=Decimal("62000.1")),
    DailyClose(day=SECOND, close=Decimal("62500.25")),
    DailyClose(day=THIRD, close=Decimal("63010.5")),
)
KAS_CLOSES: Final = (
    DailyClose(day=SECOND, close=Decimal("0.0421")),
    DailyClose(day=THIRD, close=Decimal("0.04228645")),
)

#: A message only a vendor could have written, so its absence from a report is meaningful.
VENDOR_PROSE: Final = "the vendor said something only a log line should never keep"


def _clock() -> datetime:
    return RUN_AT


@dataclass
class FakeCloses:
    """A `DailyCloseSource` answering from a table, or raising per pair. Checked by `mypy`."""

    name: str = VENDOR
    pairs: frozenset[PricePair] = field(default_factory=lambda: frozenset({BTC_USD, KAS_USD}))
    answers: dict[PricePair, tuple[DailyClose, ...]] = field(
        default_factory=lambda: {BTC_USD: BTC_CLOSES, KAS_USD: KAS_CLOSES}
    )
    raises: dict[PricePair, BaseException] = field(default_factory=dict)
    asked: list[PricePair] = field(default_factory=list)
    on_ask: list[PricePair] = field(default_factory=list)
    """Pairs at whose request `committed_when_asked` is filled in, from a second session."""
    factory: async_sessionmaker[AsyncSession] | None = None
    committed_when_asked: dict[PricePair, int] = field(default_factory=dict)

    async def daily_closes(self, pair: PricePair) -> Sequence[DailyClose]:
        self.asked.append(pair)
        if pair in self.on_ask and self.factory is not None:
            self.committed_when_asked[pair] = len(await rows_in(self.factory))
        error = self.raises.get(pair)
        if error is not None:
            raise error
        return self.answers.get(pair, ())


_CONFORMS: DailyCloseSource = FakeCloses()

OLDER: Final = "an-older-vendor"

#: The older fake's first day: seven days before `FIRST`, the recent fake's first close.
OLDER_FIRST: Final = date(2026, 9, 28)


def older_table(first: date, last: date) -> dict[date, Decimal]:
    """A close for every day in the range, each distinct from any the recent fake stores."""
    return {first + timedelta(days=n): Decimal(f"5{n}.125") for n in range((last - first).days + 1)}


@dataclass
class FakeOlder:
    """A `HistoricalCloseSource` answering from a table, **only inside the asked window**.

    The table deliberately reaches into the recent source's days, so a service that asked
    for the wrong window would store this source's numbers over the recent ones and the
    assertions on rows would see it. Checked by `mypy`.
    """

    name: str = OLDER
    pairs: frozenset[PricePair] = field(default_factory=lambda: frozenset({BTC_USD}))
    earliest_day: date = OLDER_FIRST
    table: dict[PricePair, dict[date, Decimal]] = field(
        default_factory=lambda: {BTC_USD: older_table(OLDER_FIRST, THIRD)}
    )
    raises: dict[PricePair, BaseException] = field(default_factory=dict)
    asked: list[tuple[PricePair, date, date]] = field(default_factory=list)

    async def daily_closes_between(
        self, pair: PricePair, first_day: date, last_day: date
    ) -> Sequence[DailyClose]:
        self.asked.append((pair, first_day, last_day))
        error = self.raises.get(pair)
        if error is not None:
            raise error
        found = self.table.get(pair, {})
        return tuple(
            DailyClose(day=day, close=found[day])
            for day in sorted(found)
            if first_day <= day <= last_day
        )


_CONFORMS_AS_OLDER: HistoricalCloseSource = FakeOlder()


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path, name="backfill.db") as made:
        yield made


async def backfill(
    factory: async_sessionmaker[AsyncSession],
    source: FakeCloses,
    pairs: Sequence[PricePair] | None = None,
    *,
    older: FakeOlder | None = None,
) -> BackfillReport:
    """One backfill over a session of its own, as the timer and the CLI run it."""
    async with factory() as session:
        service = build_price_backfill_service(session, source=source, older=older, clock=_clock)
        return await service.backfill(pairs)


@dataclass(frozen=True)
class Row:
    symbol: str
    currency: str
    day: date
    amount: Decimal
    basis: str
    source: str
    recorded_at: datetime


async def rows_in(factory: async_sessionmaker[AsyncSession]) -> list[Row]:
    """Every `price_history` row as committed, read through a session of its own."""
    async with factory() as reader:
        result = await reader.execute(
            select(PriceHistory, Asset.symbol)
            .join(Asset, Asset.id == PriceHistory.asset_id)
            .order_by(Asset.symbol, PriceHistory.quote_currency, PriceHistory.day)
        )
        return [
            Row(
                symbol=symbol,
                currency=row.quote_currency,
                day=row.day,
                amount=row.amount,
                basis=row.basis,
                source=row.source,
                recorded_at=row.recorded_at,
            )
            for row, symbol in result.all()
        ]


def closes_of(rows: Sequence[Row], symbol: str) -> dict[date, Decimal]:
    return {row.day: row.amount for row in rows if row.symbol == symbol}


# --------------------------------------------------------------------------------------
# Storing, and storing again
# --------------------------------------------------------------------------------------


async def test_every_close_is_stored_as_a_close_with_the_sources_name_and_one_instant(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both pairs, every day, `basis = close`, `source = name`, `recorded_at` = the clock."""
    source = FakeCloses()

    report = await backfill(factory, source)

    rows = await rows_in(factory)
    assert closes_of(rows, BTC) == {close.day: close.close for close in BTC_CLOSES}
    assert closes_of(rows, KAS) == {close.day: close.close for close in KAS_CLOSES}
    assert {row.basis for row in rows} == {CLOSE}
    assert {row.source for row in rows} == {VENDOR}
    assert {row.currency for row in rows} == {USD}
    assert {row.recorded_at for row in rows} == {RUN_AT}
    assert report == BackfillReport(
        backfilled=(
            BackfilledPair(BTC, USD, days=3, first_day=FIRST, last_day=THIRD, source=VENDOR),
            BackfilledPair(KAS, USD, days=2, first_day=SECOND, last_day=THIRD, source=VENDOR),
        ),
        failed=(),
    )
    assert source.asked == [BTC_USD, KAS_USD], "every pair the source serves, sorted"


async def test_a_second_run_leaves_one_row_per_day_and_the_same_report(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Idempotent by construction: the timer can run daily for a year without bookkeeping."""
    first = await backfill(factory, FakeCloses())
    before = await rows_in(factory)

    second = await backfill(factory, FakeCloses())
    after = await rows_in(factory)

    assert second == first
    assert after == before
    assert len(after) == len(BTC_CLOSES) + len(KAS_CLOSES)


async def test_a_later_run_corrects_a_day_and_adds_the_new_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tomorrow's run writes yesterday's close and rewrites every earlier day it still serves."""
    await backfill(factory, FakeCloses(answers={BTC_USD: BTC_CLOSES[:2]}), [BTC_USD])
    corrected = (
        DailyClose(day=FIRST, close=Decimal("62000.1")),
        DailyClose(day=SECOND, close=Decimal("62501")),
        DailyClose(day=THIRD, close=Decimal("63010.5")),
    )

    await backfill(factory, FakeCloses(answers={BTC_USD: corrected}), [BTC_USD])

    assert closes_of(await rows_in(factory), BTC) == {
        FIRST: Decimal("62000.1"),
        SECOND: Decimal("62501"),
        THIRD: Decimal("63010.5"),
    }


async def test_a_close_replaces_the_observed_price_the_refresh_left_for_that_day(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R2 from the backfill's side: yesterday's last hourly price gives way to its close.

    And a day the backfill does not serve -- today, still trading -- keeps its `observed`
    price: the backfill replaces what it has a close for and touches nothing else.
    """
    today = THIRD + timedelta(1)
    async with factory() as session:
        btc = (await session.scalars(select(Asset.id).where(Asset.symbol == BTC))).one()
        history = PriceHistoryRepository(session)
        for day in (THIRD, today):
            await history.record(
                asset_id=btc,
                quote_currency=USD,
                day=day,
                amount=Decimal("1.5"),
                basis=OBSERVED,
                source="the-refresh",
                recorded_at=RUN_AT - timedelta(hours=2),
            )
        await session.commit()

    await backfill(factory, FakeCloses(), [BTC_USD])

    by_day = {row.day: row for row in await rows_in(factory) if row.symbol == BTC}
    assert (by_day[THIRD].basis, by_day[THIRD].amount) == (CLOSE, Decimal("63010.5"))
    assert (by_day[THIRD].source, by_day[THIRD].recorded_at) == (VENDOR, RUN_AT)
    assert (by_day[today].basis, by_day[today].amount) == (OBSERVED, Decimal("1.5"))


async def test_an_empty_answer_is_a_pair_backfilled_with_no_days(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pair with nothing committed yet: zero days, `None` bounds, and not a failure."""
    report = await backfill(factory, FakeCloses(answers={}))

    assert report.backfilled == (
        BackfilledPair(BTC, USD, days=0, first_day=None, last_day=None, source=VENDOR),
        BackfilledPair(KAS, USD, days=0, first_day=None, last_day=None, source=VENDOR),
    )
    assert report.failed == ()
    assert await rows_in(factory) == []


async def test_only_the_pairs_asked_for_are_asked_about(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An explicit list narrows the run; `None` is every pair the source serves."""
    source = FakeCloses()

    report = await backfill(factory, source, [KAS_USD])

    assert source.asked == [KAS_USD]
    assert [line.asset_symbol for line in report.backfilled] == [KAS]
    assert closes_of(await rows_in(factory), BTC) == {}


# --------------------------------------------------------------------------------------
# One pair failing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ProviderUnavailableError(VENDOR_PROSE), id="unavailable"),
        pytest.param(ProviderRateLimitedError(VENDOR_PROSE), id="rate limited"),
        pytest.param(ProviderResponseError(VENDOR_PROSE), id="an answer that cannot be trusted"),
    ],
)
async def test_a_vendor_failure_on_one_pair_is_a_line_and_the_other_pair_is_stored(
    factory: async_sessionmaker[AsyncSession],
    error: Exception,
) -> None:
    """The first pair fails, the second is still asked and stored, and the run returns.

    The report carries the error's class name and never its message: a vendor's prose is
    not something a report, a log line or a CLI transcript should repeat.
    """
    source = FakeCloses(raises={BTC_USD: error})

    report = await backfill(factory, source)

    assert report.failed == (FailedPair(BTC, USD, type(error).__name__, source=VENDOR),)
    assert report.backfilled == (
        BackfilledPair(KAS, USD, days=2, first_day=SECOND, last_day=THIRD, source=VENDOR),
    )
    assert source.asked == [BTC_USD, KAS_USD]
    rows = await rows_in(factory)
    assert closes_of(rows, BTC) == {}
    assert closes_of(rows, KAS) == {close.day: close.close for close in KAS_CLOSES}
    assert VENDOR_PROSE not in repr(report)


async def test_each_pair_is_committed_before_the_next_is_asked(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """One commit per pair: what BTC stored is on disk at the moment KAS is requested.

    Read through a second session from inside the source's call, so it sees only what was
    committed. A single commit at the end would show zero rows here -- and a crash or a
    failure in the second pair would then take the first pair's closes with it.
    """
    source = FakeCloses(on_ask=[BTC_USD, KAS_USD], factory=factory)

    await backfill(factory, source)

    assert source.committed_when_asked == {BTC_USD: 0, KAS_USD: len(BTC_CLOSES)}


async def test_an_unexpected_exception_still_leaves_the_earlier_pairs_committed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Not a `ProviderError`, so it is not a line in the report: it propagates, loud.

    What the first pair stored is already committed, which is the per-pair commit's point.
    """
    source = FakeCloses(raises={KAS_USD: RuntimeError("a bug, not a vendor")})

    with pytest.raises(RuntimeError, match="a bug, not a vendor"):
        await backfill(factory, source)

    rows = await rows_in(factory)
    assert closes_of(rows, BTC) == {close.day: close.close for close in BTC_CLOSES}
    assert closes_of(rows, KAS) == {}


# --------------------------------------------------------------------------------------
# Pairs nothing could answer cost no request
# --------------------------------------------------------------------------------------


async def test_a_pair_the_source_does_not_serve_is_unsupported_without_a_request(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """EUR is not a backfill pair: refused, reported, and the source is never asked about it."""
    source = FakeCloses()

    report = await backfill(factory, source, [(BTC, EUR), BTC_USD])

    assert source.asked == [BTC_USD]
    assert report.failed == (FailedPair(BTC, EUR, UNSUPPORTED_PAIR, source=VENDOR),)
    assert [line.quote_currency for line in report.backfilled] == [USD]


async def test_a_pair_whose_asset_has_no_row_is_unsupported_without_a_request(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A source serving an asset `assets` does not have: there is no `asset_id` to store under.

    Reported as unsupported rather than raised: the other pairs still go ahead.
    """
    xrp: PricePair = ("XRP", USD)
    source = FakeCloses(pairs=frozenset({BTC_USD, xrp}), answers={BTC_USD: BTC_CLOSES})

    report = await backfill(factory, source)

    assert source.asked == [BTC_USD]
    assert report.failed == (FailedPair("XRP", USD, UNSUPPORTED_PAIR, source=VENDOR),)
    assert [line.asset_symbol for line in report.backfilled] == [BTC]


async def test_the_report_is_sorted_by_pair_whatever_order_it_was_asked_in(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both tuples sorted, so two runs print the same transcript and a log can be compared."""
    source = FakeCloses(raises={KAS_USD: ProviderUnavailableError(VENDOR_PROSE)})

    report = await backfill(
        factory, source, [("XRP", USD), KAS_USD, (KAS, EUR), BTC_USD, (BTC, EUR)]
    )

    assert source.asked == [BTC_USD, KAS_USD], "asked in sorted order too"
    assert [(line.asset_symbol, line.quote_currency) for line in report.backfilled] == [BTC_USD]
    assert [(line.asset_symbol, line.quote_currency, line.error) for line in report.failed] == [
        (BTC, EUR, "UnsupportedPair"),
        (KAS, EUR, "UnsupportedPair"),
        (KAS, USD, "ProviderUnavailableError"),
        ("XRP", USD, "UnsupportedPair"),
    ]


async def test_asking_for_no_pairs_is_an_empty_report(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An explicit empty list asks nothing, distinct from `None`, which asks everything."""
    source = FakeCloses()

    report = await backfill(factory, source, [])

    assert report == BackfillReport(backfilled=(), failed=())
    assert source.asked == []


# --------------------------------------------------------------------------------------
# The shapes callers render
# --------------------------------------------------------------------------------------


def test_the_report_lines_are_frozen_and_carry_what_the_cli_and_the_log_render() -> None:
    """Pinned field sets, because `cli.backfill_prices` and `main._report_price_backfill`
    render these, and a field renamed without this failing quietly disappears from both.
    """
    assert set(BackfillReport.__dataclass_fields__) == {"backfilled", "failed"}
    assert set(BackfilledPair.__dataclass_fields__) == {
        "asset_symbol",
        "quote_currency",
        "days",
        "first_day",
        "last_day",
        "source",
    }
    assert set(FailedPair.__dataclass_fields__) == {
        "asset_symbol",
        "quote_currency",
        "error",
        "source",
    }
    assert (UNSUPPORTED_PAIR, NO_RECENT_CLOSE) == ("UnsupportedPair", "NoRecentClose")

    line = FailedPair(BTC, USD, UNSUPPORTED_PAIR, source=VENDOR)
    with pytest.raises((AttributeError, TypeError)):
        line.error = "something else"  # type: ignore[misc]


async def test_the_builder_wires_the_service_over_the_one_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`build_price_backfill_service` with its default clock: aware UTC, read once per run."""
    source = FakeCloses(answers={BTC_USD: BTC_CLOSES[:1]})
    before = datetime.now(UTC)

    async with factory() as session:
        service = build_price_backfill_service(session, source=source)
        assert isinstance(service, PriceBackfillService)
        await service.backfill([BTC_USD])

    (row,) = await rows_in(factory)
    assert row.recorded_at.tzinfo is not None
    assert before <= row.recorded_at <= datetime.now(UTC)


# --------------------------------------------------------------------------------------
# Spec 038: the older source, before the earliest stored close (R8)
# --------------------------------------------------------------------------------------


def days_from(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


async def test_the_older_source_fills_only_the_days_before_the_earliest_close(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Asked from its first day to the day before the recent source's first close, once.

    Stored as `close` under the older source's name; the recent source's three days keep
    their own numbers although the older table has different ones for them.
    """
    older = FakeOlder()

    report = await backfill(factory, FakeCloses(), older=older)

    assert older.asked == [(BTC_USD, OLDER_FIRST, FIRST - timedelta(days=1))]
    rows = [row for row in await rows_in(factory) if row.symbol == BTC]
    before = [row for row in rows if row.day < FIRST]
    assert [row.day for row in before] == days_from(OLDER_FIRST, FIRST - timedelta(days=1))
    assert {(row.basis, row.source, row.recorded_at) for row in before} == {(CLOSE, OLDER, RUN_AT)}
    assert {row.day: row.amount for row in before} == {
        day: amount for day, amount in older_table(OLDER_FIRST, THIRD).items() if day < FIRST
    }
    assert {row.day: (row.amount, row.source) for row in rows if row.day >= FIRST} == {
        close.day: (close.close, VENDOR) for close in BTC_CLOSES
    }
    assert report == BackfillReport(
        backfilled=(
            BackfilledPair(BTC, USD, days=3, first_day=FIRST, last_day=THIRD, source=VENDOR),
            BackfilledPair(
                BTC,
                USD,
                days=7,
                first_day=OLDER_FIRST,
                last_day=FIRST - timedelta(days=1),
                source=OLDER,
            ),
            BackfilledPair(KAS, USD, days=2, first_day=SECOND, last_day=THIRD, source=VENDOR),
        ),
        failed=(),
    )


async def test_once_filled_the_older_source_is_asked_nothing_and_has_no_line(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R8's last sentence: the second run asks it for nothing, and the rows do not move."""
    await backfill(factory, FakeCloses(), older=FakeOlder())
    before = await rows_in(factory)
    older = FakeOlder()

    report = await backfill(factory, FakeCloses(), older=older)

    assert older.asked == []
    assert [line.source for line in report.backfilled] == [VENDOR, VENDOR]
    assert report.failed == ()
    assert await rows_in(factory) == before


async def test_a_close_stored_before_the_older_sources_first_day_asks_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The earliest stored close already precedes what the older source has: nothing to add."""
    older = FakeOlder(earliest_day=FIRST + timedelta(days=1))

    report = await backfill(factory, FakeCloses(), older=older)

    assert older.asked == []
    assert {line.source for line in report.backfilled} == {VENDOR}
    assert report.failed == ()


async def test_the_older_range_ends_at_the_earliest_stored_close_not_at_todays_answer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A close stored by an earlier run, before anything the recent source said today, is
    where the older range stops: the earliest **stored** close, read from the table."""
    stored = date(2026, 10, 1)
    async with factory() as session:
        btc = (await session.scalars(select(Asset.id).where(Asset.symbol == BTC))).one()
        await PriceHistoryRepository(session).record(
            asset_id=btc,
            quote_currency=USD,
            day=stored,
            amount=Decimal("61000.5"),
            basis=CLOSE,
            source="an-earlier-run",
            recorded_at=RUN_AT - timedelta(days=30),
        )
        await session.commit()
    older = FakeOlder()

    await backfill(factory, FakeCloses(), older=older)

    assert older.asked == [(BTC_USD, OLDER_FIRST, stored - timedelta(days=1))]
    by_day = {row.day: row for row in await rows_in(factory) if row.symbol == BTC}
    assert (by_day[stored].source, by_day[stored].amount) == ("an-earlier-run", Decimal("61000.5"))


async def test_an_observed_price_is_not_a_close_to_extend_back_from(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only the refresh's `observed` price is stored, and the recent source failed today:
    there is no close, so it is `NoRecentClose`, without a request."""
    async with factory() as session:
        btc = (await session.scalars(select(Asset.id).where(Asset.symbol == BTC))).one()
        await PriceHistoryRepository(session).record(
            asset_id=btc,
            quote_currency=USD,
            day=THIRD,
            amount=Decimal("1.5"),
            basis=OBSERVED,
            source="the-refresh",
            recorded_at=RUN_AT,
        )
        await session.commit()
    older = FakeOlder()
    source = FakeCloses(raises={BTC_USD: ProviderUnavailableError(VENDOR_PROSE)})

    report = await backfill(factory, source, older=older)

    assert older.asked == []
    assert report.failed == (
        FailedPair(BTC, USD, "ProviderUnavailableError", source=VENDOR),
        FailedPair(BTC, USD, NO_RECENT_CLOSE, source=OLDER),
    )


async def test_no_close_stored_is_no_recent_close_and_costs_no_request(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A fresh install whose recent source answered nothing for BTC: no anchor for R8.

    A failed line rather than a silent skip -- the history is short and the operator can
    see why -- and not a request up to today, whose answer the recent source would only
    overwrite. The pair the recent source answered is unaffected.
    """
    older = FakeOlder()

    report = await backfill(factory, FakeCloses(answers={KAS_USD: KAS_CLOSES}), older=older)

    assert older.asked == []
    assert report.failed == (FailedPair(BTC, USD, NO_RECENT_CLOSE, source=OLDER),)
    assert report.backfilled == (
        BackfilledPair(BTC, USD, days=0, first_day=None, last_day=None, source=VENDOR),
        BackfilledPair(KAS, USD, days=2, first_day=SECOND, last_day=THIRD, source=VENDOR),
    )
    assert closes_of(await rows_in(factory), BTC) == {}


async def test_the_older_source_still_fills_when_the_recent_one_fails_today(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Failure isolation: yesterday's run stored closes; today Kraken is down. The earliest
    stored close does not depend on today's answer, so the older range is filled anyway."""
    await backfill(factory, FakeCloses())
    older = FakeOlder()
    source = FakeCloses(raises={BTC_USD: ProviderRateLimitedError(VENDOR_PROSE)})

    report = await backfill(factory, source, older=older)

    assert older.asked == [(BTC_USD, OLDER_FIRST, FIRST - timedelta(days=1))]
    assert report.failed == (FailedPair(BTC, USD, "ProviderRateLimitedError", source=VENDOR),)
    assert [(line.asset_symbol, line.source, line.days) for line in report.backfilled] == [
        (BTC, OLDER, 7),
        (KAS, VENDOR, 2),
    ]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ProviderUnavailableError(VENDOR_PROSE), id="unavailable"),
        pytest.param(ProviderRateLimitedError(VENDOR_PROSE), id="rate limited"),
        pytest.param(ProviderResponseError(VENDOR_PROSE), id="an answer that cannot be trusted"),
    ],
)
async def test_the_older_source_failing_is_its_own_line_and_takes_nothing_with_it(
    factory: async_sessionmaker[AsyncSession],
    error: Exception,
) -> None:
    """The recent closes are stored and committed; the older range stores nothing, and the
    next run asks for all of it again."""
    older = FakeOlder(raises={BTC_USD: error})

    report = await backfill(factory, FakeCloses(), older=older)

    assert report.failed == (FailedPair(BTC, USD, type(error).__name__, source=OLDER),)
    assert [line.source for line in report.backfilled] == [VENDOR, VENDOR]
    rows = await rows_in(factory)
    assert {row.source for row in rows} == {VENDOR}
    assert len(rows) == len(BTC_CLOSES) + len(KAS_CLOSES)
    assert VENDOR_PROSE not in repr(report)

    retry = FakeOlder()
    await backfill(factory, FakeCloses(), older=retry)
    assert retry.asked == [(BTC_USD, OLDER_FIRST, FIRST - timedelta(days=1))]


async def test_an_older_source_with_nothing_in_the_range_is_a_line_with_no_days(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Asked, answered with nothing: zero days and `None` bounds, as for the recent source."""
    older = FakeOlder(table={})

    report = await backfill(factory, FakeCloses(), older=older)

    assert len(older.asked) == 1
    assert (
        BackfilledPair(BTC, USD, days=0, first_day=None, last_day=None, source=OLDER)
        in report.backfilled
    )
    assert report.failed == ()


async def test_the_older_source_is_asked_only_about_requested_pairs_it_serves(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An explicit KAS-only run does not touch BTC's past, although the older source serves
    BTC; and a pair the older source does not serve -- KAS, by default -- is never asked."""
    serves_both = FakeOlder(pairs=frozenset({BTC_USD, KAS_USD}), table={})
    serves_btc = FakeOlder()

    await backfill(factory, FakeCloses(), [KAS_USD], older=serves_both)
    await backfill(factory, FakeCloses(), older=serves_btc)

    assert serves_both.asked == [(KAS_USD, OLDER_FIRST, SECOND - timedelta(days=1))]
    assert [asked[0] for asked in serves_btc.asked] == [BTC_USD]


async def test_a_pair_whose_asset_has_no_row_is_reported_once_and_not_asked_of_either(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """XRP has no `assets` row: `UnsupportedPair` from the recent source, and nothing else."""
    xrp: PricePair = ("XRP", USD)
    source = FakeCloses(pairs=frozenset({BTC_USD, xrp}), answers={BTC_USD: BTC_CLOSES})
    older = FakeOlder(pairs=frozenset({BTC_USD, xrp}))

    report = await backfill(factory, source, older=older)

    assert [asked[0] for asked in older.asked] == [BTC_USD]
    assert report.failed == (FailedPair("XRP", USD, UNSUPPORTED_PAIR, source=VENDOR),)


async def test_an_unexpected_exception_from_the_older_source_propagates_after_the_commits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Not a `ProviderError`: loud, and what the recent source stored is already on disk."""
    older = FakeOlder(raises={BTC_USD: RuntimeError("a bug, not a vendor")})

    with pytest.raises(RuntimeError, match="a bug, not a vendor"):
        await backfill(factory, FakeCloses(), older=older)

    assert len(await rows_in(factory)) == len(BTC_CLOSES) + len(KAS_CLOSES)


async def test_without_an_older_source_the_backfill_is_spec_037s(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The builder's default: no older source, no `NoRecentClose`, nothing before `FIRST`."""
    async with factory() as session:
        service = build_price_backfill_service(session, source=FakeCloses(), clock=_clock)
        report = await service.backfill()

    assert {line.source for line in report.backfilled} == {VENDOR}
    assert report.failed == ()
    assert min(row.day for row in await rows_in(factory)) == FIRST
