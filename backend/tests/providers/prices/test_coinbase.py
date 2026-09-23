"""The Coinbase spot source: one request per pair, and the echoed pair is checked.

Two things about this source are worth more than the rest.

**It does not list KAS**, measured: a 404 on both `KAS-USD` and `KAS-EUR`. That fact lives
on the source as its `pairs` declaration rather than in a failover table, so an hourly
refresh never spends a round trip discovering it again. `test_registry.py` asserts the
consequence; what is asserted here is the declaration.

**The pair travels in the path**, which makes an intermediary cache one mis-keyed entry
away from answering a `BTC-EUR` request with a `BTC-USD` document. A price in the wrong
currency is not an error anybody downstream can see -- it is a plausible number attached to
the wrong holding, which is the shape of failure criterion 3 exists to refuse. So the
echoed `base` and `currency` are compared against what was asked, and that check is the
load-bearing part of this parser rather than a courtesy.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import ASSET_PRICE, ENDPOINT_EXTENSION
from portfolio.providers.prices.base import BTC, EUR, KAS, USD
from portfolio.providers.prices.coinbase import (
    COINBASE,
    SPOT_PAIRS,
    SPOT_PATH,
    CoinbasePriceSource,
    parse_spot,
)
from tests.providers.prices.harness import (
    COINBASE_HOST,
    PriceFake,
    Reply,
    ScriptedVendor,
    coinbase_body,
    price_client,
    price_settings,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.prices.base import PricePair, PriceQuote, PriceSource

BTC_USD: Final[PricePair] = (BTC, USD)
BTC_EUR: Final[PricePair] = (BTC, EUR)
KAS_USD: Final[PricePair] = (KAS, USD)

USD_AMOUNT: Final = "86000.10"
EUR_AMOUNT: Final = "79000.34"


def echoing() -> PriceFake:
    """A Coinbase answering about whatever pair the path named, with a per-pair amount.

    Echoing rather than fixed, so that a response about the *wrong* pair has to be scripted
    deliberately. With a fixed body, every request would be answered about one pair and the
    echoed-pair check would fire on the ordinary tests while the test written for it proved
    nothing.
    """

    def render(request: httpx.Request) -> str:
        pair = request.url.path.removeprefix("/v2/prices/").removesuffix("/spot")
        base, _, currency = pair.partition("-")
        amount = USD_AMOUNT if currency == USD else EUR_AMOUNT
        return coinbase_body(base=base, currency=currency, amount=amount)

    return PriceFake(coinbase=ScriptedVendor(Reply(renderer=render)))


def source_over(
    fake: PriceFake,
    *,
    max_attempts: int = 3,
) -> tuple[CoinbasePriceSource, httpx.AsyncClient]:
    client = price_client(fake, max_attempts=max_attempts)
    return CoinbasePriceSource(client, settings=price_settings()), client


async def fetch(
    fake: PriceFake,
    pairs: Sequence[PricePair],
    *,
    max_attempts: int = 3,
) -> Sequence[PriceQuote]:
    """One `fetch` against the scripted vendor, with the client closed afterwards.

    `max_attempts` exists for the rate-limit tests at the bottom of this file. Those assert
    on **which pairs were requested**, and the shared transport retries a 429 or a 5xx
    three times by default -- so the request log would carry three entries for the failing
    pair and the assertion would be about the retry policy rather than about this source's
    own loop. Retrying is `tests/providers/test_http.py`'s subject and is covered there;
    one attempt per pair is what makes the log here readable.
    """
    source, client = source_over(fake, max_attempts=max_attempts)
    async with client:
        return await source.fetch(pairs)


# --------------------------------------------------------------------------------------
# One request per pair, and both currencies
# --------------------------------------------------------------------------------------


async def test_each_pair_costs_one_request_to_its_own_path() -> None:
    """Two pairs, two requests, two documented paths -- and the singular endpoint label.

    The contrast with Kraken is the point of asserting it: this source cannot batch, so the
    budget argument for putting Kraken first is a fact about these two request counts and
    not a preference. `asset_price` rather than `asset_prices` is how a log says which of
    the two shapes made the call.
    """
    fake = echoing()

    quotes = await fetch(fake, (BTC_USD, BTC_EUR))

    assert fake.counts[COINBASE_HOST] == 2
    assert fake.paths_of(COINBASE_HOST) == [
        SPOT_PATH.format(pair="BTC-USD"),
        SPOT_PATH.format(pair="BTC-EUR"),
    ]
    assert fake.queries_of(COINBASE_HOST) == ["", ""]
    assert all(
        request.extensions[ENDPOINT_EXTENSION] == ASSET_PRICE for request in fake.coinbase.requests
    )
    assert {quote.pair: quote.amount for quote in quotes} == {
        BTC_USD: Decimal(USD_AMOUNT),
        BTC_EUR: Decimal(EUR_AMOUNT),
    }
    assert {quote.source for quote in quotes} == {COINBASE}


async def test_the_requests_are_made_one_after_another_and_never_gathered() -> None:
    """Sequential, because a `gather` hands the shared limiter every acquisition at once.

    That turns a per-host floor into a queue whose depth nobody bounded -- the reason
    `chains/kaspa.py` gives, and it applies identically here. Asserted through the recorded
    order, which is the only artefact that distinguishes the two.
    """
    fake = echoing()

    await fetch(fake, (BTC_EUR, BTC_USD))

    assert fake.paths_of(COINBASE_HOST) == [
        SPOT_PATH.format(pair="BTC-EUR"),
        SPOT_PATH.format(pair="BTC-USD"),
    ]


def test_it_declares_the_two_btc_pairs_and_no_kaspa_one() -> None:
    """The measured 404, recorded as a declaration rather than as a request that fails.

    Asking anyway would cost a round trip an hour, for as long as the product exists, to
    rediscover something measured on 2026-09-23.
    """
    fake = echoing()
    source, _client = source_over(fake)

    assert source.pairs == SPOT_PAIRS
    assert frozenset({BTC_USD, BTC_EUR}) == SPOT_PAIRS
    assert source.name == COINBASE


async def test_a_kaspa_pair_is_skipped_without_a_request() -> None:
    """A caller that did not filter costs nothing, and the BTC pair beside it still answers.

    `fetch_prices` filters on `pairs` already; this is the second guard, and what it buys
    is that an unfiltered caller gets a partial answer rather than a 404 that ends the call.
    """
    fake = echoing()

    quotes = await fetch(fake, (KAS_USD, BTC_USD))

    assert fake.counts[COINBASE_HOST] == 1
    assert [quote.pair for quote in quotes] == [BTC_USD]


async def test_asking_for_nothing_makes_no_request() -> None:
    """The empty page. A refresh with nothing outstanding must not call anybody."""
    fake = echoing()

    assert await fetch(fake, ()) == ()
    assert fake.requests == []


# --------------------------------------------------------------------------------------
# One pair failing does not take the others with it
# --------------------------------------------------------------------------------------


async def test_one_pair_failing_leaves_the_other_its_price() -> None:
    """The partial answer `fetch_prices` is built to accept, produced deliberately here.

    Letting the failure propagate would throw away a BTC/USD price that had **already
    arrived** because BTC/EUR failed -- and the caller would then move on to the next source
    for both, having already paid for one of them. This is the one place in the package
    where a refusal is swallowed, and it is swallowed because the loop above treats a
    missing pair correctly.
    """

    def render(request: httpx.Request) -> str:
        if request.url.path.endswith("BTC-EUR/spot"):
            return "{}"  # Not the documented envelope: a refusal for this pair only.
        return coinbase_body(base=BTC, currency=USD, amount=USD_AMOUNT)

    fake = PriceFake(coinbase=ScriptedVendor(Reply(renderer=render)))

    quotes = await fetch(fake, (BTC_USD, BTC_EUR))

    assert [quote.pair for quote in quotes] == [BTC_USD]
    assert fake.counts[COINBASE_HOST] == 2, "the failing pair was still attempted"


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(404, id="a pair it does not list"),
        pytest.param(429, id="throttled"),
        pytest.param(503, id="unavailable"),
    ],
)
async def test_every_status_a_pair_can_fail_with_leaves_the_other_pair_alone(status: int) -> None:
    """All three typed errors are swallowed per pair, not only the schema one.

    A `429` is the interesting row: the transport has already retried it, so by the time it
    reaches here the vendor has refused repeatedly -- and continuing to the *next pair* on
    the same host is a deliberate choice rather than an oversight. It is bounded by there
    being two pairs.
    """

    def render(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError  # pragma: no cover - replaced below

    del render

    class PerPair(ScriptedVendor):
        def answer(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith("BTC-EUR/spot"):
                return httpx.Response(status, content="{}")
            return httpx.Response(
                200, content=coinbase_body(base=BTC, currency=USD, amount=USD_AMOUNT)
            )

    fake = PriceFake(coinbase=PerPair())

    quotes = await fetch(fake, (BTC_USD, BTC_EUR))

    assert [quote.pair for quote in quotes] == [BTC_USD]


# --------------------------------------------------------------------------------------
# The echoed pair is checked, which is this parser's whole job
# --------------------------------------------------------------------------------------


def test_a_document_about_another_currency_is_refused() -> None:
    """The cache failure: a `BTC-EUR` request answered with the `BTC-USD` document.

    Nothing downstream can see this. The amount is a plausible number, the request was for
    EUR, and the total would be roughly fifteen percent wrong with a complete flag on it --
    which is exactly the kind of confident wrong number criterion 3 exists to refuse.
    """
    body = coinbase_body(base=BTC, currency=USD, amount=USD_AMOUNT)

    with pytest.raises(ProviderResponseError) as caught:
        parse_spot(body, BTC_EUR)

    message = str(caught.value)
    assert "BTC/EUR" in message, "the refusal must name the pair that was asked for"
    assert USD_AMOUNT not in message


def test_a_document_about_another_asset_is_refused() -> None:
    """The same check on the other field, because a cache can miss on either key."""
    body = coinbase_body(base="ETH", currency=USD, amount=USD_AMOUNT)

    with pytest.raises(ProviderResponseError):
        parse_spot(body, BTC_USD)


def test_the_comparison_is_case_sensitive_against_what_was_sent() -> None:
    """`btc` is not `BTC`, and the constants that built the path are what it is compared to.

    A case-insensitive comparison would be a small kindness that quietly accepted a
    different vendor's envelope, and the path was built from these exact constants.
    """
    body = coinbase_body(base="btc", currency="usd", amount=USD_AMOUNT)

    with pytest.raises(ProviderResponseError):
        parse_spot(body, BTC_USD)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="not JSON"),
        pytest.param("[]", id="a JSON array rather than an object"),
        pytest.param("{}", id="no data object"),
        pytest.param('{"data": "86000.10"}', id="a data field that is not an object"),
        pytest.param('{"data": {"base": "BTC", "currency": "USD"}}', id="no amount"),
        pytest.param(
            '{"data": {"amount": "0", "base": "BTC", "currency": "USD"}}', id="a zero price"
        ),
        pytest.param(
            '{"data": {"amount": "-1", "base": "BTC", "currency": "USD"}}', id="a negative price"
        ),
        pytest.param(
            '{"data": {"amount": "1,234.5", "base": "BTC", "currency": "USD"}}',
            id="a localised separator",
        ),
    ],
)
def test_a_body_that_cannot_be_trusted_is_a_typed_refusal(body: str) -> None:
    """`ProviderResponseError` in every arm, never a `KeyError` or a `TypeError`.

    The localised-separator row is the one a reader would not have written down:
    `Decimal("1,234.5")` raises `InvalidOperation`, which is not a `ValueError` subclass a
    naive `except ValueError` would catch, and an untyped escape here would end a refresh
    rather than moving to the next source.
    """
    with pytest.raises(ProviderResponseError):
        parse_spot(body, BTC_USD)


def test_a_string_price_keeps_every_digit_the_vendor_sent() -> None:
    """Coinbase sends a string, so nothing can happen to it -- asserted, not assumed.

    The control beside the Kaspa float test: this is the shape that is already safe, and
    if a change to the shared decoder ever broke it this is where it would show.
    """
    body = coinbase_body(base=BTC, currency=USD, amount="86000.10000000")

    quote = parse_spot(body, BTC_USD)

    assert quote.amount == Decimal("86000.10000000")
    assert str(quote.amount) == "86000.10000000"


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(Reply(status=429), id="throttled"),
        pytest.param(Reply(status=503), id="unavailable"),
        pytest.param(Reply(status=500), id="a server error"),
        pytest.param(Reply(status=404), id="a pair it does not list"),
        pytest.param(Reply(status=401), id="behind an auth proxy"),
        pytest.param(Reply(body="not json"), id="a 200 that is not JSON"),
        pytest.param(Reply(error=httpx.ConnectError("no route")), id="a transport failure"),
    ],
)
async def test_nothing_escapes_fetch_whatever_the_vendor_does(reply: Reply) -> None:
    """`fetch` raises for no failure at all, which is this source's whole contract.

    Every other source in the package raises and lets `fetch_prices` move on; this one
    swallows, because it is the only source that makes more than one request and a refusal
    on the second pair must not discard the first pair's answer.

    That makes "it never raises" a property worth asserting over the *whole* range of
    failures rather than over the one a test happened to script. A transport failure is in
    the list deliberately: it arrives as a different exception type inside `EndpointSet`
    and would escape a catch clause written around statuses alone.

    An empty answer with the pair left for the next source is the correct outcome in all
    seven cases, and `test_one_pair_failing_leaves_the_other_its_price` is what shows the
    swallow is per pair rather than per call.
    """
    fake = PriceFake(coinbase=ScriptedVendor(reply))

    quotes = await fetch(fake, (BTC_USD,))

    assert quotes == ()
    assert fake.counts[COINBASE_HOST] >= 1, "the vendor was actually asked"


# --------------------------------------------------------------------------------------
# Conformance, decided by mypy rather than by isinstance
# --------------------------------------------------------------------------------------

_CONFORMS: PriceSource = CoinbasePriceSource(
    httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
)


# --------------------------------------------------------------------------------------
# A rate limit is the one failure that stops the loop
# --------------------------------------------------------------------------------------
#
# Every other per-pair failure leaves the remaining pairs to be asked for. A 429 does not:
# re-asking a host that has just told us to stop is how a soft throttle becomes the ban one
# vendor warns about, and by the time a 429 reaches this source the shared transport has
# already retried it -- so the next request would be the third refusal in a row.
#
# `ProviderRateLimitedError` is a **subclass** of `ProviderUnavailableError`, so the order
# of the two `except` arms decides whether this behaviour exists at all. The tests below
# are written so that a reordering fails one of them rather than merely making a branch
# dead.


class PerPairVendor(ScriptedVendor):
    """A Coinbase that fails one named pair and answers every other one correctly.

    Keyed on the pair rather than on a call count, because what these tests turn on is
    *which* pair failed and whether the next one was asked at all -- and a queue of replies
    would make the answer depend on the count instead, which is the thing under test.

    The successful answers echo the pair out of the path, so they pass `parse_spot`'s
    echoed-pair check; a fixed body would fail it for one of the two pairs and the test
    would be measuring the wrong refusal.
    """

    def __init__(self, status: int, *, failing: str) -> None:
        super().__init__()
        self._status = status
        self._failing = failing

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        pair = request.url.path.removeprefix("/v2/prices/").removesuffix("/spot")
        if pair == self._failing:
            return httpx.Response(self._status, content="{}")
        base, _, currency = pair.partition("-")
        amount = USD_AMOUNT if currency == USD else EUR_AMOUNT
        return httpx.Response(
            200, content=coinbase_body(base=base, currency=currency, amount=amount)
        )


async def test_a_rate_limited_pair_stops_the_source_asking_about_the_next_one() -> None:
    """429 on the first pair, and the second pair is never requested.

    The assertion is the request log. A source that carried on would return one quote for
    the second pair and nothing about that *answer* would distinguish the two behaviours --
    only the fact that Coinbase was asked once rather than twice does, and the difference
    between them is an application that is throttled and one that is banned.
    """
    fake = PriceFake(coinbase=PerPairVendor(429, failing="BTC-USD"))

    quotes = await fetch(fake, (BTC_USD, BTC_EUR), max_attempts=1)

    assert quotes == ()
    assert fake.paths_of(COINBASE_HOST) == [SPOT_PATH.format(pair="BTC-USD")]


async def test_a_server_error_on_one_pair_does_not_stop_the_next_one() -> None:
    """The discriminator, and without it the test above is satisfied by "stop on anything".

    A 503 is the same *class* of failure -- `ProviderUnavailableError` -- and it must not
    stop the loop: one pair's upstream hiccup is no reason to abandon a price that is one
    request away. These two tests differ in exactly one input, the status, and in exactly
    one outcome, whether the second pair was asked for.

    This is also what makes the `except` order load-bearing rather than incidental.
    Catching `ProviderUnavailableError` first would make both statuses behave like this
    one, and the test above would go red -- which is the point of writing the pair.
    """
    fake = PriceFake(coinbase=PerPairVendor(503, failing="BTC-USD"))

    quotes = await fetch(fake, (BTC_USD, BTC_EUR), max_attempts=1)

    assert [quote.pair for quote in quotes] == [BTC_EUR]
    assert fake.paths_of(COINBASE_HOST) == [
        SPOT_PATH.format(pair="BTC-USD"),
        SPOT_PATH.format(pair="BTC-EUR"),
    ]


def test_the_rate_limit_error_is_a_subclass_of_the_unavailable_one() -> None:
    """The fact that makes the `except` order decide the behaviour, stated once.

    A reader meeting the two arms has to know this to see why they are in that order; a
    reader who does not will reorder them one day and delete a branch without touching a
    line of logic. #7's second closing lesson is that an exception hierarchy nobody has
    read is a guess that reads like a fact, and `issubclass` takes one line.
    """
    assert issubclass(ProviderRateLimitedError, ProviderUnavailableError)
    assert issubclass(ProviderUnavailableError, ProviderError)
    assert not issubclass(ProviderResponseError, ProviderUnavailableError)


async def test_a_rate_limit_on_a_later_pair_keeps_the_earlier_pairs_price() -> None:
    """Stopping is not discarding: whatever arrived before the 429 is still returned.

    `break` rather than `return ()`. The distinction matters because the first pair's price
    has already been paid for -- the request was made, the answer came back -- and throwing
    it away would mean the next source is asked for both, spending a second request on a
    pair that is already answered.
    """
    fake = PriceFake(coinbase=PerPairVendor(429, failing="BTC-USD"))

    quotes = await fetch(fake, (BTC_EUR, BTC_USD), max_attempts=1)

    assert [quote.pair for quote in quotes] == [BTC_EUR]
    assert fake.paths_of(COINBASE_HOST) == [
        SPOT_PATH.format(pair="BTC-EUR"),
        SPOT_PATH.format(pair="BTC-USD"),
    ]
