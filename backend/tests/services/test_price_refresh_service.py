"""The refresh service: which reason a pair gets, and what reaches the table.

`refresh_prices` is the only thing in this change that produces three of the four
`PriceUnavailable` members -- `lookup_price` can only ever say `NEVER_FETCHED`, because the
table is all it reads and "there is no row" is the only thing a table knows. So the
distinctions between them are decided here and nowhere else, and each one is a different
instruction to whoever reads it:

| Reason | What it means | Where to look |
|---|---|---|
| `UNSUPPORTED_PAIR` | nothing was asked; nothing could answer | this product's pair list |
| `NO_SOURCE_CONFIGURED` | a supported pair, no eligible source | the configuration |
| `EVERY_SOURCE_FAILED` | every eligible source was asked and failed | the vendors |

Reporting the first two as a failed fetch would be a lie about a call nobody made, and it
would send somebody to read a status page for a vendor that was never contacted.

**This module drives the service with fake sources**, so that the reasons can be produced
without scripting four vendors; `tests/providers/prices/test_failover.py` drives the same
service over the real sources and the scripted vendors, and is where the "which source
answered" join is proven. The split is deliberate and the two halves overlap on purpose:
this one is about the report, that one is about the row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import select, text

from portfolio.db.models import Asset, AssetPrice, PriceHistory
from portfolio.providers.errors import ProviderUnavailableError
from portfolio.providers.prices.base import BTC, EUR, KAS, SUPPORTED_PAIRS, USD, PriceQuote
from portfolio.repositories.price_history import CLOSE, OBSERVED, PriceHistoryRepository
from portfolio.services.price_refresh import (
    PriceRefreshService,
    RefreshedPair,
    RefreshReport,
    UnavailablePair,
    UnknownAssetError,
    build_price_refresh_service,
)
from portfolio.services.prices import Holding, PriceUnavailable, build_price_service

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.providers.prices.base import PricePair, PriceSource

REFRESHED_AT: Final = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

BTC_USD: Final[PricePair] = (BTC, USD)
BTC_EUR: Final[PricePair] = (BTC, EUR)
KAS_USD: Final[PricePair] = (KAS, USD)
KAS_EUR: Final[PricePair] = (KAS, EUR)

BTC_PRICE: Final = Decimal("86000.10000")
KAS_PRICE: Final = Decimal("0.04228645")

ALL_FOUR_PRICES: Final[dict[PricePair, Decimal]] = {
    BTC_USD: BTC_PRICE,
    BTC_EUR: Decimal("79000.34000"),
    KAS_USD: KAS_PRICE,
    KAS_EUR: Decimal("0.03885120"),
}

VENDOR: Final = "a-vendor"


def _clock() -> datetime:
    """One instant, injected, so `as_of` never depends on when the suite ran."""
    return REFRESHED_AT


@dataclass
class FakeSource:
    """A source that answers from a table, or refuses. Structural, checked by `mypy`."""

    name: str = VENDOR
    pairs: frozenset[PricePair] = field(default_factory=lambda: SUPPORTED_PAIRS)
    answers: dict[PricePair, Decimal] = field(default_factory=lambda: dict(ALL_FOUR_PRICES))
    raises: BaseException | None = None
    asked: list[tuple[PricePair, ...]] = field(default_factory=list)
    volunteers: dict[PricePair, Decimal] = field(default_factory=dict)
    """Quotes returned for pairs that were **not** asked for.

    A misbehaving vendor, in one field. `fetch_prices` passes a source's answers through
    without checking that they correspond to the request -- each parser enforces that for
    its own vendor's document -- so this is the shape in which an unrequested pair can
    still reach the refresh service, and the shape
    `test_a_quote_for_an_asset_with_no_row_is_loud_and_writes_nothing` needs to drive the
    one guard that stands behind it.
    """

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        self.asked.append(tuple(pairs))
        if self.raises is not None:
            raise self.raises
        answered = [
            (pair, amount) for pair in pairs if (amount := self.answers.get(pair)) is not None
        ]
        answered.extend(self.volunteers.items())
        return tuple(
            PriceQuote(
                asset_symbol=pair[0],
                quote_currency=pair[1],
                amount=amount,
                source=self.name,
            )
            for pair, amount in answered
        )


def service(
    session: AsyncSession,
    *sources: PriceSource,
) -> PriceRefreshService:
    """The refresh service as `build_price_refresh_service` assembles it."""
    return build_price_refresh_service(session, sources=sources, clock=_clock)


def reasons(report: RefreshReport) -> dict[PricePair, PriceUnavailable]:
    return {(line.asset_symbol, line.quote_currency): line.reason for line in report.unavailable}


# --------------------------------------------------------------------------------------
# The happy path, and what "no argument" means
# --------------------------------------------------------------------------------------


async def test_refreshing_with_no_argument_covers_every_supported_pair(
    service_session: AsyncSession,
) -> None:
    """`None` means all four, sorted -- which is what the scheduler and the CLI both want.

    Sorted rather than in set order, and that is not tidiness: `SUPPORTED_PAIRS` is a
    frozenset, so an unsorted request would send Kraken a different query string on every
    run. A vendor behind a cache would miss every time, and two logs would be impossible to
    compare.
    """
    source = FakeSource()
    report = await service(service_session, source).refresh_prices()

    assert source.asked == [tuple(sorted(SUPPORTED_PAIRS))]
    assert len(report.refreshed) == len(SUPPORTED_PAIRS)
    assert report.unavailable == ()
    assert report.as_of == REFRESHED_AT


async def test_every_refreshed_pair_reaches_the_table_with_its_source_and_amount(
    service_session: AsyncSession,
) -> None:
    """The report and the rows agree, field for field. Either alone could be the lie.

    A report is built in memory by the same method that writes the rows, so a report that
    agreed with itself would prove nothing about what is on disk. Reading the table back
    through a second identity and comparing the two is what makes the report trustworthy.
    """
    await service(service_session, FakeSource()).refresh_prices()
    service_session.expunge_all()

    rows = (await service_session.scalars(select(AssetPrice))).all()
    by_symbol = {
        ((await service_session.get_one(Asset, row.asset_id)).symbol, row.quote_currency): row
        for row in rows
    }

    assert {pair: row.amount for pair, row in by_symbol.items()} == ALL_FOUR_PRICES
    assert {row.source for row in rows} == {VENDOR}
    assert {row.as_of for row in rows} == {REFRESHED_AT}
    assert {row.fetched_at for row in rows} == {REFRESHED_AT}


async def test_the_whole_refresh_shares_one_clock_read(
    service_session: AsyncSession,
) -> None:
    """One instant on every row, so `as_of` describes the refresh rather than the write.

    Reading the clock per pair would make the four rows differ by microseconds, which is
    meaningless on its own and wrong in one specific way: staleness would then be a
    statement about how long the loop took rather than about how old the prices are.

    `as_of` and `fetched_at` are the same value today, and the assertion says so rather
    than checking each separately -- they diverge the day a vendor supplies a quote time,
    and none does.
    """
    report = await service(service_session, FakeSource()).refresh_prices()
    service_session.expunge_all()

    rows = (await service_session.scalars(select(AssetPrice))).all()

    assert {row.as_of for row in rows} == {report.as_of}
    assert all(row.as_of == row.fetched_at for row in rows)


async def test_a_second_refresh_replaces_the_rows_rather_than_adding_to_them(
    service_session: AsyncSession,
) -> None:
    """Hourly, forever. A refresh that appended would grow the table by four rows an hour.

    And the "current" price would become whichever row a query happened to return -- most
    likely the oldest, since nothing orders by time. The failure presents as a dashboard
    showing last Tuesday's number with nothing indicating anything is wrong.
    """
    later = FakeSource(answers={BTC_USD: Decimal("1.00000")}, pairs=frozenset({BTC_USD}))

    await service(service_session, FakeSource()).refresh_prices()
    await service(service_session, later).refresh_prices([BTC_USD])
    service_session.expunge_all()

    rows = (await service_session.scalars(select(AssetPrice))).all()
    btc = await service_session.scalar(
        text(
            "SELECT p.amount FROM prices p JOIN assets a ON a.id = p.asset_id "
            "WHERE a.symbol = 'BTC' AND p.quote_currency = 'USD'"
        )
    )

    assert len(rows) == len(SUPPORTED_PAIRS)
    assert btc == "1.000000000000"


async def test_the_report_is_sorted_so_two_runs_produce_the_same_transcript(
    service_session: AsyncSession,
) -> None:
    """A report is read by a person and diffed by a machine; a shuffled one is neither.

    Asserted over a request deliberately given in a scrambled order, so that a service
    which simply preserved the caller's order would fail.
    """
    scrambled = [KAS_EUR, BTC_USD, KAS_USD, BTC_EUR]

    report = await service(service_session, FakeSource()).refresh_prices(scrambled)

    assert [(line.asset_symbol, line.quote_currency) for line in report.refreshed] == sorted(
        scrambled
    )


# --------------------------------------------------------------------------------------
# Which reason, and why each one is a different instruction
# --------------------------------------------------------------------------------------


async def test_an_unsupported_pair_is_reported_without_anybody_being_asked(
    service_session: AsyncSession,
) -> None:
    """`UNSUPPORTED_PAIR`, and the source's call log is what proves "without being asked".

    Reporting this as `EVERY_SOURCE_FAILED` would send an operator to check the status of
    four vendors none of which was contacted. The supported pair in the same call is the
    control: one line each, and the unsupported one costs nothing.
    """
    source = FakeSource()

    report = await service(service_session, source).refresh_prices([("XRP", USD), BTC_USD])

    assert reasons(report) == {("XRP", USD): PriceUnavailable.UNSUPPORTED_PAIR}
    assert [line.asset_symbol for line in report.refreshed] == [BTC]
    assert source.asked == [(BTC_USD,)], "an unsupported pair must cost no request"


async def test_a_supported_pair_with_no_eligible_source_is_a_configuration_reason(
    service_session: AsyncSession,
) -> None:
    """`NO_SOURCE_CONFIGURED`, which is the branch no shipped configuration can reach.

    Kraken is key-free, always constructed, and lists all four pairs, so `sources_for`
    cannot return nothing for a supported pair unless the service was built with a source
    list that does not cover it -- which is exactly what a future configuration switch
    turning a source off would produce, and why `sources` is a required constructor
    argument with no default.

    Both shapes are exercised: no sources at all, and a source that covers a different pair.
    The second is the realistic one and the first is the one a reader would think of.
    """
    empty = await service(service_session).refresh_prices([BTC_USD])
    btc_only = FakeSource(pairs=frozenset({BTC_USD}))
    partial = await service(service_session, btc_only).refresh_prices([BTC_USD, KAS_EUR])

    assert reasons(empty) == {BTC_USD: PriceUnavailable.NO_SOURCE_CONFIGURED}
    assert empty.refreshed == ()
    assert reasons(partial) == {KAS_EUR: PriceUnavailable.NO_SOURCE_CONFIGURED}
    assert [line.asset_symbol for line in partial.refreshed] == [BTC]
    assert btc_only.asked == [(BTC_USD,)]


async def test_a_pair_every_source_refused_is_the_reason_that_means_look_at_a_vendor(
    service_session: AsyncSession,
) -> None:
    """`EVERY_SOURCE_FAILED` -- the only one of the three that is about somebody else.

    The distinction from the two above is the whole point of having three members: this is
    the reason that means a call was made and went wrong, and it is the only one where a
    vendor's status page is the right thing to read next.
    """
    down = FakeSource(raises=ProviderUnavailableError("the vendor did not answer"))

    report = await service(service_session, down).refresh_prices([BTC_USD])

    assert reasons(report) == {BTC_USD: PriceUnavailable.EVERY_SOURCE_FAILED}
    assert down.asked == [(BTC_USD,)], "the vendor really was asked"
    assert report.refreshed == ()


async def test_all_three_reasons_can_appear_in_one_report(
    service_session: AsyncSession,
) -> None:
    """A real refresh mixes them, and each pair keeps its own reason.

    A report that collapsed every failure into one verdict would be simpler and would tell
    an operator to look in one place for three different problems. This is the assertion
    that says the three remain distinguishable when they arrive together -- with a
    successful pair alongside them, so "some of it worked" is representable too.
    """
    btc_usd_only = FakeSource(
        pairs=frozenset({BTC_USD, KAS_USD}),
        answers={BTC_USD: BTC_PRICE},
    )

    report = await service(service_session, btc_usd_only).refresh_prices(
        [BTC_USD, KAS_USD, KAS_EUR, ("XRP", EUR)]
    )

    assert reasons(report) == {
        KAS_USD: PriceUnavailable.EVERY_SOURCE_FAILED,
        KAS_EUR: PriceUnavailable.NO_SOURCE_CONFIGURED,
        ("XRP", EUR): PriceUnavailable.UNSUPPORTED_PAIR,
    }
    assert [line.asset_symbol for line in report.refreshed] == [BTC]


async def test_a_pair_that_fails_leaves_no_row_at_all(
    service_session: AsyncSession,
) -> None:
    """Criterion 3 at the write: a failed pair is an absence, never a zero.

    A row written with `Decimal(0)` for a pair nobody could price is the exact failure the
    issue's third criterion describes -- a holding valued at nothing, in a total that reads
    as complete. The lookup afterwards is what a renderer would do next and it answers with
    a reason.
    """
    down = FakeSource(raises=ProviderUnavailableError("down"))

    await service(service_session, down).refresh_prices([BTC_USD])

    assert (await service_session.scalars(select(AssetPrice))).all() == []
    lookup = build_price_service(service_session, clock=_clock)
    assert await lookup.lookup_price(BTC, USD) is PriceUnavailable.NEVER_FETCHED


# --------------------------------------------------------------------------------------
# The join with the valuation service, which is where all of this is going
# --------------------------------------------------------------------------------------


async def test_a_refresh_is_what_makes_a_portfolio_valuable(
    service_session: AsyncSession,
) -> None:
    """Refresh, then value: the two services meeting over the table, with nothing stubbed.

    Neither service imports the other -- that is the whole point of the split, and the
    `import-linter` contract depends on it -- so the table is the only thing joining them.
    A test that exercised each separately would leave that join unobserved, which is #8's
    closing lesson in the shape it would take here.
    """
    await service(service_session, FakeSource()).refresh_prices()

    valuation = build_price_service(service_session, clock=_clock)
    value = await valuation.value_portfolio(
        (
            Holding(asset_symbol=BTC, quantity=Decimal("0.5")),
            Holding(asset_symbol=KAS, quantity=Decimal("1000")),
        ),
        quote_currency=USD,
    )

    assert value.complete is True
    assert value.total == Decimal("43042.33645")
    assert {holding.price.source for holding in value.valued} == {VENDOR}


async def test_a_partial_refresh_is_what_an_incomplete_total_is_made_of(
    service_session: AsyncSession,
) -> None:
    """The failure criterion 3 exists for, produced the way it would actually happen.

    Nobody constructs an unpriced holding by hand in production: it arrives because a
    refresh could not price one pair. This drives that -- a source covering BTC only, a
    portfolio holding both -- and asserts the total is marked incomplete and names KAS.

    The total is asserted **after** the completeness flag and the name, deliberately. An
    implementation that silently dropped the Kaspa holding produces exactly this number.
    """
    btc_only = FakeSource(pairs=frozenset({BTC_USD}), answers={BTC_USD: BTC_PRICE})
    await service(service_session, btc_only).refresh_prices([BTC_USD, KAS_USD])

    valuation = build_price_service(service_session, clock=_clock)
    value = await valuation.value_portfolio(
        (
            Holding(asset_symbol=BTC, quantity=Decimal("0.5")),
            Holding(asset_symbol=KAS, quantity=Decimal("1000")),
        ),
        quote_currency=USD,
    )

    assert value.complete is False
    assert [holding.asset_symbol for holding in value.unpriced] == [KAS]
    assert value.unpriced[0].reason is PriceUnavailable.NEVER_FETCHED
    assert value.total == Decimal("43000.05")


async def test_an_hour_old_refresh_is_what_makes_a_price_stale(
    service_session: AsyncSession,
) -> None:
    """Criterion 4's clock, driven from the write rather than from a row built by hand.

    `as_of` is set by the refresh, and staleness is decided against it at read time. This
    is the only test that exercises both ends with the real writer in between, which is
    what turns "computed from an injected clock" into a statement about the system.
    """
    await service(service_session, FakeSource()).refresh_prices([BTC_USD])

    fresh = build_price_service(service_session, clock=_clock)
    aged = build_price_service(
        service_session,
        clock=lambda: REFRESHED_AT + timedelta(hours=2),
    )

    first = await fresh.lookup_price(BTC, USD)
    second = await aged.lookup_price(BTC, USD)

    assert getattr(first, "stale", None) is False
    assert getattr(second, "stale", None) is True


# --------------------------------------------------------------------------------------
# Spec 037: every refresh also records today's `observed` price in `price_history`
# --------------------------------------------------------------------------------------

#: The UTC day of `REFRESHED_AT`, written out rather than derived from it.
REFRESHED_DAY: Final = date(2026, 9, 23)


async def history_rows(session: AsyncSession) -> dict[tuple[str, str, date], PriceHistory]:
    """Every `price_history` row by `(symbol, currency, day)`, read afresh from the table.

    `expunge_all` first, so what comes back is the table and not the objects the refresh's
    own identity map still holds.
    """
    session.expunge_all()
    result = await session.execute(
        select(PriceHistory, Asset.symbol).join(Asset, Asset.id == PriceHistory.asset_id)
    )
    return {(symbol, row.quote_currency, row.day): row for row, symbol in result.all()}


async def test_a_refresh_records_todays_observed_price_for_every_pair_it_stored(
    service_session: AsyncSession,
) -> None:
    """R2's other writer: one `observed` row per stored pair, for the UTC day of the refresh.

    The same amount, source and instant as the `prices` row, from the same clock read, so
    the history and the current price cannot disagree about when or what.
    """
    await service(service_session, FakeSource()).refresh_prices()

    rows = await history_rows(service_session)

    assert set(rows) == {(symbol, currency, REFRESHED_DAY) for symbol, currency in SUPPORTED_PAIRS}
    assert {
        (symbol, currency): row.amount for (symbol, currency, _day), row in rows.items()
    } == ALL_FOUR_PRICES
    assert {row.basis for row in rows.values()} == {OBSERVED}
    assert {row.source for row in rows.values()} == {VENDOR}
    assert {row.recorded_at for row in rows.values()} == {REFRESHED_AT}


async def test_a_later_refresh_the_same_day_replaces_the_observed_price(
    service_session: AsyncSession,
) -> None:
    """Hourly: the day keeps one row, holding the latest price seen, not the first."""
    later = REFRESHED_AT + timedelta(hours=5)
    await service(service_session, FakeSource()).refresh_prices([BTC_USD])
    moved = FakeSource(name="another-vendor", answers={BTC_USD: Decimal("86500.5")})

    await build_price_refresh_service(
        service_session, sources=(moved,), clock=lambda: later
    ).refresh_prices([BTC_USD])

    rows = await history_rows(service_session)
    assert list(rows) == [(BTC, USD, REFRESHED_DAY)]
    row = rows[(BTC, USD, REFRESHED_DAY)]
    assert (row.amount, row.basis, row.source) == (Decimal("86500.5"), OBSERVED, "another-vendor")
    assert row.recorded_at == later


async def test_a_refresh_never_overwrites_a_close_for_the_same_day(
    service_session: AsyncSession,
) -> None:
    """Criterion 2: the backfill's close stands, while the current price still refreshes.

    The two halves together: `prices` takes the new number -- the dashboard's now -- and
    `price_history` keeps the close, because an hourly price must never undo a final one.
    The other pairs, with no close that day, get their `observed` row as usual.
    """
    btc = (await service_session.scalars(select(Asset.id).where(Asset.symbol == BTC))).one()
    await PriceHistoryRepository(service_session).record(
        asset_id=btc,
        quote_currency=USD,
        day=REFRESHED_DAY,
        amount=Decimal("85000.25"),
        basis=CLOSE,
        source="kraken",
        recorded_at=REFRESHED_AT - timedelta(hours=1),
    )
    await service_session.commit()

    report = await service(service_session, FakeSource()).refresh_prices([BTC_USD, KAS_USD])

    rows = await history_rows(service_session)
    close = rows[(BTC, USD, REFRESHED_DAY)]
    assert (close.amount, close.basis, close.source) == (Decimal("85000.25"), CLOSE, "kraken")
    assert close.recorded_at == REFRESHED_AT - timedelta(hours=1)
    assert rows[(KAS, USD, REFRESHED_DAY)].basis == OBSERVED
    assert [line.amount for line in report.refreshed if line.asset_symbol == BTC] == [BTC_PRICE]
    current = await service_session.scalar(
        text(
            "SELECT p.amount FROM prices p JOIN assets a ON a.id = p.asset_id "
            "WHERE a.symbol = 'BTC' AND p.quote_currency = 'USD'"
        )
    )
    assert current == "86000.100000000000", "the current price refreshed regardless"


async def test_the_history_day_is_the_utc_date_of_the_clock_read(
    service_session: AsyncSession,
) -> None:
    """A clock reading 22:30 at UTC-5 is 03:30 the next day in UTC, and that is the day.

    The day a price belongs to is a UTC day (spec 037), so a clock handed in another zone --
    a test's, or a future caller's -- must not file the price under its local date.
    """
    eastern = timezone(timedelta(hours=-5))
    local_evening = datetime(2026, 9, 23, 22, 30, tzinfo=eastern)

    await build_price_refresh_service(
        service_session, sources=(FakeSource(),), clock=lambda: local_evening
    ).refresh_prices([BTC_USD])

    rows = await history_rows(service_session)
    assert list(rows) == [(BTC, USD, date(2026, 9, 24))]


async def test_a_pair_that_fails_records_no_history_either(
    service_session: AsyncSession,
) -> None:
    """A failed pair is an absence in `price_history` too, never a zero for the day."""
    down = FakeSource(raises=ProviderUnavailableError("down"))

    await service(service_session, down).refresh_prices([BTC_USD])

    assert await history_rows(service_session) == {}


# --------------------------------------------------------------------------------------
# The loud failure, and the shape of the report
# --------------------------------------------------------------------------------------


async def test_a_quote_for_an_asset_with_no_row_is_loud_and_writes_nothing(
    service_session: AsyncSession,
) -> None:
    """The `assets` row is missing from a database that should have it. Raise, commit nothing.

    This is not a vendor being a vendor, which is why it is the one condition in
    `refresh_prices` that raises rather than becoming a line in the report. Reporting it as
    an unavailable pair would file "this deployment's database is missing a seed row" under
    "the vendor was slow", and it would sit there while every KAS price silently stopped
    updating.

    **The cause driven here is the one that happens in production**: a database restored
    from before `0002_seed_assets`, or a row deleted by hand on the host. The other cause --
    a pair added to `SUPPORTED_PAIRS` without the migration that seeds its asset -- is a code
    change, caught the first time a refresh runs, and needs no test of its own because it
    produces this same raise.

    The two assertions after the raise are the half that matters operationally. The BTC
    price was fetched successfully in the same call and is **not** committed, so a refresh
    that hits this leaves the previous prices standing rather than writing a partial set
    with nothing recording which rows are new.
    """
    await service_session.execute(text("DELETE FROM assets WHERE symbol = 'KAS'"))
    await service_session.commit()

    with pytest.raises(UnknownAssetError) as caught:
        await service(service_session, FakeSource()).refresh_prices([BTC_USD, KAS_USD])

    assert "KAS" in str(caught.value)

    await service_session.rollback()
    assert (await service_session.scalars(select(AssetPrice))).all() == [], (
        "a refresh that raised must commit nothing, not even the pairs it did resolve"
    )
    assert (await service_session.scalars(select(PriceHistory))).all() == [], (
        "nor the history row it recorded for them"
    )


async def test_a_source_that_answers_about_a_pair_nobody_asked_for_contributes_nothing(
    service_session: AsyncSession,
) -> None:
    """The correlation refusal, seen from the service: the whole response is discarded.

    Not a filter. The quotes that *were* asked for came out of the same document that gave
    the correlation away, so keeping them hides a paging mistake or somebody else's cached
    answer behind prices that still look plausible -- the argument `align_balances` makes
    about an unrequested address, applied one layer up.

    The consequence at this level is that a misbehaving source is treated exactly like one
    that failed: its pairs go to the next source, and with none behind it they come back as
    `EVERY_SOURCE_FAILED` and no row is written. Asserted on the table as well as on the
    report, because "contributes nothing" is a claim about what was stored.
    """
    volunteering = FakeSource(volunteers={KAS_USD: Decimal("1")})

    report = await service(service_session, volunteering).refresh_prices([BTC_USD])

    assert report.refreshed == ()
    assert reasons(report) == {BTC_USD: PriceUnavailable.EVERY_SOURCE_FAILED}
    assert (await service_session.scalars(select(AssetPrice))).all() == []


async def test_a_source_behind_a_volunteering_one_still_answers(
    service_session: AsyncSession,
) -> None:
    """The other half: a bad response costs that source, not the pair.

    A raise would have thrown away everything; a filter would have kept a document that
    had already shown it could not be trusted. Passing over the source leaves the pair
    outstanding for whoever is next, which is what turns a misbehaving vendor into an
    ordinary failover rather than an outage.
    """
    volunteering = FakeSource(name="volunteering", volunteers={KAS_USD: Decimal("1")})
    honest = FakeSource(name="honest", answers={BTC_USD: BTC_PRICE})

    report = await service(service_session, volunteering, honest).refresh_prices([BTC_USD])

    assert [(line.asset_symbol, line.source) for line in report.refreshed] == [(BTC, "honest")]
    assert report.unavailable == ()
    assert volunteering.asked == [(BTC_USD,)]
    assert honest.asked == [(BTC_USD,)]


def test_the_report_is_frozen_and_its_lines_carry_what_a_reader_needs() -> None:
    """The field sets, pinned, because #10's scheduler and the CLI both render this.

    A report a caller can edit is a report of nothing in particular, and a field renamed
    without this test failing is a field that quietly disappears from whatever renders it.
    """
    assert set(RefreshReport.__dataclass_fields__) == {"as_of", "refreshed", "unavailable"}
    assert set(RefreshedPair.__dataclass_fields__) == {
        "asset_symbol",
        "quote_currency",
        "source",
        "amount",
    }
    assert set(UnavailablePair.__dataclass_fields__) == {
        "asset_symbol",
        "quote_currency",
        "reason",
    }

    line = RefreshedPair(asset_symbol=BTC, quote_currency=USD, source=VENDOR, amount=BTC_PRICE)
    with pytest.raises((AttributeError, TypeError)):
        line.amount = Decimal(0)  # type: ignore[misc]


async def test_refreshing_nothing_is_an_empty_report_rather_than_an_error(
    service_session: AsyncSession,
) -> None:
    """The empty page. An explicit empty list asks for nothing and gets nothing.

    Distinct from `None`, which means "everything" -- and the two are one character apart
    at a call site, so the difference is asserted rather than assumed.
    """
    source = FakeSource()

    report = await service(service_session, source).refresh_prices([])

    assert report.refreshed == ()
    assert report.unavailable == ()
    assert source.asked == []
    assert report.as_of == REFRESHED_AT


# --------------------------------------------------------------------------------------
# Conformance, decided by mypy rather than by isinstance
# --------------------------------------------------------------------------------------

_CONFORMS: PriceSource = FakeSource()
