"""Criterion 8 of #9, and the measurement the whole call budget rests on.

The spec's decisive finding is one sentence: **Kraken returns all four pairs in a single
call.** Everything else follows from it -- the budget of 720 requests a month, the choice
of a key-free primary, and the conclusion that CoinGecko's Demo quota was never the binding
constraint. `test_one_call_returns_every_configured_pair` is what turns that sentence into
something the build checks.

It asserts the **request**, not only the answer. Four quotes could come back from four
requests, and every assertion about the result would be identical; only the request count
and the query string say which happened, and the difference between them is the difference
between 720 requests a month and 2,880.

## Two shapes this file is deliberately careful about

**`c[0]`, not `a`, `b` or `o`.** The ticker entry carries the ask, the bid, the last trade,
the volume, the open and more, all as strings and all plausible. A parser reaching for the
wrong one returns a number nobody would question. `harness.kraken_body` therefore gives
every other field a *different* value derived from the price, so a wrong field is a wrong
assertion rather than a coincidence.

**`error: []` is checked.** Kraken answers `200` and reports its own failures in an `error`
list, so a status check alone reads an error document as an empty result and silently
reports no prices at all -- which, hourly, is a portfolio that stops updating and says
nothing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.providers.errors import (
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import ASSET_PRICES, ENDPOINT_EXTENSION
from portfolio.providers.prices.base import BTC, EUR, KAS, SUPPORTED_PAIRS, USD
from portfolio.providers.prices.kraken import (
    KRAKEN,
    LAST_TRADE_FIELD,
    PAIR_CODES,
    TICKER_PATH,
    KrakenPriceSource,
    parse_ticker,
)
from tests.providers.prices.harness import (
    KRAKEN_HOST,
    KRAKEN_PRICES,
    PriceFake,
    Reply,
    ScriptedVendor,
    kraken_body,
    kraken_echo,
    price_client,
    price_settings,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from portfolio.providers.prices.base import PricePair, PriceQuote, PriceSource

#: All four pairs, in a fixed order, so "one call carried every one of them" means
#: something. Sorted, because `SUPPORTED_PAIRS` is a frozenset and an unordered fixture
#: would make the query string's contents unassertable.
ALL_FOUR: Final[tuple[PricePair, ...]] = tuple(sorted(SUPPORTED_PAIRS))

#: What each pair's price must come back as, read off the harness's table through the
#: shipped pair codes. Derived here rather than written out again, because the pair codes
#: are the thing under test in `test_the_pair_codes_are_the_measured_ones` and repeating
#: them would mean a typo agreed with itself.
EXPECTED: Final[dict[PricePair, Decimal]] = {
    pair: Decimal(KRAKEN_PRICES[code]) for pair, code in PAIR_CODES.items()
}


def source_over(fake: PriceFake) -> tuple[KrakenPriceSource, httpx.AsyncClient]:
    """The source and the client it holds, so a test can close the client afterwards."""
    client = price_client(fake)
    return KrakenPriceSource(client, settings=price_settings()), client


async def fetch(fake: PriceFake, pairs: Sequence[PricePair] = ALL_FOUR) -> Sequence[PriceQuote]:
    """One `fetch` against the scripted vendor, with the client closed afterwards."""
    source, client = source_over(fake)
    async with client:
        return await source.fetch(pairs)


def answering(prices: Mapping[str, str] = KRAKEN_PRICES) -> PriceFake:
    """A Kraken that answers about exactly the codes it was asked for, from `prices`.

    Echoing rather than fixed, for the reason `kraken_echo` gives: a fixed body would
    answer about all four pairs however few were requested, so the guard that refuses an
    unrequested entry would fire on every ordinary test and the test written *for* that
    guard would prove nothing.
    """
    return PriceFake(kraken=ScriptedVendor(Reply(renderer=kraken_echo(prices))))


# --------------------------------------------------------------------------------------
# Criterion 8: one call, every configured pair, both currencies
# --------------------------------------------------------------------------------------


async def test_one_call_returns_every_configured_pair() -> None:
    """The measurement the budget rests on, asserted on the request and on the answer.

    **One** request, carrying all four pair codes in a single `pair=` parameter, answered
    with all four prices. The request assertions come first because they are the ones the
    budget depends on: four quotes obtained through four requests would satisfy every
    assertion about the result and quadruple the traffic.

    Both currencies appear, which is the other half of criterion 8 -- USD and EUR are
    fetched, each on its own, and neither is derived from the other.
    """
    fake = answering()

    quotes = await fetch(fake)

    assert fake.counts[KRAKEN_HOST] == 1, "the budget rests on this being one request"
    assert fake.paths_of(KRAKEN_HOST) == [TICKER_PATH]
    query = fake.queries_of(KRAKEN_HOST)[0]
    assert query.startswith("pair=")
    assert set(query.removeprefix("pair=").split(",")) == set(PAIR_CODES.values())

    assert {quote.pair: quote.amount for quote in quotes} == EXPECTED
    assert {quote.quote_currency for quote in quotes} == {USD, EUR}
    assert {quote.source for quote in quotes} == {KRAKEN}


async def test_the_four_codes_travel_in_one_parameter_and_not_in_four() -> None:
    """`pair=A,B,C,D` and not `pair=A&pair=B&...`, which is a different request entirely.

    The measurement on 2026-09-23 exercised the comma-separated form. Four repeated
    parameters is what a naive `params=` would build out of a list, and whether the vendor
    answers it at all is unknown -- so the shape is pinned rather than left to a library's
    convention.
    """
    fake = answering()

    await fetch(fake)

    query = fake.queries_of(KRAKEN_HOST)[0]

    assert query.count("pair=") == 1
    assert query.count(",") == len(PAIR_CODES) - 1


async def test_a_smaller_request_asks_only_for_what_it_wants() -> None:
    """Two pairs asked, two codes sent, two quotes back -- and still one request.

    `refresh_prices` may be asked to refresh one pair, and a source that always requested
    all four would make a targeted refresh cost the same as a full one. Asserted alongside
    the full case, because the full case alone cannot tell "sends what it was asked for"
    from "always sends everything".
    """
    fake = answering()
    wanted = ((BTC, USD), (KAS, EUR))

    quotes = await fetch(fake, wanted)

    assert fake.counts[KRAKEN_HOST] == 1
    query = fake.queries_of(KRAKEN_HOST)[0]
    assert set(query.removeprefix("pair=").split(",")) == {
        PAIR_CODES[(BTC, USD)],
        PAIR_CODES[(KAS, EUR)],
    }
    assert {quote.pair for quote in quotes} == set(wanted)


async def test_asking_for_nothing_makes_no_request() -> None:
    """An empty request is a round trip spent on a question with no content.

    It also counts against a rate limit, which for the primary vendor is the one resource
    this design is careful with. `fetch_prices` skips a source with nothing outstanding, so
    this is the second guard -- for a caller that does not.
    """
    fake = answering()

    quotes = await fetch(fake, ())

    assert quotes == ()
    assert fake.requests == []


async def test_a_pair_kraken_does_not_list_is_not_asked_for() -> None:
    """A pair outside `PAIR_CODES` is dropped before a URL is built out of it.

    Without this the code would be `None` in the query string, and the vendor would answer
    an error for the whole call -- taking the three answerable pairs down with the one that
    was never listed.
    """
    fake = answering()

    quotes = await fetch(fake, ((BTC, USD), ("XRP", USD)))

    assert fake.counts[KRAKEN_HOST] == 1
    assert fake.queries_of(KRAKEN_HOST)[0] == f"pair={PAIR_CODES[(BTC, USD)]}"
    assert [quote.pair for quote in quotes] == [(BTC, USD)]


def test_the_pair_codes_are_the_measured_ones() -> None:
    """All four, as literals, because they are not derivable from the symbols.

    Kraken's older assets carry the `X`/`Z` class prefixes and newer listings do not:
    `XXBTZUSD` and `KASUSD` follow different rules. A format string inferred from either
    one is wrong for the other, so the table is the implementation and this is the check on
    it.
    """
    assert PAIR_CODES == {
        (BTC, USD): "XXBTZUSD",
        (BTC, EUR): "XXBTZEUR",
        (KAS, USD): "KASUSD",
        (KAS, EUR): "KASEUR",
    }
    assert set(PAIR_CODES) == SUPPORTED_PAIRS


async def test_the_declared_pairs_are_the_table_and_the_request_label_is_the_plural_one() -> None:
    """One source that lists everything, and the label that says "several in one call".

    `asset_prices` rather than `asset_price` is what makes the budget visible in a log: one
    plural line per refresh is the whole of an hourly refresh's traffic against the primary,
    so a log that starts showing more of them is evidence that something began fetching
    prices somewhere it should not.
    """
    fake = answering()
    source, client = source_over(fake)

    assert source.pairs == SUPPORTED_PAIRS
    assert source.name == KRAKEN

    async with client:
        await source.fetch(ALL_FOUR)

    assert fake.kraken.requests[0].extensions[ENDPOINT_EXTENSION] == ASSET_PRICES
    assert fake.kraken.requests[0].method == "GET"


# --------------------------------------------------------------------------------------
# The price is the last trade, and the envelope is checked
# --------------------------------------------------------------------------------------


async def test_the_price_is_the_last_trade_and_not_the_ask_the_bid_or_the_open() -> None:
    """Every other field in the entry is a different number, so a wrong field is visible.

    A mid-point built from the bid and the ask is a figure no trade ever happened at, and
    the open is yesterday's. All three are strings of the same shape in the same object, so
    nothing about a wrong choice would look wrong -- which is why the fixture makes them
    differ and why this assertion exists at all.
    """
    fake = answering({"XXBTZUSD": "86000.10000"})

    quotes = await fetch(fake, ((BTC, USD),))

    assert quotes[0].amount == Decimal("86000.10000")
    # The neighbouring fields in the fixture, which a wrong reach would have returned.
    for wrong in ("86000.100001", "86000.100002", "86000.100009"):
        assert quotes[0].amount != Decimal(wrong)


async def test_a_string_price_keeps_its_trailing_zeros() -> None:
    """Kraken sends a string, so nothing can happen to it -- and this says so.

    `86000.10000` through a `double` comes back `86000.1`: the same number, a different
    string, and the string is what `NumericText` stores and what a diff of the database
    shows. This is the control for the Kaspa float test -- the vendor whose shape is
    already safe, asserted so that a change to the shared decoder that broke strings would
    be caught here rather than in production.
    """
    fake = answering({"XXBTZUSD": "86000.10000"})

    quotes = await fetch(fake, ((BTC, USD),))

    assert str(quotes[0].amount) == "86000.10000"


async def test_an_error_in_the_envelope_is_a_refusal_even_with_a_200() -> None:
    """Kraken reports its own failures in a list and still answers 200.

    A status check alone would read an error document as an empty result and report no
    prices -- quietly, hourly, forever. The message carries the *count* and none of the
    vendor's prose, which is the habit that keeps a response body out of a log.
    """
    body = kraken_body({}, errors=["EQuery:Unknown asset pair"])
    fake = PriceFake(kraken=ScriptedVendor(Reply(body=body)))

    with pytest.raises(ProviderResponseError) as caught:
        await fetch(fake)

    rendered = str(caught.value)
    assert "1 error" in rendered
    assert "Unknown asset pair" not in rendered


async def test_a_pair_simply_missing_from_the_answer_is_left_for_the_next_source() -> None:
    """Three of four is a partial answer, which is what failover is for -- not an error.

    The fourth pair travels on to Coinbase, or to the Kaspa endpoint, or becomes a reason.
    Raising here would throw away three prices that had already arrived because one had not.
    """
    fake = answering({"XXBTZUSD": "86000.10000", "KASUSD": "0.04228645"})

    quotes = await fetch(fake)

    assert {quote.pair for quote in quotes} == {(BTC, USD), (KAS, USD)}


def test_an_entry_for_a_pair_nobody_asked_about_is_refused() -> None:
    """A correlation bug, refused rather than dropped, the way `align_balances` refuses one.

    A vendor answering a question we did not put is a cached response for another caller or
    a mis-parsed query, and either way nothing else in the document can be trusted. Dropping
    the extra entry silently would hide that behind prices that still look plausible.

    Asserted on `parse_ticker` directly: the source filters what it *sends*, so a response
    carrying an unrequested code can only be produced by a fake -- which is the point.
    """
    body = kraken_body({"XXBTZUSD": "86000.10000", "XETHZUSD": "3000.00000"})

    with pytest.raises(ProviderResponseError) as caught:
        parse_ticker(body, {"XXBTZUSD": (BTC, USD)})

    assert "1 pair" in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="not JSON"),
        pytest.param("[]", id="a JSON array rather than an object"),
        pytest.param('{"result": {}}', id="no error list"),
        pytest.param('{"error": "none", "result": {}}', id="an error field that is not a list"),
        pytest.param('{"error": []}', id="no result object"),
        pytest.param('{"error": [], "result": []}', id="a result that is not an object"),
        pytest.param(
            '{"error": [], "result": {"XXBTZUSD": "86000"}}',
            id="an entry that is a string",
        ),
        pytest.param('{"error": [], "result": {"XXBTZUSD": {}}}', id="an entry with no c"),
        pytest.param('{"error": [], "result": {"XXBTZUSD": {"c": []}}}', id="an empty c"),
        pytest.param('{"error": [], "result": {"XXBTZUSD": {"c": "86000"}}}', id="c is not a list"),
        pytest.param('{"error": [], "result": {"XXBTZUSD": {"c": ["0", "1"]}}}', id="a zero price"),
        pytest.param(
            '{"error": [], "result": {"XXBTZUSD": {"c": ["-1", "1"]}}}', id="a negative price"
        ),
        pytest.param(
            '{"error": [], "result": {"XXBTZUSD": {"c": ["not a number", "1"]}}}',
            id="a string that is not a number",
        ),
    ],
)
def test_a_body_that_cannot_be_trusted_is_a_typed_refusal(body: str) -> None:
    """Every refusal in the parser's own table, driven from the shape that causes it.

    `ProviderResponseError` in every case, never a `KeyError`, a `TypeError` or an
    `IndexError` -- those would reach a service as exceptions the layering contract forbids
    it from naming, and `fetch_prices` catches `ProviderError` and nothing else, so an
    untyped escape would end a whole refresh rather than moving to the next source.
    """
    with pytest.raises(ProviderResponseError):
        parse_ticker(body, {"XXBTZUSD": (BTC, USD)})


def test_the_refusals_name_the_pair_code_and_never_the_value() -> None:
    """A pair code is public vendor vocabulary; a response body is not, anywhere.

    The value is kept out here although a price is harmless, because the same habit at the
    next boundary is what keeps an address out of a log. `LAST_TRADE_FIELD` is named so an
    operator knows which field was wrong.
    """
    body = '{"error": [], "result": {"XXBTZUSD": {"c": ["a-suspicious-value", "1"]}}}'

    with pytest.raises(ProviderResponseError) as caught:
        parse_ticker(body, {"XXBTZUSD": (BTC, USD)})

    assert "a-suspicious-value" not in str(caught.value)

    with pytest.raises(ProviderResponseError) as missing:
        parse_ticker('{"error": [], "result": {"XXBTZUSD": {}}}', {"XXBTZUSD": (BTC, USD)})

    assert "XXBTZUSD" in str(missing.value)
    assert LAST_TRADE_FIELD in str(missing.value)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(429, ProviderRateLimitedError, id="throttled"),
        pytest.param(503, ProviderUnavailableError, id="unavailable"),
        pytest.param(400, ProviderResponseError, id="refused"),
    ],
)
async def test_a_failing_vendor_produces_the_typed_error_its_status_means(
    status: int,
    expected: type[Exception],
) -> None:
    """The three-way split, so `fetch_prices` sees a `ProviderError` and moves on.

    This source has one endpoint and no fallback instance -- there is one Kraken -- so
    every one of these ends the call, and what happens next is another *source*, which is
    `test_failover.py`'s subject.
    """
    fake = PriceFake(kraken=ScriptedVendor(Reply(status=status)))

    with pytest.raises(expected):
        await fetch(fake)


async def test_a_refusal_never_names_the_host() -> None:
    """The vendor's brand, never its hostname: a message is a string that reaches a log.

    `providers/http.py` goes to some trouble to keep a URL out of the logs, and an
    exception message naming one walks around all of it.
    """
    fake = PriceFake(kraken=ScriptedVendor(Reply(status=500)))

    with pytest.raises(ProviderUnavailableError) as caught:
        await fetch(fake)

    rendered = f"{caught.value}{caught.value!r}"
    assert KRAKEN_HOST not in rendered
    assert "Kraken" in rendered


# --------------------------------------------------------------------------------------
# Conformance, decided by mypy rather than by isinstance
# --------------------------------------------------------------------------------------

_CONFORMS: PriceSource = KrakenPriceSource(
    httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
)
