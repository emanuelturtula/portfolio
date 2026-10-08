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

    from portfolio.providers.prices.base import DailyCloseSource, PricePair

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


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path, name="backfill.db") as made:
        yield made


async def backfill(
    factory: async_sessionmaker[AsyncSession],
    source: FakeCloses,
    pairs: Sequence[PricePair] | None = None,
) -> BackfillReport:
    """One backfill over a session of its own, as the timer and the CLI run it."""
    async with factory() as session:
        service = build_price_backfill_service(session, source=source, clock=_clock)
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
            BackfilledPair(BTC, USD, days=3, first_day=FIRST, last_day=THIRD),
            BackfilledPair(KAS, USD, days=2, first_day=SECOND, last_day=THIRD),
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
        BackfilledPair(BTC, USD, days=0, first_day=None, last_day=None),
        BackfilledPair(KAS, USD, days=0, first_day=None, last_day=None),
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

    assert report.failed == (FailedPair(BTC, USD, type(error).__name__),)
    assert report.backfilled == (
        BackfilledPair(KAS, USD, days=2, first_day=SECOND, last_day=THIRD),
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
    assert report.failed == (FailedPair(BTC, EUR, "UnsupportedPair"),)
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
    assert report.failed == (FailedPair("XRP", USD, "UnsupportedPair"),)
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
    }
    assert set(FailedPair.__dataclass_fields__) == {"asset_symbol", "quote_currency", "error"}

    line = FailedPair(BTC, USD, "UnsupportedPair")
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
