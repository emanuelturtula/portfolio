"""Criteria 3, 4 and 8 of #9: a missing price is a reason, staleness is read-time, EUR is not USD.

Criterion 3 is the most important line in the issue -- *a portfolio silently showing 0 is
worse than one showing an error, because it is believed* -- and it decides the shape of
this whole module.

## Why `lookup_price` returning a reason is necessary and not sufficient

The failure the criterion describes is not an unavailable price. It is a **total** that
quietly omits a holding. A test asserting that `lookup_price("KAS", "USD")` answers
`NEVER_FETCHED` proves the lookup is honest and says nothing about what the number on the
dashboard will be, because nothing in that test ever computes one.

So the criterion-3 tests here build a **two-asset portfolio**, price one of them, and assert
on the object a renderer would actually receive. And the assertion that carries the weight
is `complete is False` together with the name of the asset that is missing -- never
`total == <the one price it found>` on its own, because that assertion is *satisfied by the
bug*: an implementation that drops the unpriced holding and reports a confident smaller
number passes it exactly.

`test_an_incomplete_total_is_indistinguishable_from_a_complete_one_without_the_flag` is
where that is made explicit: the same portfolio, valued twice, and the only thing that tells
the two numbers apart is the flag.

## Why the clock is injected and the threshold is both pinned and patched

Criterion 4's `stale` is `now - as_of > STALE_AFTER`, computed when the price is read. Two
different things have to be true and each is invisible to the other's test:

* the **shipped** threshold is one hour -- pinned as a literal, because a test that passes
  its own threshold can no longer observe the one production uses (#6's lesson, which cost
  a retry subsystem that could have shipped dead);
* and `STALE_AFTER` is the value the service actually consults -- proven by patching it and
  watching the verdict move, because a service carrying its own `timedelta(hours=1)` beside
  the constant satisfies every other assertion in this file.

Nothing here sleeps or reads a wall clock. A staleness test that waited would be a
measurement of the machine it ran on.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.db.models import AssetPrice
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.prices import PriceRepository
from portfolio.services import prices as prices_module
from portfolio.services.prices import (
    STALE_AFTER,
    Holding,
    PortfolioValue,
    Price,
    PriceService,
    PriceUnavailable,
    UnpricedHolding,
    ValuedHolding,
    build_price_service,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

#: The instant every price in this module was observed. Everything else is expressed as an
#: offset from it, so no assertion depends on what time the suite runs.
OBSERVED_AT: Final = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

USD: Final = "USD"
EUR: Final = "EUR"
BTC: Final = "BTC"
KAS: Final = "KAS"
KRAKEN: Final = "kraken"

#: The measured prices, as the characters the vendors sent. Public market data, so rule 3
#: permits them in full; no address and no key appears anywhere in this file.
BTC_USD: Final = Decimal("86000.10000")
BTC_EUR: Final = Decimal("79000.34000")
KAS_USD: Final = Decimal("0.04228645")

#: A two-asset portfolio. Two is the smallest number that can tell "the total omitted a
#: holding" apart from "the total is empty", which is the distinction criterion 3 is about.
HALF_A_BITCOIN: Final = Decimal("0.5")
A_THOUSAND_KAS: Final = Decimal("1000")

#: Written out by hand rather than computed in the test body. `0.5 * 86000.10000` is the
#: kind of arithmetic that is easy to re-derive the way the code derives it, and an
#: expectation computed that way is a copy of the implementation wearing an assertion's
#: clothes.
BTC_VALUE_USD: Final = Decimal("43000.05")
KAS_VALUE_USD: Final = Decimal("42.28645")
BOTH_VALUES_USD: Final = Decimal("43042.33645")

TWO_ASSETS: Final[tuple[Holding, ...]] = (
    Holding(asset_symbol=BTC, quantity=HALF_A_BITCOIN),
    Holding(asset_symbol=KAS, quantity=A_THOUSAND_KAS),
)


class MovableClock:
    """A clock a test moves by hand, returning an aware UTC datetime.

    Aware, because `UtcDateTime` hands `as_of` back aware and subtracting a naive datetime
    from an aware one raises `TypeError`. Movable, because criterion 4's whole claim is
    that the same stored row reads fresh and then stale without anything rewriting it, and
    a fixed clock cannot express "and then".
    """

    def __init__(self, now: datetime = OBSERVED_AT) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


async def store_price(
    session: AsyncSession,
    *,
    symbol: str,
    currency: str,
    amount: Decimal,
    as_of: datetime = OBSERVED_AT,
    source: str = KRAKEN,
) -> None:
    """Put one row in `prices`, through the repository, as a refresh would.

    Through the repository rather than through raw SQL on purpose: what this module is
    about is what the *service* does with a row, and a row inserted by hand could differ
    from a row the refresh writes in a way no assertion here would see.
    """
    assets = AssetRepository(session)
    asset = await assets.get_by_symbol(symbol)
    assert asset is not None, f"{symbol} is not a seeded asset"
    assert asset.id is not None
    await PriceRepository(session).upsert(
        asset_id=asset.id,
        quote_currency=currency,
        amount=amount,
        source=source,
        as_of=as_of,
        fetched_at=as_of,
    )
    await session.commit()


def service(session: AsyncSession, clock: MovableClock) -> PriceService:
    """The service exactly as `build_price_service` assembles it, with the clock injected."""
    return build_price_service(session, clock=clock)


def symbols(holdings: Sequence[ValuedHolding] | Sequence[UnpricedHolding]) -> list[str]:
    """The asset symbols of a `valued` or `unpriced` tuple, in the order given.

    The union rather than a structural type: `asset_symbol` is the one field the two share,
    and spelling it as a `Protocol` here would be a second declaration of a shape both
    dataclasses already have. `Sequence` is covariant, so this accepts either tuple.
    """
    return [holding.asset_symbol for holding in holdings]


# --------------------------------------------------------------------------------------
# The vocabulary, pinned before anything is asserted in terms of it
# --------------------------------------------------------------------------------------


def test_the_reasons_a_price_can_be_missing_are_the_four_the_spec_names() -> None:
    """The enum's members and their wire values, pinned against literals.

    Derived checks shrink along with the thing they describe: a test that looped over
    `PriceUnavailable` and asserted each member was a string would pass for an enum with
    one member left. These are the four reasons the design document argues for, and each
    one means something different to whoever reads it -- "we have never asked" is an
    operator's problem, "every source failed" is a vendor's, and "unsupported pair" is
    neither and will never fix itself.
    """
    assert {member.name for member in PriceUnavailable} == {
        "NEVER_FETCHED",
        "EVERY_SOURCE_FAILED",
        "UNSUPPORTED_PAIR",
        "NO_SOURCE_CONFIGURED",
    }
    # `.value` rather than comparing the member to a literal. A `StrEnum` member *is* a
    # `str` at run time, but mypy's `--strict-equality` does not treat the two as
    # overlapping and reports the comparison as one that can never be true -- so the
    # spelling that reads most naturally is the one the gate rejects.
    assert PriceUnavailable.NEVER_FETCHED.value == "never_fetched"
    assert PriceUnavailable.EVERY_SOURCE_FAILED.value == "every_source_failed"
    assert PriceUnavailable.UNSUPPORTED_PAIR.value == "unsupported_pair"
    assert PriceUnavailable.NO_SOURCE_CONFIGURED.value == "no_source_configured"
    # And it really is a `str` subclass, which is what makes these safe to serialise
    # straight into a JSON body when #10 and #11 expose them.
    assert all(isinstance(member, str) for member in PriceUnavailable)


def test_a_price_is_frozen_and_carries_no_stored_staleness() -> None:
    """The field set of `Price`, and the two names that must not be in the table.

    `stale` is a field on the value object and is **not** a column: a stored boolean would
    be wrong one second after it was written and would need a background job whose only
    purpose is to keep a derived field true. Asserting its absence from `AssetPrice` is
    what makes that a property of the schema rather than of the current implementation.
    """
    assert set(Price.__dataclass_fields__) == {
        "asset_symbol",
        "quote_currency",
        "amount",
        "source",
        "as_of",
        "stale",
    }

    columns = set(AssetPrice.__table__.c.keys())
    assert "stale" not in columns
    assert "is_stale" not in columns

    price = Price(
        asset_symbol=BTC,
        quote_currency=USD,
        amount=BTC_USD,
        source=KRAKEN,
        as_of=OBSERVED_AT,
        stale=False,
    )
    with pytest.raises((AttributeError, TypeError)):
        price.stale = True  # type: ignore[misc]


def test_the_shipped_stale_threshold_is_one_hour() -> None:
    """The shipped value, as a literal. Every other staleness test moves around it.

    A test that passed its own threshold to the service could no longer observe this one,
    so the number production ships is pinned here and nowhere else -- and
    `test_the_service_reads_the_shipped_threshold_rather_than_its_own_copy` proves that the
    service is the thing reading it.
    """
    assert timedelta(hours=1) == STALE_AFTER
    assert STALE_AFTER.total_seconds() == 3600


# --------------------------------------------------------------------------------------
# Criterion 3: a missing price is a reason, never a zero
# --------------------------------------------------------------------------------------


async def test_a_missing_price_is_a_reason_rather_than_a_zero(
    service_session: AsyncSession,
) -> None:
    """Criterion 3 at the lookup. `NEVER_FETCHED`, and nothing that can be added to a total.

    The assertion that matters is the last pair. A `Price` whose `amount` was
    `Decimal("0")` would satisfy "the lookup returned something"; what makes this honest is
    that the result is not a `Price` **at all**, so no caller can reach an `.amount` on it
    and no arithmetic can silently treat the absence as nothing.
    """
    lookup = service(service_session, MovableClock())

    result = await lookup.lookup_price(KAS, USD)

    assert result is PriceUnavailable.NEVER_FETCHED
    assert not isinstance(result, Price)
    assert not hasattr(result, "amount")


async def test_an_asset_with_no_row_at_all_is_the_same_reason_as_one_with_no_price(
    service_session: AsyncSession,
) -> None:
    """Two different facts, one reason -- and both are `NEVER_FETCHED` deliberately.

    A symbol with no row in `assets` and an asset with a row but no price are different
    situations, and a caller can do nothing different about either: neither is a number,
    and neither is a zero. Giving the first one `UNSUPPORTED_PAIR` would be tempting and
    wrong, because `lookup_price` reads the table and nothing else -- deciding what is
    *supported* means consulting `providers.prices.base`, and a read path that imported a
    price provider is precisely what `backend/.importlinter`'s contract forbids.

    So the reason says what this layer can honestly know: nobody has ever stored a price
    for this. Written as a test because the two arms are two lines apart in the service and
    a change to either one would otherwise be invisible.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    lookup = service(service_session, MovableClock())

    unknown_asset = await lookup.lookup_price("NOTACOIN", USD)
    known_asset_unpriced = await lookup.lookup_price(KAS, USD)
    known_asset_priced = await lookup.lookup_price(BTC, USD)

    assert unknown_asset is PriceUnavailable.NEVER_FETCHED
    assert known_asset_unpriced is PriceUnavailable.NEVER_FETCHED
    assert isinstance(known_asset_priced, Price)


def test_the_services_clock_is_aware_utc_and_is_the_default_nothing_injects() -> None:
    """The shipped clock, called -- because every other test in this file replaces it.

    A suite in which every test injects its own clock never observes the default at all,
    and the default is what production runs with. #6's lesson, and it cost a retry
    subsystem that could have shipped dead.

    Aware and UTC is the whole requirement: `as_of` comes back from `UtcDateTime` aware, and
    subtracting a naive datetime from an aware one raises `TypeError` -- which would at
    least be loud. A naive clock in the *other* direction is the quiet failure: two naive
    datetimes subtract happily and measure staleness against whatever timezone the
    Raspberry Pi is in.
    """
    now = prices_module.utc_now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    # And it is what `build_price_service` reaches for when nobody passes one.
    signature = inspect.signature(build_price_service)
    assert signature.parameters["clock"].default is prices_module.utc_now


async def test_a_service_built_without_a_clock_still_answers(
    service_session: AsyncSession,
) -> None:
    """The default clock, exercised end to end rather than only inspected.

    `stale` is not asserted here and deliberately so: with a real clock the only available
    assertion would be about how long the test took, which is a measurement of the machine.
    What is asserted is that the default is a usable clock at all -- a `None` default, or
    one returning a naive datetime, would raise `TypeError` inside the subtraction and this
    is the only test that would see it.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)

    result = await build_price_service(service_session).lookup_price(BTC, USD)

    assert isinstance(result, Price)
    assert result.amount == BTC_USD
    assert isinstance(result.stale, bool)


async def test_a_price_that_is_there_comes_back_with_its_digits_and_its_source(
    service_session: AsyncSession,
) -> None:
    """The control for the test above: a guard that refused everything would pass it.

    The amount is compared against the literal `Decimal("0.04228645")`. That is also the
    digit assertion: a value that had been through a binary `double` anywhere on this path
    would be `0.0422864500000000032...` and would not equal it, however it printed.
    """
    await store_price(service_session, symbol=KAS, currency=USD, amount=KAS_USD)
    lookup = service(service_session, MovableClock())

    result = await lookup.lookup_price(KAS, USD)

    assert isinstance(result, Price)
    assert result.asset_symbol == KAS
    assert result.quote_currency == USD
    assert result.amount == KAS_USD
    assert result.source == KRAKEN
    assert result.as_of == OBSERVED_AT


async def test_a_portfolio_with_one_unpriced_asset_reports_an_incomplete_total(
    service_session: AsyncSession,
) -> None:
    """Criterion 3 where it actually bites: a two-asset portfolio with one price.

    The order of these assertions is the argument. `complete is False` comes first, because
    it is the only one the bug cannot satisfy -- an implementation that silently drops the
    Kaspa holding produces exactly the total asserted three lines below it, and every
    assertion about that number would go green over a portfolio that had quietly lost a
    holding.

    The total is asserted too, because "incomplete" must not become a licence to return
    anything: what it could price, it priced correctly, and the sum is in Python and not in
    SQL.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    valuation = service(service_session, MovableClock())

    value = await valuation.value_portfolio(TWO_ASSETS, quote_currency=USD)

    assert value.complete is False
    assert symbols(value.unpriced) == [KAS]
    assert symbols(value.valued) == [BTC]
    assert value.total == BTC_VALUE_USD
    assert value.quote_currency == USD


async def test_an_incomplete_total_names_the_assets_it_could_not_price(
    service_session: AsyncSession,
) -> None:
    """Not just "something is missing" -- which one, and why, per holding.

    A boolean alone tells a renderer to show a warning and tells the person reading it
    nothing they can act on. The reason travels with the name because the two answers are
    different jobs: `NEVER_FETCHED` on one asset while another is priced means the refresh
    has never covered that pair, which is an operator's problem and not a vendor's.

    The quantity travels too, so that a renderer can say *what* is missing from the total
    rather than only that something is.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    valuation = service(service_session, MovableClock())

    value = await valuation.value_portfolio(TWO_ASSETS, quote_currency=USD)

    assert len(value.unpriced) == 1
    missing = value.unpriced[0]
    assert missing.asset_symbol == KAS
    assert missing.quantity == A_THOUSAND_KAS
    assert missing.reason is PriceUnavailable.NEVER_FETCHED


async def test_an_incomplete_total_is_indistinguishable_from_a_complete_one_without_the_flag(
    service_session: AsyncSession,
) -> None:
    """The whole of criterion 3 in one test: two valuations, and the flag is the difference.

    The same portfolio and the same quantities, valued once with one price in the table and
    once with both. Both totals are legal-looking numbers in the same currency with the
    same magnitude; nothing about `43000.05` announces that a thousand KAS is missing from
    it. If `complete` were dropped -- or stored as a constant `True`, which is the
    refactor that would do it -- this is the test that goes red, and it is the only one
    that could.
    """
    valuation = service(service_session, MovableClock())
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)

    partial = await valuation.value_portfolio(TWO_ASSETS, quote_currency=USD)

    await store_price(service_session, symbol=KAS, currency=USD, amount=KAS_USD)
    whole = await valuation.value_portfolio(TWO_ASSETS, quote_currency=USD)

    assert partial.total < whole.total
    assert partial.complete is False
    assert whole.complete is True
    assert whole.unpriced == ()
    assert whole.total == BOTH_VALUES_USD
    # And the sum really is the parts, so "complete" is not merely a flag someone set.
    assert whole.total == BTC_VALUE_USD + KAS_VALUE_USD
    assert [holding.value for holding in whole.valued] == [BTC_VALUE_USD, KAS_VALUE_USD]


async def test_a_portfolio_with_no_prices_at_all_is_incomplete_and_not_worth_zero(
    service_session: AsyncSession,
) -> None:
    """The case the criterion quotes: a total of zero that is believed.

    A total of `0` is what the arithmetic produces when nothing can be priced, and there is
    no other number it could produce. The point is that it never travels alone: `complete`
    is false and both holdings are named, so a renderer cannot show the zero as a figure.
    """
    valuation = service(service_session, MovableClock())

    value = await valuation.value_portfolio(TWO_ASSETS, quote_currency=USD)

    assert value.total == Decimal(0)
    assert value.complete is False
    assert symbols(value.unpriced) == [BTC, KAS]
    assert value.valued == ()


async def test_an_empty_portfolio_is_complete_and_worth_zero(
    service_session: AsyncSession,
) -> None:
    """The one zero that is a fact. A new owner with no wallets is an ordinary state.

    The contrast with the test above is the point of having both: the same `total` of zero
    means "nothing is held" here and "nothing could be priced" there, and `complete` is the
    only thing that distinguishes them. Making the empty portfolio an error path instead
    would make the first screen after sign-up a failure.
    """
    valuation = service(service_session, MovableClock())

    value = await valuation.value_portfolio((), quote_currency=USD)

    assert value.total == Decimal(0)
    assert value.complete is True
    assert value.valued == ()
    assert value.unpriced == ()


async def test_a_holding_in_an_asset_nobody_has_ever_seeded_is_unpriced_not_an_error(
    service_session: AsyncSession,
) -> None:
    """One unknown symbol must not cost the other holdings their prices.

    A `KeyError` out of the asset lookup would end the whole valuation, so a single row of
    junk in a wallet table would take the dashboard down. Reported as an unpriced holding,
    it costs exactly itself -- and the priced holding beside it is what proves the
    difference.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    valuation = service(service_session, MovableClock())
    holdings = (
        Holding(asset_symbol=BTC, quantity=HALF_A_BITCOIN),
        Holding(asset_symbol="NOTACOIN", quantity=Decimal("1")),
    )

    value = await valuation.value_portfolio(holdings, quote_currency=USD)

    assert symbols(value.valued) == [BTC]
    assert symbols(value.unpriced) == ["NOTACOIN"]
    assert value.complete is False
    assert value.total == BTC_VALUE_USD


def test_complete_is_derived_and_not_a_field_that_can_disagree() -> None:
    """`complete` is a property over `unpriced`, so the two cannot drift apart.

    A stored boolean beside a list is one fact in two places, and this is the fact where
    disagreement is the exact failure criterion 3 describes -- a total flagged complete
    while carrying a list of things it could not price. Asserted on the class rather than
    through a call, so it stays true for a `PortfolioValue` built anywhere.
    """
    assert "complete" not in PortfolioValue.__dataclass_fields__
    assert isinstance(PortfolioValue.complete, property)


# --------------------------------------------------------------------------------------
# Criterion 4: staleness is computed when the price is read, never stored
# --------------------------------------------------------------------------------------


async def test_a_price_older_than_an_hour_is_stale(service_session: AsyncSession) -> None:
    """An hour and a second after it was observed, with the row untouched."""
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    clock = MovableClock(OBSERVED_AT + STALE_AFTER + timedelta(seconds=1))

    result = await service(service_session, clock).lookup_price(BTC, USD)

    assert isinstance(result, Price)
    assert result.stale is True
    assert result.as_of == OBSERVED_AT
    assert result.amount == BTC_USD


async def test_a_price_read_within_the_hour_is_not_stale(service_session: AsyncSession) -> None:
    """Fifty-nine minutes later. The control: a `stale` hard-coded true would pass above."""
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    clock = MovableClock(OBSERVED_AT + timedelta(minutes=59))

    result = await service(service_session, clock).lookup_price(BTC, USD)

    assert isinstance(result, Price)
    assert result.stale is False


async def test_exactly_one_hour_old_is_not_yet_stale(service_session: AsyncSession) -> None:
    """The boundary, where `>` and `>=` differ and nothing else does.

    A price is stale when it is *older* than the threshold, so the instant it turns exactly
    one hour old it is still fresh. Written out because the two spellings are one character
    apart and both read correctly aloud, and because an hourly refresh lands on this
    boundary every single time it runs -- a `>=` would flag every freshly refreshed price
    as stale on the tick.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    clock = MovableClock(OBSERVED_AT + STALE_AFTER)

    result = await service(service_session, clock).lookup_price(BTC, USD)

    assert isinstance(result, Price)
    assert result.stale is False


async def test_the_same_row_is_fresh_then_stale_as_the_clock_moves(
    service_session: AsyncSession,
) -> None:
    """Criterion 4's interpretation, asserted as behaviour: read-time, and never stored.

    One row, written once, read three times as the clock advances -- and after all three
    reads the row on disk is byte for byte what it was. A stored `is_stale` boolean would
    have to be rewritten by something to make this pass, and nothing here writes.

    This is the test that makes "computed at read time" a property of the code rather than
    a sentence in a design document.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    clock = MovableClock(OBSERVED_AT + timedelta(minutes=1))
    reader = service(service_session, clock)

    first = await reader.lookup_price(BTC, USD)
    clock.advance(timedelta(minutes=58))
    second = await reader.lookup_price(BTC, USD)
    clock.advance(timedelta(minutes=2))
    third = await reader.lookup_price(BTC, USD)

    assert isinstance(first, Price)
    assert isinstance(second, Price)
    assert isinstance(third, Price)
    assert [first.stale, second.stale, third.stale] == [False, False, True]
    # Same row, same observation time, same amount, throughout. Nothing rewrote anything.
    assert first.as_of == second.as_of == third.as_of == OBSERVED_AT
    assert first.amount == second.amount == third.amount == BTC_USD


async def test_the_service_reads_the_shipped_threshold_rather_than_its_own_copy(
    service_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`STALE_AFTER` is consumed, not merely declared beside a duplicate literal.

    Every other staleness test is satisfied by a service carrying its own
    `timedelta(hours=1)`, with `STALE_AFTER` exported and read by nobody -- and
    `test_the_shipped_stale_threshold_is_one_hour` would keep asserting a constant that
    decided nothing. Shrinking the constant and watching a five-minute-old price turn stale
    is what closes that gap.

    Patched **on the module**, so this only passes if the service looks the value up when
    it is used. A service that had copied it into a default argument at import time would
    not move, which is the other shape of the same defect.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    clock = MovableClock(OBSERVED_AT + timedelta(minutes=5))
    reader = service(service_session, clock)

    before = await reader.lookup_price(BTC, USD)
    monkeypatch.setattr(prices_module, "STALE_AFTER", timedelta(minutes=1))
    after = await reader.lookup_price(BTC, USD)

    assert isinstance(before, Price)
    assert isinstance(after, Price)
    assert before.stale is False, "five minutes is inside the shipped one-hour threshold"
    assert after.stale is True, "the service is not reading STALE_AFTER"


async def test_a_stale_price_is_still_a_price_and_still_counts_toward_the_total(
    service_session: AsyncSession,
) -> None:
    """Stale is a flag on a number, not a fifth reason a price is unavailable.

    `PriceUnavailable` has four members and none of them is "stale", deliberately: an hour
    old is a statement about confidence, not about absence, and dropping the holding would
    turn a refresh that is slightly late into criterion 3's silently-smaller total -- the
    exact failure this issue exists to prevent, arriving through the door marked "being
    careful".

    So the holding is valued, the total includes it, `complete` stays true, and the flag
    travels on the price where a renderer can find it and say so.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    await store_price(service_session, symbol=KAS, currency=USD, amount=KAS_USD)
    clock = MovableClock(OBSERVED_AT + timedelta(days=1))

    value = await service(service_session, clock).value_portfolio(TWO_ASSETS, quote_currency=USD)

    assert value.complete is True
    assert value.unpriced == ()
    assert value.total == BOTH_VALUES_USD
    assert all(holding.price.stale is True for holding in value.valued)


# --------------------------------------------------------------------------------------
# Criterion 8: USD and EUR are both supported, and neither is derived from the other
# --------------------------------------------------------------------------------------


async def test_a_eur_value_never_comes_from_a_usd_price(
    service_session: AsyncSession,
) -> None:
    """Criterion 8's interpretation, and the one that has a failure mode worth testing.

    Valuing a EUR portfolio from a USD price and a cross rate introduces a second vendor's
    error into every number, silently. The refusal is what makes that impossible: with only
    a USD price stored, a EUR valuation reports the holding as **unpriced**, not as
    `86000.10 x 0.92`.

    The control is in the same test on purpose. Asserting only the refusal would pass for a
    service that could not read EUR at all, so the EUR row is then stored and the same call
    is made again -- and the number that comes back is the EUR one, which is deliberately
    nowhere near a plausible conversion of the USD one.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    valuation = service(service_session, MovableClock())
    holdings = (Holding(asset_symbol=BTC, quantity=HALF_A_BITCOIN),)

    refused = await valuation.value_portfolio(holdings, quote_currency=EUR)

    assert refused.complete is False
    assert symbols(refused.unpriced) == [BTC]
    assert refused.unpriced[0].reason is PriceUnavailable.NEVER_FETCHED
    assert refused.total == Decimal(0)
    assert refused.quote_currency == EUR

    await store_price(service_session, symbol=BTC, currency=EUR, amount=BTC_EUR)
    answered = await valuation.value_portfolio(holdings, quote_currency=EUR)

    assert answered.complete is True
    assert answered.total == Decimal("39500.17")
    assert answered.total != BTC_VALUE_USD
    assert answered.valued[0].price.amount == BTC_EUR


async def test_the_two_currencies_are_read_independently_from_one_table(
    service_session: AsyncSession,
) -> None:
    """Both stored, both read, and each valuation uses its own row and not the other's.

    The failure this catches is a read that ignores `quote_currency` and returns whichever
    row for the asset it finds first -- which would answer a EUR request with a USD price,
    correctly labelled EUR. Asserted in both directions in one test, because a read that
    always returned the *first* row would satisfy a one-directional assertion.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    await store_price(service_session, symbol=BTC, currency=EUR, amount=BTC_EUR)
    lookup = service(service_session, MovableClock())

    in_usd = await lookup.lookup_price(BTC, USD)
    in_eur = await lookup.lookup_price(BTC, EUR)

    assert isinstance(in_usd, Price)
    assert isinstance(in_eur, Price)
    assert in_usd.amount == BTC_USD
    assert in_eur.amount == BTC_EUR
    assert in_usd.quote_currency == USD
    assert in_eur.quote_currency == EUR
    assert in_usd.amount != in_eur.amount


# --------------------------------------------------------------------------------------
# Rule 2: the arithmetic is Decimal, in Python, and keeps every digit
# --------------------------------------------------------------------------------------


async def test_a_total_keeps_the_digits_a_float_would_round_away(
    service_session: AsyncSession,
) -> None:
    """The sum is exact, which is what `Decimal` is for and what a `double` is not.

    A thousand KAS at `0.04228645` is `42.28645` exactly. In IEEE-754 the same
    multiplication produces `42.286450000000005`, and a portfolio that adds a few thousand
    fills that way reports a total that is wrong in the last places and never says so.

    Asserted as a `Decimal` equality against a literal. Nothing in this test, and nothing
    in the path it exercises, is allowed to be a `float`.
    """
    await store_price(service_session, symbol=KAS, currency=USD, amount=KAS_USD)
    holdings = (Holding(asset_symbol=KAS, quantity=A_THOUSAND_KAS),)

    value = await service(service_session, MovableClock()).value_portfolio(
        holdings, quote_currency=USD
    )

    assert value.total == KAS_VALUE_USD
    assert isinstance(value.total, Decimal)
    assert value.valued[0].value == KAS_VALUE_USD
    assert value.valued[0].quantity == A_THOUSAND_KAS


async def test_the_total_is_the_sum_of_the_valued_holdings_and_of_nothing_else(
    service_session: AsyncSession,
) -> None:
    """The total agrees with the list beside it, which is the invariant a renderer assumes.

    A renderer shows the total and the breakdown together. If they disagree -- a holding
    counted twice, a holding in the list but not the sum -- the page contradicts itself and
    the person reading it has to decide which half to believe. Asserted over a portfolio
    with two holdings of very different magnitudes, so a dropped term is visible rather
    than lost in a rounding place.
    """
    await store_price(service_session, symbol=BTC, currency=USD, amount=BTC_USD)
    await store_price(service_session, symbol=KAS, currency=USD, amount=KAS_USD)

    value = await service(service_session, MovableClock()).value_portfolio(
        TWO_ASSETS, quote_currency=USD
    )

    assert value.total == sum((holding.value for holding in value.valued), Decimal(0))
    assert len(value.valued) == 2
