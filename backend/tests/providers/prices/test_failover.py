"""Criterion 6 of #9: failover is exercised -- and the row says who actually answered.

Two halves, and the second is the one #8's closing note is about. Proving that
`fetch_prices` moves to the next source when the first fails, and separately proving that
`PriceRepository.upsert` stores a `source`, leaves the wiring between them deletable with
the suite green. A `prices.source` column reading `kraken` when Coinbase supplied the number
is a record nobody can audit, and no assertion made against a `PriceQuote` in memory can
see it.

So `test_the_stored_source_is_the_one_that_actually_answered` drives the whole path:
scripted vendors, the **shipped** source order out of `price_sources`, the refresh service,
the repository, and then the row read back through a second identity. Nothing in it is a
stub except the vendors themselves.

## The loop's rule, and why it is more forgiving than `EndpointSet`'s

`EndpointSet` fails over between two instances of the *same* software against the *same*
chain, so a partial answer there is a correlation bug. Here the sources are four unrelated
vendors: Kraken lists all four pairs, Coinbase lists two, the Kaspa endpoint lists one. A
partial answer is the normal case, and the rule is one sentence -- **every failure to answer
moves to the next source, and a source that answers some of what it was asked leaves the
rest to the one behind it.**

The other difference matters just as much: **the last failure is not raised.** A portfolio
with three prices and one missing is a real, reportable state, and an exception would throw
away the three. The missing one travels in `unanswered` and becomes a reason rather than a
zero, which is criterion 3 and criterion 6 meeting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import select

from portfolio.db.models import Asset, AssetPrice
from portfolio.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.prices.base import (
    BTC,
    EUR,
    KAS,
    SUPPORTED_PAIRS,
    USD,
    PriceQuote,
    fetch_prices,
)
from portfolio.providers.prices.coinbase import COINBASE
from portfolio.providers.prices.kaspa import KASPA
from portfolio.providers.prices.kraken import KRAKEN
from portfolio.providers.prices.registry import price_sources
from portfolio.services.price_refresh import build_price_refresh_service
from portfolio.services.prices import Holding, PriceUnavailable, build_price_service
from tests.providers.prices.harness import (
    COINBASE_HOST,
    KASPA_PRICE_BODY,
    KASPA_PRICE_DIGITS,
    KRAKEN_HOST,
    KRAKEN_PRICES,
    PriceFake,
    Reply,
    ScriptedVendor,
    coinbase_body,
    kraken_echo,
    price_client,
    price_settings,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.providers.prices.base import PricePair, PriceSource

#: A clock that never moves. Nothing in this module is about time -- staleness is
#: `tests/services/test_prices_service.py`'s subject -- so the refresh service is given one
#: instant rather than a real clock whose value no assertion here reads. Injected rather
#: than defaulted, because a default would make every row's `as_of` depend on when the
#: suite ran.
REFRESHED_AT: Final = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return REFRESHED_AT


BTC_USD: Final[PricePair] = (BTC, USD)
BTC_EUR: Final[PricePair] = (BTC, EUR)
KAS_USD: Final[PricePair] = (KAS, USD)
KAS_EUR: Final[PricePair] = (KAS, EUR)

ONE: Final = Decimal("1.5")
TWO: Final = Decimal("2.5")

#: The Coinbase spot amount used wherever Coinbase is the source that answers. Deliberately
#: not equal to the Kraken figure for the same pair, so "which source answered" is visible
#: in the *amount* as well as in the `source` column -- two independent witnesses to one
#: fact, and a test that agreed with only one of them would not be saying much.
COINBASE_BTC_USD: Final = "85999.90"


@dataclass
class FakeSource:
    """A price source that answers, refuses, or answers part of what it was asked.

    Satisfies `PriceSource` structurally; the assignment at the bottom of this module is
    what `mypy --strict` decides, the same mechanism the real sources use.

    `asked` is the interesting field. "Kraken answered, so Coinbase was never called" and
    "both were called and Kraken's answer won" produce identical quotes, and only the call
    log tells them apart -- which is the difference between a measured budget of one request
    an hour and one of three.
    """

    name: str
    pairs: frozenset[PricePair]
    answers: dict[PricePair, Decimal] = field(default_factory=dict)
    raises: BaseException | None = None
    asked: list[tuple[PricePair, ...]] = field(default_factory=list)

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        self.asked.append(tuple(pairs))
        if self.raises is not None:
            raise self.raises
        return tuple(
            PriceQuote(
                asset_symbol=pair[0],
                quote_currency=pair[1],
                amount=amount,
                source=self.name,
            )
            for pair in pairs
            if (amount := self.answers.get(pair)) is not None
        )


@dataclass
class VolunteeringSource(FakeSource):
    """A source that answers about pairs it was **not** asked for. A misbehaving vendor.

    A cached response for another caller, a mis-keyed lookup, a paging bug: the shapes
    differ and they all arrive here as a quote for a pair nobody requested. Every real
    parser refuses this for its own document, so the only way to drive the loop's own check
    is a source that has no parser in front of it.
    """

    volunteers: dict[PricePair, Decimal] = field(default_factory=dict)

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        answered = list(await super().fetch(pairs))
        answered.extend(
            PriceQuote(
                asset_symbol=pair[0],
                quote_currency=pair[1],
                amount=amount,
                source=self.name,
            )
            for pair, amount in self.volunteers.items()
        )
        return tuple(answered)


# --------------------------------------------------------------------------------------
# The loop: a failure moves on, a success stops
# --------------------------------------------------------------------------------------


async def test_a_failed_primary_falls_over_to_the_next_source() -> None:
    """The primary refuses, the secondary answers, and the quote names the secondary.

    Asserted on the call log as well as on the quote. A loop that asked both and preferred
    the second would produce the same quote; only `asked` says that the primary was tried
    first and that the secondary was tried *because* it failed.
    """
    primary = FakeSource(
        name="primary",
        pairs=SUPPORTED_PAIRS,
        raises=ProviderUnavailableError("the vendor did not answer"),
    )
    secondary = FakeSource(name="secondary", pairs=SUPPORTED_PAIRS, answers={BTC_USD: TWO})

    result = await fetch_prices([BTC_USD], [primary, secondary])

    assert result.unanswered == ()
    assert [quote.source for quote in result.quotes] == ["secondary"]
    assert result.quotes[0].amount == TWO
    assert primary.asked == [(BTC_USD,)]
    assert secondary.asked == [(BTC_USD,)]


async def test_a_healthy_primary_means_the_next_source_is_never_called() -> None:
    """The budget, expressed as an absence. This is the whole of "720 requests a month".

    A loop that asked every source and took the first answer would return the same quotes,
    spend four times the requests, and pass every assertion about the result. The empty
    call log on the sources behind the primary is the only thing that says otherwise.
    """
    primary = FakeSource(name="primary", pairs=SUPPORTED_PAIRS, answers={BTC_USD: ONE})
    second = FakeSource(name="second", pairs=SUPPORTED_PAIRS, answers={BTC_USD: TWO})
    third = FakeSource(name="third", pairs=SUPPORTED_PAIRS, answers={BTC_USD: TWO})

    result = await fetch_prices([BTC_USD], [primary, second, third])

    assert [quote.source for quote in result.quotes] == ["primary"]
    assert primary.asked == [(BTC_USD,)]
    assert second.asked == []
    assert third.asked == []


async def test_a_partial_answer_leaves_the_rest_to_the_source_behind_it() -> None:
    """Two of four answered, two carried forward -- and the second source is asked for two.

    This is what makes the loop different from `EndpointSet`'s: there, a partial answer is a
    correlation bug, because both endpoints run the same software against the same chain.
    Here the sources are unrelated vendors and a partial answer is the ordinary case.

    The assertion that carries the weight is `second.asked`: a loop that re-asked for all
    four would pay for two answers it already had, every hour.
    """
    first = FakeSource(
        name="first",
        pairs=SUPPORTED_PAIRS,
        answers={BTC_USD: ONE, BTC_EUR: ONE},
    )
    second = FakeSource(
        name="second",
        pairs=SUPPORTED_PAIRS,
        answers={KAS_USD: TWO, KAS_EUR: TWO},
    )

    result = await fetch_prices(sorted(SUPPORTED_PAIRS), [first, second])

    assert {quote.pair: quote.source for quote in result.quotes} == {
        BTC_USD: "first",
        BTC_EUR: "first",
        KAS_USD: "second",
        KAS_EUR: "second",
    }
    assert result.unanswered == ()
    assert second.asked == [(KAS_EUR, KAS_USD)]


async def test_a_source_is_not_asked_about_a_pair_it_does_not_list() -> None:
    """Coinbase's measured 404 on KAS, as an absence of a request rather than a failure.

    A global failover chain would ask, pay a round trip, and move on -- hourly, forever, to
    rediscover something measured once. Declaring `pairs` on the source is what turns that
    into nothing at all, and this asserts the declaration is consulted.
    """
    btc_only = FakeSource(name="btc-only", pairs=frozenset({BTC_USD, BTC_EUR}))
    everything = FakeSource(
        name="everything",
        pairs=SUPPORTED_PAIRS,
        answers=dict.fromkeys(SUPPORTED_PAIRS, ONE),
    )

    await fetch_prices([KAS_USD, KAS_EUR], [btc_only, everything])

    assert btc_only.asked == [], "a source was asked about a pair it does not list"
    assert everything.asked == [(KAS_USD, KAS_EUR)]


async def test_every_source_failing_yields_no_quotes_and_names_the_pairs() -> None:
    """Nothing is raised. The pairs come back in `unanswered`, sorted, for the report.

    Raising the last failure would be the `EndpointSet` behaviour and would be wrong here:
    a refresh in which one pair failed and three succeeded must keep the three, and a
    refresh in which all four failed still has to produce a report rather than a traceback.
    """
    first = FakeSource(name="first", pairs=SUPPORTED_PAIRS, raises=ProviderUnavailableError("down"))
    second = FakeSource(name="second", pairs=SUPPORTED_PAIRS, raises=ProviderResponseError("junk"))

    result = await fetch_prices([KAS_USD, BTC_USD], [first, second])

    assert result.quotes == ()
    assert result.unanswered == (BTC_USD, KAS_USD)
    assert first.asked != []
    assert second.asked != []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ProviderUnavailableError("down"), id="unavailable"),
        pytest.param(ProviderRateLimitedError("slow down"), id="rate limited"),
        pytest.param(ProviderResponseError("not the documented envelope"), id="a bad body"),
        pytest.param(ProviderError("something else in the family"), id="the base class"),
    ],
)
async def test_every_kind_of_provider_failure_moves_to_the_next_source(
    failure: BaseException,
) -> None:
    """Including a refusal, which is the arm somebody would be tempted to treat differently.

    A 401 from a keyed source means that one key is wrong, not that the price is
    unknowable; a 404 from Coinbase for a pair it turns out not to list is exactly what the
    next source is for. Catching only "unavailable" would strand both behind a vendor that
    answered perfectly clearly.
    """
    failing = FakeSource(name="failing", pairs=SUPPORTED_PAIRS, raises=failure)
    healthy = FakeSource(name="healthy", pairs=SUPPORTED_PAIRS, answers={BTC_USD: ONE})

    result = await fetch_prices([BTC_USD], [failing, healthy])

    assert [quote.source for quote in result.quotes] == ["healthy"]


async def test_a_bug_in_a_source_is_not_swallowed() -> None:
    """A `ValueError` out of a source's own logic is not a vendor being a vendor.

    Swallowing it would turn a broken parser into a permanently missing price with nothing
    in any log saying why -- a refresh that reports `EVERY_SOURCE_FAILED` forever while the
    vendor is perfectly healthy, which sends an operator to read a status page instead of a
    traceback. The catch clause is `ProviderError` and this is what pins its narrowness.
    """
    broken = FakeSource(name="broken", pairs=SUPPORTED_PAIRS, raises=ValueError("a bug in here"))
    healthy = FakeSource(name="healthy", pairs=SUPPORTED_PAIRS, answers={BTC_USD: ONE})

    with pytest.raises(ValueError, match=r"a bug in here"):
        await fetch_prices([BTC_USD], [broken, healthy])

    assert healthy.asked == []


async def test_duplicate_pairs_collapse_and_no_sources_means_everything_unanswered() -> None:
    """Two edges in one test, because both are "the loop did nothing surprising".

    A duplicated pair must not be asked for twice -- a caller assembling pairs from a
    wallet table can produce one easily -- and an empty source list must not raise: it is
    the unkeyed-and-everything-disabled configuration, and it produces a report full of
    reasons rather than a traceback.
    """
    source = FakeSource(name="only", pairs=SUPPORTED_PAIRS, answers={BTC_USD: ONE})

    collapsed = await fetch_prices([BTC_USD, BTC_USD, BTC_USD], [source])
    nobody = await fetch_prices([BTC_USD, KAS_EUR], [])

    assert source.asked == [(BTC_USD,)]
    assert len(collapsed.quotes) == 1
    assert nobody.quotes == ()
    assert nobody.unanswered == (BTC_USD, KAS_EUR)


# --------------------------------------------------------------------------------------
# The correlation rule the protocol states, enforced in the loop
# --------------------------------------------------------------------------------------
#
# `PriceSource.fetch` says an answer about a pair nobody asked for is not allowed. All four
# parsers check their own documents, so nothing reaches this in the shipped configuration
# -- which is exactly why it needs a test: a rule enforced in four places is a rule one of
# them can drop, and the fifth source nobody has written yet is the one that would.
#
# `align_balances` makes the same argument one layer down and resolves it the same way: the
# shared step refuses, rather than each provider being trusted to.


async def test_a_source_that_answers_about_a_pair_nobody_asked_for_is_passed_over() -> None:
    """The whole response is discarded, not filtered -- and the source is treated as failed.

    Not filtered, because the quote that *was* asked for came out of the same document that
    gave the correlation away. Keeping it hides a paging mistake, or somebody else's cached
    answer, behind a price that still looks plausible -- which is the argument
    `align_balances` makes about an unrequested address.

    Not raised, because three pairs already obtained from an earlier source are real and a
    fourth vendor misbehaving is no reason to throw them away. So a misbehaving source
    behaves exactly like one that failed: its pairs move on.
    """
    volunteering = VolunteeringSource(
        name="volunteering",
        pairs=SUPPORTED_PAIRS,
        answers={BTC_USD: ONE},
        volunteers={KAS_EUR: TWO},
    )

    result = await fetch_prices([BTC_USD], [volunteering])

    assert result.quotes == (), "the requested quote came out of the same untrusted document"
    assert result.unanswered == (BTC_USD,)
    assert volunteering.asked == [(BTC_USD,)]


async def test_the_pair_a_volunteering_source_lost_is_answered_by_the_next_one() -> None:
    """A bad response costs that source, not the pair. This is what "passed over" means.

    A raise would have thrown away everything and an unconditional filter would have kept a
    document that had already shown it could not be trusted. Moving on leaves the pair
    outstanding for whoever is next, which turns a misbehaving vendor into an ordinary
    failover rather than an outage.
    """
    volunteering = VolunteeringSource(
        name="volunteering",
        pairs=SUPPORTED_PAIRS,
        answers={BTC_USD: ONE},
        volunteers={KAS_EUR: TWO},
    )
    honest = FakeSource(name="honest", pairs=SUPPORTED_PAIRS, answers={BTC_USD: TWO})

    result = await fetch_prices([BTC_USD], [volunteering, honest])

    assert [quote.source for quote in result.quotes] == ["honest"]
    assert result.quotes[0].amount == TWO
    assert result.unanswered == ()


async def test_quotes_already_obtained_from_an_earlier_source_survive_a_later_bad_one() -> None:
    """The reason the check returns rather than raises, shown rather than described.

    Three pairs answered by a healthy source, then a fourth source volunteering something
    nobody asked for. Everything already obtained is kept; only the pair the bad source was
    asked about goes unanswered. A raise here would discard three real prices because a
    vendor behind them misbehaved on a fourth.
    """
    healthy = FakeSource(
        name="healthy",
        pairs=frozenset({BTC_USD, BTC_EUR, KAS_USD}),
        answers={BTC_USD: ONE, BTC_EUR: ONE, KAS_USD: ONE},
    )
    volunteering = VolunteeringSource(
        name="volunteering",
        pairs=SUPPORTED_PAIRS,
        volunteers={("XRP", USD): TWO},
    )

    result = await fetch_prices(sorted(SUPPORTED_PAIRS), [healthy, volunteering])

    assert {quote.pair for quote in result.quotes} == {BTC_USD, BTC_EUR, KAS_USD}
    assert result.unanswered == (KAS_EUR,)


async def test_answering_about_fewer_pairs_than_asked_is_still_perfectly_fine() -> None:
    """The control, and it is the case that must not be caught by the check above.

    A partial answer is the normal case here -- Kraken lists four pairs, Coinbase two, the
    Kaspa endpoint one -- so a rule written as "the answer must match the request" rather
    than "the answer must be a subset of it" would break every real refresh. The two
    readings differ on exactly this input.
    """
    partial = FakeSource(
        name="partial",
        pairs=SUPPORTED_PAIRS,
        answers={BTC_USD: ONE},
    )

    result = await fetch_prices([BTC_USD, KAS_EUR], [partial])

    assert [quote.pair for quote in result.quotes] == [BTC_USD]
    assert result.unanswered == (KAS_EUR,)


# --------------------------------------------------------------------------------------
# Criterion 6's other half: the row says who answered
# --------------------------------------------------------------------------------------


async def test_the_stored_source_is_the_one_that_actually_answered(
    price_session: AsyncSession,
) -> None:
    """The join, end to end: scripted vendors, the shipped order, the service, the row.

    Kraken -- the primary, and the source that would answer this pair in every healthy
    refresh -- is scripted to fail. Coinbase answers, with a **different amount**, and what
    is asserted is the row read back out of SQLite through a second identity: its `source`
    column says `coinbase` and its `amount` is Coinbase's figure.

    Every piece between the two is the real one. `price_sources` builds the order, so a
    change to it is felt here; `PriceRefreshService` does the writing; `PriceRepository`
    does the upsert. If the four lines that carry `quote.source` into `upsert(source=...)`
    were deleted, this is the test that goes red -- and, as `test_kraken.py` and
    `test_coinbase.py` show, it would be the only one.
    """
    fake = PriceFake(
        kraken=ScriptedVendor(Reply(status=503)),
        coinbase=ScriptedVendor(
            Reply(body=coinbase_body(base=BTC, currency=USD, amount=COINBASE_BTC_USD))
        ),
    )
    client = price_client(fake)
    service = build_price_refresh_service(
        price_session,
        sources=price_sources(client, settings=price_settings()),
        clock=_fixed_clock,
    )

    async with client:
        report = await service.refresh_prices([BTC_USD])

    assert [(line.asset_symbol, line.quote_currency, line.source) for line in report.refreshed] == [
        (BTC, USD, COINBASE)
    ]
    assert report.unavailable == ()
    assert fake.counts[KRAKEN_HOST] >= 1, "the primary was actually tried"
    assert fake.counts[COINBASE_HOST] == 1

    price_session.expunge_all()
    stored = (await price_session.scalars(select(AssetPrice))).one()
    symbol = (await price_session.get_one(Asset, stored.asset_id)).symbol

    assert symbol == BTC
    assert stored.quote_currency == USD
    assert stored.source == COINBASE
    assert stored.amount == Decimal(COINBASE_BTC_USD)
    assert stored.source != KRAKEN


async def test_a_healthy_primary_is_what_the_row_records_and_costs_one_request(
    price_session: AsyncSession,
) -> None:
    """The control for the test above, and the measured budget in one assertion.

    Without it, "the stored source is Coinbase" would be satisfied by an implementation
    that always wrote `coinbase`. Here Kraken answers, every pair comes from it, the row
    says so -- and Coinbase, the Kaspa node and CoinGecko are never called at all, which is
    the 720-requests-a-month figure the spec rests on.
    """
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=kraken_echo())))
    client = price_client(fake)
    service = build_price_refresh_service(
        price_session,
        sources=price_sources(client, settings=price_settings()),
        clock=_fixed_clock,
    )

    async with client:
        report = await service.refresh_prices()

    assert {line.source for line in report.refreshed} == {KRAKEN}
    assert len(report.refreshed) == len(SUPPORTED_PAIRS)
    assert report.unavailable == ()
    assert fake.counts[KRAKEN_HOST] == 1, "one request per refresh is the whole budget"
    assert fake.counts[COINBASE_HOST] == 0

    price_session.expunge_all()
    stored = (await price_session.scalars(select(AssetPrice))).all()

    assert {row.source for row in stored} == {KRAKEN}
    assert len(stored) == len(SUPPORTED_PAIRS)


async def test_the_kaspa_endpoint_is_reached_only_when_the_two_above_it_cannot_answer(
    price_session: AsyncSession,
) -> None:
    """The source whose currency is a guess, used only when nothing that states one is left.

    Kraken fails and Coinbase does not list KAS at all, so KAS/USD falls to the endpoint
    whose body names no currency. The row records `kaspa`, which is what makes the
    assumption auditable after the fact rather than invisible: an operator looking at a
    `source` column full of `kaspa` knows the guess is in use.

    KAS/EUR has nowhere to fall and becomes a reason in the same report, which is the
    single-point-of-failure the Risks section names, observed.
    """
    fake = PriceFake(
        kraken=ScriptedVendor(Reply(status=503)),
        kaspa=ScriptedVendor(Reply(body=KASPA_PRICE_BODY)),
    )
    client = price_client(fake)
    service = build_price_refresh_service(
        price_session,
        sources=price_sources(client, settings=price_settings()),
        clock=_fixed_clock,
    )

    async with client:
        report = await service.refresh_prices([KAS_USD, KAS_EUR])

    assert [(line.asset_symbol, line.quote_currency, line.source) for line in report.refreshed] == [
        (KAS, USD, KASPA)
    ]
    unavailable = [
        (line.asset_symbol, line.quote_currency, line.reason) for line in report.unavailable
    ]
    assert unavailable == [(KAS, EUR, PriceUnavailable.EVERY_SOURCE_FAILED)]

    price_session.expunge_all()
    stored = (await price_session.scalars(select(AssetPrice))).one()

    assert stored.source == KASPA
    assert stored.quote_currency == USD
    # The digits the node sent, through the failover, the service, the column and back.
    assert stored.amount == Decimal(KASPA_PRICE_DIGITS)


async def test_every_source_failing_yields_every_source_failed_and_writes_nothing(
    price_session: AsyncSession,
) -> None:
    """A pair nobody answered is a reason in the report and **no row** in the table.

    The absence of a row is the assertion that matters. A refresh that wrote a zero, or
    that left a previous price in place while reporting the pair as refreshed, would both
    produce a total that reads as authoritative -- which is criterion 3's failure arriving
    through criterion 6's door.

    The lookup afterwards is what a renderer would do next, and it answers
    `NEVER_FETCHED` rather than a price: the two halves of the design meeting where they
    have to.
    """
    fake = PriceFake(
        kraken=ScriptedVendor(Reply(status=503)),
        coinbase=ScriptedVendor(Reply(status=503)),
        kaspa=ScriptedVendor(Reply(status=503)),
    )
    client = price_client(fake)
    service = build_price_refresh_service(
        price_session,
        sources=price_sources(client, settings=price_settings()),
        clock=_fixed_clock,
    )

    async with client:
        report = await service.refresh_prices([BTC_USD])

    assert report.refreshed == ()
    assert [line.reason for line in report.unavailable] == [PriceUnavailable.EVERY_SOURCE_FAILED]

    assert (await price_session.scalars(select(AssetPrice))).all() == []

    lookup = build_price_service(price_session, clock=_fixed_clock)
    assert await lookup.lookup_price(BTC, USD) is PriceUnavailable.NEVER_FETCHED


async def test_a_later_refresh_that_fails_leaves_the_previous_price_standing(
    price_session: AsyncSession,
) -> None:
    """A failed refresh is not a reason to forget what was already known.

    The alternative -- deleting a row whose refresh failed -- would turn one bad afternoon
    at a vendor into a portfolio that reports nothing, when what it actually has is an
    hour-old price. Staleness is the mechanism for saying so, and criterion 4 is what makes
    that honest rather than silent.

    Asserted with the amount and the source both unchanged, so a refresh that overwrote the
    row with something it invented is visible too.
    """
    healthy = PriceFake(kraken=ScriptedVendor(Reply(renderer=kraken_echo())))
    client = price_client(healthy)
    async with client:
        await build_price_refresh_service(
            price_session,
            sources=price_sources(client, settings=price_settings()),
            clock=_fixed_clock,
        ).refresh_prices([BTC_USD])

    broken = PriceFake(
        kraken=ScriptedVendor(Reply(status=503)),
        coinbase=ScriptedVendor(Reply(status=503)),
    )
    second_client = price_client(broken)
    async with second_client:
        report = await build_price_refresh_service(
            price_session,
            sources=price_sources(second_client, settings=price_settings()),
            clock=_fixed_clock,
        ).refresh_prices([BTC_USD])

    assert [line.reason for line in report.unavailable] == [PriceUnavailable.EVERY_SOURCE_FAILED]

    price_session.expunge_all()
    stored = (await price_session.scalars(select(AssetPrice))).one()

    assert stored.source == KRAKEN
    assert stored.amount == Decimal(KRAKEN_PRICES["XXBTZUSD"])


# --------------------------------------------------------------------------------------
# A price this application cannot store must not cost the refresh the pairs that worked
# --------------------------------------------------------------------------------------
#
# Both of these were blocking review findings, and they are two ends of one bound. Measured
# before either was fixed:
#
#     Decimal("1E+300")     beside three good pairs -> sqlalchemy.exc.StatementError
#                                                      out of refresh_prices, 0 rows written
#     Decimal("5E-13")      stored TEXT "0.000000000000"
#                           value_portfolio(1_000_000 KAS) -> total=0E-12 complete=True
#
# The first threw away three prices that had already been paid for. The second is worse,
# because nothing failed: a positive price became a zero, and `complete=True` told every
# renderer the total was whole. There is no absent row for a valuation to notice.
#
# Both are now refused by `require_price`, which puts them on the vendor error path that
# already exists -- the source is passed over, the other pairs are kept, and the offending
# pair falls to the next source or becomes a reason. These two tests drive that through the
# real sources and assert the rows, because every layer in between is where the old
# behaviour lived.


async def test_one_unstorable_price_does_not_cost_the_other_pairs_their_rows(
    price_session: AsyncSession,
) -> None:
    """Kraken sends one impossible number among four; three pairs are still written.

    `1e300` is a well-formed string in a well-formed ticker envelope, so nothing above the
    parser can tell it from a price. Refused there, it costs Kraken the whole response --
    which is `parse_ticker`'s existing rule for a body carrying a value that cannot be
    trusted -- and the pairs fall through to the sources behind it.

    The assertion that carries this is the **row count**. The old behaviour produced zero
    rows and an exception; the new one produces three rows and a report. Asserting only
    that no exception was raised would pass for a refresh that quietly wrote nothing.
    """
    fake = PriceFake(
        kraken=ScriptedVendor(
            Reply(renderer=kraken_echo(dict(KRAKEN_PRICES) | {"KASUSD": "1e300"}))
        ),
        coinbase=ScriptedVendor(
            Reply(
                renderer=lambda request: coinbase_body(
                    base=BTC,
                    currency=request.url.path.removesuffix("/spot")[-3:],
                    amount=COINBASE_BTC_USD,
                )
            )
        ),
        kaspa=ScriptedVendor(Reply(body=KASPA_PRICE_BODY)),
    )
    client = price_client(fake)
    service = build_price_refresh_service(
        price_session,
        sources=price_sources(client, settings=price_settings()),
        clock=_fixed_clock,
    )

    async with client:
        report = await service.refresh_prices()

    assert [(line.asset_symbol, line.quote_currency) for line in report.refreshed] == [
        (BTC, EUR),
        (BTC, USD),
        (KAS, USD),
    ]
    assert [(line.asset_symbol, line.quote_currency) for line in report.unavailable] == [(KAS, EUR)]

    price_session.expunge_all()
    stored = (await price_session.scalars(select(AssetPrice))).all()

    assert len(stored) == 3, "three pairs answered; one bad number must not discard them"
    assert {row.source for row in stored} == {COINBASE, KASPA}


async def test_a_price_that_would_round_away_never_becomes_a_complete_total(
    price_session: AsyncSession,
) -> None:
    """The blocking finding, end to end: no zero row, and no total claiming to be whole.

    Every source that can answer KAS/USD sends a price below the column's resolution, so
    the pair is genuinely unpriceable -- which is the honest outcome for a number this
    application cannot store. What matters is what a renderer is then told.

    Three assertions, in the order they matter:

    * **no row holds a zero.** A stored zero is the defect; its absence is the fix.
    * **`complete` is false and KAS is named.** This is the difference between a total that
      is short and a total that is short *and says so*, which is criterion 3 in one line.
    * **the total is the BTC holding alone.** Asserted last and never on its own: an
      implementation that silently dropped the Kaspa holding produces exactly this number,
      so it is only meaningful beside the flag.
    """
    vanishing = "0.0000000000005"
    fake = PriceFake(
        kraken=ScriptedVendor(
            Reply(renderer=kraken_echo(dict(KRAKEN_PRICES) | {"KASUSD": vanishing}))
        ),
        coinbase=ScriptedVendor(
            Reply(
                renderer=lambda request: coinbase_body(
                    base=BTC,
                    currency=request.url.path.removesuffix("/spot")[-3:],
                    amount=COINBASE_BTC_USD,
                )
            )
        ),
        kaspa=ScriptedVendor(Reply(body=f'{{"price": {vanishing}}}')),
    )
    client = price_client(fake)

    async with client:
        report = await build_price_refresh_service(
            price_session,
            sources=price_sources(client, settings=price_settings()),
            clock=_fixed_clock,
        ).refresh_prices()

    # The refresh knows *why*: it asked, and every source that could answer sent a number
    # this application cannot store.
    assert {
        (line.asset_symbol, line.quote_currency): line.reason for line in report.unavailable
    } == {
        (KAS, USD): PriceUnavailable.EVERY_SOURCE_FAILED,
        (KAS, EUR): PriceUnavailable.EVERY_SOURCE_FAILED,
    }

    price_session.expunge_all()
    stored = (await price_session.scalars(select(AssetPrice))).all()

    assert all(not row.amount.is_zero() for row in stored), (
        "a stored zero values every holding of that asset at nothing"
    )
    assert {row.quote_currency for row in stored} == {USD, EUR}
    assert len(stored) == 2, "only the two BTC pairs could be priced"

    value = await build_price_service(price_session, clock=_fixed_clock).value_portfolio(
        (
            Holding(asset_symbol=BTC, quantity=Decimal("0.5")),
            Holding(asset_symbol=KAS, quantity=Decimal("1000000")),
        ),
        quote_currency=USD,
    )

    assert value.complete is False
    assert [holding.asset_symbol for holding in value.unpriced] == [KAS]
    # **`NEVER_FETCHED`, not `EVERY_SOURCE_FAILED`, and the difference is the layer.** The
    # valuation reads the table and nothing else, so "there is no row" is the only thing it
    # can honestly say; the refresh, which made the calls, is where the vendor's failure is
    # recorded. The two vocabularies meet at the table and neither borrows the other's --
    # which is what the `services.prices` / `services.price_refresh` split is for, and what
    # `backend/.importlinter`'s price contract keeps true.
    assert value.unpriced[0].reason is PriceUnavailable.NEVER_FETCHED
    assert value.total == Decimal(COINBASE_BTC_USD) / 2


async def test_a_vanishing_price_from_one_source_still_falls_over_to_a_good_one(
    price_session: AsyncSession,
) -> None:
    """The control: refusing the number must not refuse the pair.

    Kraken sends a price that would round away and the Kaspa node sends the measured one.
    KAS/USD is priced -- by the source behind the bad number -- which is what makes the
    refusal a *failover* rather than a way of losing a pair. Without this, both tests above
    are satisfied by a bound that simply gave up on any pair it saw a bad price for.
    """
    fake = PriceFake(
        kraken=ScriptedVendor(
            Reply(renderer=kraken_echo(dict(KRAKEN_PRICES) | {"KASUSD": "0.0000000000005"}))
        ),
        coinbase=ScriptedVendor(
            Reply(
                renderer=lambda request: coinbase_body(
                    base=BTC,
                    currency=request.url.path.removesuffix("/spot")[-3:],
                    amount=COINBASE_BTC_USD,
                )
            )
        ),
        kaspa=ScriptedVendor(Reply(body=KASPA_PRICE_BODY)),
    )
    client = price_client(fake)

    async with client:
        report = await build_price_refresh_service(
            price_session,
            sources=price_sources(client, settings=price_settings()),
            clock=_fixed_clock,
        ).refresh_prices([KAS_USD])

    assert [(line.asset_symbol, line.source) for line in report.refreshed] == [(KAS, KASPA)]
    assert report.refreshed[0].amount == Decimal(KASPA_PRICE_DIGITS)


# --------------------------------------------------------------------------------------
# A source whose quote the column could not store
# --------------------------------------------------------------------------------------
#
# All four shipped sources build their quotes through `require_price`, so nothing reaches
# this guard in the shipped configuration. It is the guard against the fifth source --
# written by somebody who assembled a `PriceQuote` directly, which is exactly what a fake
# in this file does and exactly what the next provider author will do first.
#
# Measured here before it was closed: an unstorable `Decimal` travelled to
# `PriceRepository.upsert`, `NumericText` refused it, and the `ValueError` propagated out
# of `refresh_prices` rolling back **three pairs that had already succeeded**. That is the
# outcome this function's own docstring says must never happen, which is why the check
# belongs on the vendor error path rather than in the transaction.
#
# The same shape as `_answers_only_what_was_asked`, deliberately: the whole response is
# discarded and the source is passed over. `EVERY_SOURCE_FAILED` is the reason, and not a
# new enum member -- the source answered, but with nothing usable, so from the pair's point
# of view every eligible source failed to produce a price.


@dataclass
class BypassingSource(FakeSource):
    """A source that assembles `PriceQuote` directly, without `require_price`.

    Not a contrivance: it is what every fake in this suite does, and what a fifth source
    would do on its first draft. The four real ones run `require_price` while parsing, and
    this class is the shape of a source that does not -- which is the only way to reach the
    loop's own check.
    """

    unstorable: dict[PricePair, Decimal] = field(default_factory=dict)

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        answered = list(await super().fetch(pairs))
        answered.extend(
            PriceQuote(
                asset_symbol=pair[0],
                quote_currency=pair[1],
                amount=amount,
                source=self.name,
            )
            for pair, amount in self.unstorable.items()
            if pair in pairs
        )
        return tuple(answered)


#: One value for each arm of `require_price`, reached through a quote rather than through a
#: parsed field. Together they say the loop re-runs the whole rule rather than a copy of the
#: half somebody remembered.
UNSTORABLE: Final[tuple[Decimal, ...]] = (
    Decimal("1E+300"),
    Decimal("5E-13"),
    Decimal(0),
    Decimal(-1),
)


@pytest.mark.parametrize("amount", UNSTORABLE, ids=lambda value: f"{value}")
async def test_a_source_whose_price_the_column_cannot_hold_is_passed_over(
    amount: Decimal,
) -> None:
    """Every arm of `require_price`, through a quote, and the pair still gets a price.

    Four values: too large for the digits in front of the point, small enough to round away
    to nothing, zero, and negative. Parametrised rather than written as one case because
    the guard calls `require_price` itself instead of restating it -- and a guard that had
    restated only the magnitude half would pass a single-case test and let a zero through,
    which is the one value criterion 3 is quoted for.

    The honest source behind it is what makes this a failover rather than a loss: the pair
    is answered, by somebody else, and `unanswered` is empty.
    """
    bypassing = BypassingSource(
        name="bypassing",
        pairs=SUPPORTED_PAIRS,
        unstorable={BTC_USD: amount},
    )
    honest = FakeSource(name="honest", pairs=SUPPORTED_PAIRS, answers={BTC_USD: ONE})

    result = await fetch_prices([BTC_USD], [bypassing, honest])

    assert [quote.source for quote in result.quotes] == ["honest"]
    assert result.quotes[0].amount == ONE
    assert result.unanswered == ()
    assert bypassing.asked == [(BTC_USD,)], "it was asked; its answer was refused"


async def test_a_bypassing_source_alone_leaves_the_pair_unanswered() -> None:
    """Nothing behind it, so the pair comes back unanswered rather than carrying the value.

    The assertion that matters is `quotes == ()`. A loop that kept the quote and let the
    repository refuse it later would satisfy every assertion about `unanswered` and would
    be the defect: the value reaches a `flush()` that rolls back whatever else succeeded.
    """
    bypassing = BypassingSource(
        name="bypassing",
        pairs=SUPPORTED_PAIRS,
        unstorable={BTC_USD: Decimal("1E+300")},
    )

    result = await fetch_prices([BTC_USD], [bypassing])

    assert result.quotes == ()
    assert result.unanswered == (BTC_USD,)


async def test_the_whole_response_is_discarded_and_not_merely_the_unstorable_quote() -> None:
    """One bad amount costs the source both its pairs, the way a bad correlation does.

    The same decision `_answers_only_what_was_asked` makes and for a weaker reason, so it
    is worth stating: a source that produced one number the column cannot hold has shown
    that whatever built its quotes is not running the rule every other source runs, and the
    quote beside it came out of the same place.

    Asserted with a good quote in the same response, so "discarded" means discarded rather
    than "there was nothing else to keep".
    """
    bypassing = BypassingSource(
        name="bypassing",
        pairs=SUPPORTED_PAIRS,
        answers={BTC_EUR: ONE},
        unstorable={BTC_USD: Decimal("1E+300")},
    )

    result = await fetch_prices([BTC_USD, BTC_EUR], [bypassing])

    assert result.quotes == ()
    assert result.unanswered == (BTC_EUR, BTC_USD)


async def test_a_storable_amount_from_the_same_kind_of_source_is_kept() -> None:
    """The control, and without it every test above passes for "discard this fake".

    `BypassingSource` is a distinct class that assembles quotes by hand, so a guard keyed
    on anything about the source rather than about the amount would satisfy all three tests
    above. Here the same class answers with an ordinary price and is believed.
    """
    bypassing = BypassingSource(
        name="bypassing",
        pairs=SUPPORTED_PAIRS,
        unstorable={BTC_USD: Decimal("86000.10")},
    )

    result = await fetch_prices([BTC_USD], [bypassing])

    assert [quote.source for quote in result.quotes] == ["bypassing"]
    assert result.quotes[0].amount == Decimal("86000.10")
    assert result.unanswered == ()


async def test_a_bypassing_source_no_longer_costs_a_refresh_the_pairs_that_worked(
    price_session: AsyncSession,
) -> None:
    """The measurement, asserted as the fixed behaviour rather than as the defect.

    Before the guard: an honest pair and an unstorable one in the same refresh produced
    `builtins.ValueError` out of `refresh_prices` and **zero** rows -- the good pair rolled
    back with the bad one. After it: both pairs refreshed, nothing unavailable, two rows.

    Both pairs, because the unstorable quote is answered by the honest source behind the
    bypassing one; `test_a_bypassing_source_alone_through_a_refresh_writes_nothing` is the
    case where nobody can answer it. The absence of an exception is asserted implicitly by
    the test not raising, and explicitly by the row count -- a `ValueError` here would have
    left the table empty, which is the assertion that would have failed before.
    """
    bypassing = BypassingSource(
        name="bypassing",
        pairs=SUPPORTED_PAIRS,
        unstorable={BTC_USD: Decimal("1E+300")},
    )
    honest = FakeSource(
        name="honest",
        pairs=SUPPORTED_PAIRS,
        answers={BTC_USD: ONE, BTC_EUR: TWO},
    )
    service = build_price_refresh_service(
        price_session,
        sources=(bypassing, honest),
        clock=_fixed_clock,
    )

    report = await service.refresh_prices([BTC_USD, BTC_EUR])

    assert [(line.asset_symbol, line.quote_currency) for line in report.refreshed] == [
        (BTC, EUR),
        (BTC, USD),
    ]
    assert report.unavailable == ()
    assert {line.source for line in report.refreshed} == {"honest"}

    price_session.expunge_all()
    stored = (await price_session.scalars(select(AssetPrice))).all()

    assert len(stored) == 2, "the pair that worked must survive the pair that did not"


async def test_a_bypassing_source_alone_through_a_refresh_writes_nothing(
    price_session: AsyncSession,
) -> None:
    """Nobody can answer the pair, so it is a reason and an empty table -- not a zero row.

    `EVERY_SOURCE_FAILED` rather than a new enum member: the source answered, but with
    nothing usable, so from the pair's point of view every eligible source failed to
    produce a price. That reason sends an operator to look at the vendor, which is where
    the problem is, and the enum stays at the spec's four members.
    """
    bypassing = BypassingSource(
        name="bypassing",
        pairs=SUPPORTED_PAIRS,
        unstorable={BTC_USD: Decimal("5E-13")},
    )
    service = build_price_refresh_service(
        price_session,
        sources=(bypassing,),
        clock=_fixed_clock,
    )

    report = await service.refresh_prices([BTC_USD])

    assert report.refreshed == ()
    assert [(line.asset_symbol, line.reason) for line in report.unavailable] == [
        (BTC, PriceUnavailable.EVERY_SOURCE_FAILED)
    ]
    assert (await price_session.scalars(select(AssetPrice))).all() == []


# --------------------------------------------------------------------------------------
# Conformance, decided by mypy rather than by isinstance
# --------------------------------------------------------------------------------------
#
# `PriceSource` is deliberately not `@runtime_checkable`, so `isinstance(FakeSource(...),
# PriceSource)` would compare three attribute names and say nothing about whether `fetch`
# takes a sequence or is a coroutine function. This annotated assignment is the real check
# and `mypy --strict` decides it in the gate -- which also means the fake above is held to
# the same interface the four real sources are, rather than to whatever this file happened
# to call.

_CONFORMS: PriceSource = FakeSource(name="conforming", pairs=SUPPORTED_PAIRS)
