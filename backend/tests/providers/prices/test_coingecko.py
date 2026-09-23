"""The keyed source: one request, a header credential, and the second JSON-number boundary.

Three things make this source different from the other three.

**It is the only one that needs a credential**, which makes it criterion 5's whole subject
and the only place in this change where rule 3's "never logged, never returned, never
persisted" has anything to guard. The key travels as a **header** and never in the query
string -- `providers/http.py` warns that `strip_query` meets the letter of the logging rule
and leaks anyway, and a credential in a URL is the exact case that warning is about. What is
asserted here is that the query carries no credential and the header carries exactly one;
`tests/security/test_price_key_logging.py` drives the whole production pipeline and asserts
the sentinel reaches no byte of stdout.

**It sends JSON numbers, like the Kaspa endpoint.** That is a second float boundary and it
gets its own assertions rather than sharing Kaspa's, because the two documents are different
shapes and "the digits survive" has to be shown for the one this parser reads.

**It is the one source whose response shape was never measured.** The spec records this as a
risk: verifying it needs a key this repository must not contain, so the fixtures here are
built from the vendor's documentation. That makes this the source most likely to meet
production with a wrong assumption, and the reason the parser's refusals are exercised as
thoroughly as the happy path -- a wrong shape has to become a typed refusal that failover
can move past, not an exception that ends a refresh.

No API key appears anywhere in this file, including a fake one shaped like a real one.
`SYNTHETIC_COINGECKO_KEY` is a sentence.
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
from portfolio.providers.prices.coingecko import (
    API_KEY_HEADER,
    COIN_IDS,
    COINGECKO,
    CURRENCY_CODES,
    FULL_PRECISION,
    SIMPLE_PRICE_PATH,
    CoinGeckoPriceSource,
    parse_simple_price,
)
from tests.providers.prices.harness import (
    COINGECKO_HOST,
    SYNTHETIC_COINGECKO_KEY,
    PriceFake,
    Reply,
    ScriptedVendor,
    coingecko_body,
    coingecko_echo,
    price_client,
    price_settings,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.prices.base import PricePair, PriceQuote, PriceSource

BTC_USD: Final[PricePair] = (BTC, USD)
BTC_EUR: Final[PricePair] = (BTC, EUR)
KAS_USD: Final[PricePair] = (KAS, USD)
KAS_EUR: Final[PricePair] = (KAS, EUR)

ALL_FOUR: Final[tuple[PricePair, ...]] = tuple(sorted(SUPPORTED_PAIRS))

#: The documented document, with every price a JSON **number** and its digits written out.
#: The BTC figures carry trailing zeros and the KAS ones eight decimal places, which is
#: what `precision=full` is asked for.
DOCUMENTED: Final[dict[str, dict[str, str]]] = {
    "bitcoin": {"usd": "86000.10000", "eur": "79000.34000"},
    "kaspa": {"usd": "0.04228645", "eur": "0.03885120"},
}

EXPECTED: Final[dict[PricePair, Decimal]] = {
    (BTC, USD): Decimal("86000.10000"),
    (BTC, EUR): Decimal("79000.34000"),
    (KAS, USD): Decimal("0.04228645"),
    (KAS, EUR): Decimal("0.03885120"),
}


def keyed() -> PriceFake:
    """A CoinGecko answering about exactly the coin ids the query asked for.

    Echoing rather than fixed, for the reason `coingecko_echo` gives: a fixed document
    would carry both coins however few were asked for, so the guard that refuses an
    unrequested coin would fire on every ordinary test while the test written for it
    proved nothing.
    """
    return PriceFake(coingecko=ScriptedVendor(Reply(renderer=coingecko_echo(DOCUMENTED))))


def source_over(fake: PriceFake) -> tuple[CoinGeckoPriceSource, httpx.AsyncClient]:
    client = price_client(fake)
    settings = price_settings(coingecko_api_key=SYNTHETIC_COINGECKO_KEY)
    return CoinGeckoPriceSource(client, settings=settings), client


async def fetch(fake: PriceFake, pairs: Sequence[PricePair] = ALL_FOUR) -> Sequence[PriceQuote]:
    source, client = source_over(fake)
    async with client:
        return await source.fetch(pairs)


# --------------------------------------------------------------------------------------
# One keyed request for every pair
# --------------------------------------------------------------------------------------


async def test_every_requested_pair_arrives_in_one_request() -> None:
    """All four pairs, one call, and the prices keyed back into our own vocabulary.

    This is the only source besides Kraken that lists all four, which is why it is the
    fallback for the pair Kraken is otherwise alone on -- KAS/EUR. Asserted on the request
    count as well as on the answer, because a source that made four calls would spend four
    of the ten thousand a Demo key has each month rather than one.
    """
    fake = keyed()

    quotes = await fetch(fake)

    assert fake.counts[COINGECKO_HOST] == 1
    assert fake.paths_of(COINGECKO_HOST) == [SIMPLE_PRICE_PATH]
    assert {quote.pair: quote.amount for quote in quotes} == EXPECTED
    assert {quote.source for quote in quotes} == {COINGECKO}


async def test_the_query_is_sorted_deduplicated_and_asks_for_full_precision() -> None:
    """Two pairs of one coin cost one id, and the query is byte-stable between runs.

    Stability matters twice over: the vendor sits behind a cache, so a query whose
    parameters shuffle would miss it every time, and a human comparing two logs side by
    side should be reading a difference rather than a reordering.

    `precision=full` is the row that carries a rule-2 consequence. The documented default is
    a *rounded* value, and a price that has been rounded before it reaches us is a price we
    cannot un-round.
    """
    fake = keyed()

    await fetch(fake)

    assert fake.queries_of(COINGECKO_HOST) == [
        f"ids=bitcoin,kaspa&vs_currencies=eur,usd&precision={FULL_PRECISION}"
    ]
    assert FULL_PRECISION == "full"


async def test_two_pairs_of_one_coin_send_that_coin_once() -> None:
    """The de-duplication, shown rather than implied by the sorted query above."""
    fake = keyed()

    await fetch(fake, (BTC_USD, BTC_EUR))

    assert fake.queries_of(COINGECKO_HOST) == [
        f"ids=bitcoin&vs_currencies=eur,usd&precision={FULL_PRECISION}"
    ]


async def test_asking_for_nothing_makes_no_request() -> None:
    """Every request against a keyed vendor is one of ten thousand a month."""
    fake = keyed()

    assert await fetch(fake, ()) == ()
    assert fake.requests == []


async def test_an_unsupported_pair_is_dropped_before_the_query_is_built() -> None:
    """`COIN_IDS[symbol]` on an unknown symbol would be a `KeyError` in the source itself.

    Which is not a `ProviderError`, so `fetch_prices` would not catch it and the whole
    refresh would end -- losing three pairs that had already been answered by earlier
    sources. The filter is what keeps an unfiltered caller from doing that.
    """
    fake = keyed()

    quotes = await fetch(fake, (("XRP", USD), BTC_USD))

    assert fake.queries_of(COINGECKO_HOST) == [
        f"ids=bitcoin&vs_currencies=usd&precision={FULL_PRECISION}"
    ]
    assert [quote.pair for quote in quotes] == [BTC_USD]


def test_it_declares_every_pair_the_product_prices() -> None:
    """The only source that lists all four, which is what makes it a usable last resort."""
    fake = keyed()
    source, _client = source_over(fake)

    assert source.pairs == SUPPORTED_PAIRS
    assert source.name == COINGECKO


def test_the_two_vocabularies_are_tables_rather_than_transformations() -> None:
    """A coin id is not a lower-cased symbol, and pinning that is what keeps it a table.

    Several unrelated assets share a ticker symbol on this vendor -- which is the reason the
    id exists at all -- so `symbol.lower()` is a transformation that happens to work today
    and is not a statement that the two vocabularies are the same. The day an id is not the
    lower-cased name, a table is a line to edit and a rule is a bug to discover.
    """
    assert COIN_IDS == {BTC: "bitcoin", KAS: "kaspa"}
    assert CURRENCY_CODES == {USD: "usd", EUR: "eur"}
    assert set(COIN_IDS) == {symbol for symbol, _currency in SUPPORTED_PAIRS}
    assert set(CURRENCY_CODES) == {currency for _symbol, currency in SUPPORTED_PAIRS}


# --------------------------------------------------------------------------------------
# The credential is a header, and it is in nothing else
# --------------------------------------------------------------------------------------


async def test_the_key_travels_as_a_header_and_never_in_the_query() -> None:
    """The documented query-parameter alternative is deliberately unused, and this says so.

    A credential in a URL reaches an access log, a proxy log, a browser history and a
    `Referer`, and `strip_query` in this application's own transport is described in its own
    docstring as meeting the letter of the rule while leaking anyway. A header is the one
    place the value does not become part of a request's identity.

    The absence is asserted against the whole URL rather than against the query alone,
    because a key could reach a path as easily as a parameter.
    """
    fake = keyed()

    await fetch(fake)

    request = fake.coingecko.requests[0]

    assert request.headers[API_KEY_HEADER] == SYNTHETIC_COINGECKO_KEY
    assert API_KEY_HEADER == "x-cg-demo-api-key"
    assert SYNTHETIC_COINGECKO_KEY not in str(request.url)
    assert SYNTHETIC_COINGECKO_KEY not in request.url.query.decode()
    assert request.extensions[ENDPOINT_EXTENSION] == ASSET_PRICES


async def test_the_source_holds_the_secret_wrapped_and_not_as_a_string() -> None:
    """An instance reaching a repr or a traceback must carry a mask, not the key.

    `SecretStr` is what does that, and holding the unwrapped string "because it is needed at
    request time anyway" is the edit that would undo it -- silently, with every test in this
    file still green except this one.
    """
    fake = keyed()
    source, client = source_over(fake)

    async with client:
        rendered = f"{source!r}{vars(source) if hasattr(source, '__dict__') else ''}"

    assert SYNTHETIC_COINGECKO_KEY not in rendered


# --------------------------------------------------------------------------------------
# The second JSON-number boundary
# --------------------------------------------------------------------------------------


def test_a_json_number_from_this_vendor_keeps_its_digits_too() -> None:
    """The same hook, the other document. Asserted against the literal in the fixture.

    `0.04228645` is typed out in `DOCUMENTED` and compared against here as a string; nothing
    in the expectation has been through `decode_json` or any other step the parser takes.
    """
    body = coingecko_body({"kaspa": {"usd": "0.04228645"}})

    quotes = parse_simple_price(body, [KAS_USD])

    assert isinstance(quotes[0].amount, Decimal)
    assert str(quotes[0].amount) == "0.04228645"
    assert quotes[0].amount == Decimal("0.04228645")


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("86000.10000", id="trailing zeros full precision is asked for"),
        pytest.param("0.1", id="the value IEEE-754 cannot represent at all"),
        pytest.param("1e-8", id="exponent form"),
        pytest.param("0.000000000000000000001", id="finer than the column's own scale"),
    ],
)
def test_every_shape_of_number_survives_this_parser(digits: str) -> None:
    """The property over the shapes `precision=full` can produce, not only the measured ones.

    The last row is finer than `PRICE_SCALE`, which is deliberate: the parser's job is to
    carry what the vendor sent, and deciding what the column can hold is `NumericText`'s.
    A parser that rounded early would make the column's declared scale a fiction.
    """
    body = coingecko_body({"bitcoin": {"usd": digits}})

    quotes = parse_simple_price(body, [BTC_USD])

    assert quotes[0].amount == Decimal(digits)
    assert str(quotes[0].amount) == str(Decimal(digits))


# --------------------------------------------------------------------------------------
# What it refuses, and what it merely leaves unanswered
# --------------------------------------------------------------------------------------


def test_a_coin_nobody_asked_about_is_refused() -> None:
    """The correlation rule, the same one `align_balances` applies to an address.

    A vendor answering a question we did not put is a cached response for another caller or
    a mis-parsed query, and nothing else in that document can be trusted either. The count
    is named and the coin id is not, which keeps a response body out of the message.
    """
    body = coingecko_body({"bitcoin": {"usd": "86000.10000"}, "dogecoin": {"usd": "0.4"}})

    with pytest.raises(ProviderResponseError) as caught:
        parse_simple_price(body, [BTC_USD])

    assert "1 coin" in str(caught.value)
    assert "dogecoin" not in str(caught.value)


def test_a_currency_nobody_asked_about_inside_a_requested_coin_is_not_refused() -> None:
    """The asymmetry, and it follows from the request's own shape rather than from taste.

    `vs_currencies` applies to the whole call, so asking for BTC/USD and KAS/EUR necessarily
    asks both currencies of both coins. The extra entries are therefore **ours** and not the
    vendor's, and refusing them would refuse every mixed-currency request this source ever
    makes.
    """
    body = coingecko_body({"bitcoin": {"usd": "86000.10000", "eur": "79000.34000"}})

    quotes = parse_simple_price(body, [BTC_USD])

    assert [quote.pair for quote in quotes] == [BTC_USD]
    assert quotes[0].amount == Decimal("86000.10000")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param("{}", "an empty document", id="nothing at all"),
        pytest.param('{"bitcoin": {}}', "the coin with no currencies", id="an empty coin"),
        pytest.param('{"bitcoin": {"gbp": 1}}', "another currency only", id="a currency we want"),
        pytest.param('{"bitcoin": 86000}', "a coin that is not an object", id="not an object"),
    ],
)
def test_a_pair_that_is_absent_is_unanswered_rather_than_an_error(body: str, reason: str) -> None:
    """This is the last source, so an absent pair becomes a reason rather than a refusal.

    There is nobody behind it to try. Raising would throw away whatever pairs *were* in the
    same document, which for a mixed request is the common case -- and would turn "CoinGecko
    does not list this coin today" into a failed refresh for everything.
    """
    assert parse_simple_price(body, [BTC_USD]) == (), reason


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="not JSON"),
        pytest.param("[]", id="a JSON array rather than an object"),
        pytest.param('{"bitcoin": {"usd": 0}}', id="a zero price"),
        pytest.param('{"bitcoin": {"usd": -1}}', id="a negative price"),
        pytest.param('{"bitcoin": {"usd": NaN}}', id="NaN, which json.loads accepts"),
        pytest.param('{"bitcoin": {"usd": true}}', id="a bool, which is an int subclass"),
        pytest.param('{"bitcoin": {"usd": "not a number"}}', id="a string that is not a number"),
    ],
)
def test_a_value_that_is_not_a_price_is_a_typed_refusal(body: str) -> None:
    """`ProviderResponseError`, never a `KeyError`, a `TypeError` or an `InvalidOperation`.

    Zero is the row criterion 3 cares about: a stored zero values every holding of that
    asset at nothing and produces a confident, believable, wrong total.
    """
    with pytest.raises(ProviderResponseError):
        parse_simple_price(body, [BTC_USD])


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(429, ProviderRateLimitedError, id="the monthly quota, or the per-minute one"),
        pytest.param(503, ProviderUnavailableError, id="unavailable"),
        pytest.param(401, ProviderResponseError, id="a wrong or expired key"),
        pytest.param(403, ProviderResponseError, id="a key without the plan"),
    ],
)
async def test_a_failing_vendor_produces_the_typed_error_its_status_means(
    status: int,
    expected: type[Exception],
) -> None:
    """A wrong key is a refusal, not unavailability -- and failover moves past both.

    The distinction matters to whoever reads it. "The chain is unavailable" sends an
    operator to look at a vendor's status page; a refusal on a keyed source sends them to
    look at their own variable, which is where the problem is. `401` and `403` are the two
    statuses a bad key actually produces.
    """
    fake = PriceFake(coingecko=ScriptedVendor(Reply(status=status)))

    with pytest.raises(expected):
        await fetch(fake, (BTC_USD,))


async def test_a_refusal_carries_neither_the_host_nor_the_key() -> None:
    """The one place in this change where an exception could carry a credential.

    `EndpointSet` builds the message and this source hands it a header, so nothing here
    should be able to put the key in it -- which is exactly why it is worth asserting rather
    than reasoning about. A 401 is the status most likely to tempt somebody into quoting the
    credentials that failed.
    """
    fake = PriceFake(coingecko=ScriptedVendor(Reply(status=401)))

    with pytest.raises(ProviderResponseError) as caught:
        await fetch(fake, (BTC_USD,))

    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert SYNTHETIC_COINGECKO_KEY not in rendered
    assert COINGECKO_HOST not in rendered
    assert "CoinGecko" in rendered


# --------------------------------------------------------------------------------------
# Conformance, decided by mypy rather than by isinstance
# --------------------------------------------------------------------------------------

_CONFORMS: PriceSource = CoinGeckoPriceSource(
    httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
    settings=price_settings(coingecko_api_key=SYNTHETIC_COINGECKO_KEY),
)
