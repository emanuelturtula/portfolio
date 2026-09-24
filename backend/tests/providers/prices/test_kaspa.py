"""The Kaspa price source: the JSON number that is this issue's real subject.

Kraken and Coinbase send a price as a JSON **string**, so nothing can happen to it on the
way in. The Kaspa node sends `{"price": 0.04228645}` -- a JSON **number** -- and
`json.loads` turns that into a `float` before a single line of this application runs. Rule
2 is not "do not write `float`"; it is "do not let a monetary value pass through binary
floating point", and by the time a parser could refuse one, it already has.

## How the expectation is built, which is the point of this file

`test_the_price_keeps_the_digits_the_vendor_sent` asserts against **the literal string in
the fixture body**. It does not decode the body and compare the result to itself, and it
does not build a `Decimal` by any route the implementation also takes. The digits
`0.04228645` are typed out, once, in `harness.py`, beside the body that contains them --
and the test compares the parsed amount to `Decimal` of that literal.

`test_plain_json_loads_produces_a_different_number_from_the_same_body` is the companion,
and without it the test above proves nothing about the hook: it shows that decoding the
same bytes the ordinary way gives a value that is **not** those digits. The hook is
therefore doing work rather than merely being present.

## The currency is assumed, and the assumption is tested as an assumption

The body names no currency. USD is an inference from the number's magnitude, which is not
evidence. So this source declares one pair, is never asked for a EUR one, and is last among
the key-free sources for the pair it does answer -- `test_registry.py` pins that ordering;
what is pinned here is that the constant exists and that the source refuses to answer
anything else.

Every host in this file is `.example`, reserved by RFC 2606. No address appears at all: a
price endpoint takes none, which is the one pleasant thing about it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.providers.errors import (
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import ASSET_PRICE, ENDPOINT_EXTENSION
from portfolio.providers.prices.base import EUR, KAS, USD, PriceQuote
from portfolio.providers.prices.kaspa import (
    ASSUMED_CURRENCY,
    KASPA,
    PRICE_FIELD,
    PRICE_PAIRS,
    PRICE_PATH,
    KaspaPriceSource,
    parse_price,
)
from tests.providers.prices.harness import (
    KASPA_FALLBACK_HOST,
    KASPA_FALLBACK_URL,
    KASPA_PRICE_BODY,
    KASPA_PRICE_DIGITS,
    KASPA_PRIMARY_HOST,
    KASPA_PRIMARY_URL,
    PriceFake,
    Reply,
    ScriptedVendor,
    price_client,
    price_settings,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.prices.base import PriceSource

KAS_USD: Final = (KAS, USD)
KAS_EUR: Final = (KAS, EUR)


def source_over(
    fake: PriceFake,
    *,
    fallback_url: str = "",
) -> tuple[KaspaPriceSource, httpx.AsyncClient]:
    """The source and the client it holds, so a test can close the client afterwards."""
    client = price_client(fake)
    settings = price_settings(
        kaspa_api_url=KASPA_PRIMARY_URL,
        kaspa_api_fallback_url=fallback_url,
    )
    return KaspaPriceSource(client, settings=settings), client


async def fetch(
    fake: PriceFake, pairs: Sequence[tuple[str, str]] = (KAS_USD,)
) -> Sequence[PriceQuote]:
    """One `fetch` against the scripted node, with the client closed afterwards."""
    source, client = source_over(fake)
    async with client:
        return await source.fetch(pairs)


def priced(body: str = KASPA_PRICE_BODY) -> PriceFake:
    """A node answering one body. `body` is a string, never a serialised Python value."""
    return PriceFake(kaspa=ScriptedVendor(Reply(body=body)))


# --------------------------------------------------------------------------------------
# The float test, and the companion that proves the hook does work
# --------------------------------------------------------------------------------------


async def test_the_price_keeps_the_digits_the_vendor_sent() -> None:
    """The measured body, through the real source, against the literal in the fixture.

    Three assertions and none is redundant.

    The **type** is `Decimal`, because a `float` reaching here is the bug. The **digits**
    are the characters in the body -- `str()` rather than `==`, because `==` on a `Decimal`
    ignores a trailing zero and could not tell `0.04228645` from `0.042286450`. And the
    **value** equals `Decimal(KASPA_PRICE_DIGITS)`, a constant this suite typed out rather
    than a number the code under test produced.

    Nothing in the expectation has been through `decode_json`, through `require_price`, or
    through any other step the implementation takes. That is the whole discipline: an
    expectation built the way the code builds it is the verifier sharing state with its
    subject.
    """
    quotes = await fetch(priced())

    assert len(quotes) == 1
    amount = quotes[0].amount

    assert isinstance(amount, Decimal)
    assert str(amount) == KASPA_PRICE_DIGITS
    assert amount == Decimal(KASPA_PRICE_DIGITS)


def test_plain_json_loads_produces_a_different_number_from_the_same_body() -> None:
    """The companion. Without it the test above is an assertion about an assertion.

    Decoding the fixture the ordinary way gives a `float`, and `Decimal` of that float is
    `0.0422864500000000032020608387028914876282215118408203125` -- twenty-odd digits that
    are not a display artefact but the value itself, and that every multiplication by a
    quantity carries forward into a portfolio total.

    The last assertion is what makes it dangerous rather than merely wrong: the float
    *prints* as the right number. Nobody reading a log or a debugger would see this.
    """
    naive = json.loads(KASPA_PRICE_BODY)[PRICE_FIELD]

    assert isinstance(naive, float)
    assert Decimal(naive) != Decimal(KASPA_PRICE_DIGITS)
    assert str(Decimal(naive)) != KASPA_PRICE_DIGITS
    assert str(naive) == KASPA_PRICE_DIGITS


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("0.04228645", id="the measured price"),
        pytest.param("0.10000000", id="trailing zeros a float round trip drops"),
        pytest.param("0.1", id="the value IEEE-754 cannot represent at all"),
        pytest.param("1e-8", id="exponent form, which a vendor is free to send"),
        pytest.param("0.123456789012345678901234567890", id="more digits than a double holds"),
    ],
)
async def test_every_shape_of_number_this_endpoint_could_send_survives(digits: str) -> None:
    """The property, over the shapes a vendor may choose, not only the one measured today.

    The endpoint returns whatever its own pricing source gives it, and nothing in its
    documentation constrains the rendering. A parser that happened to be exact for eight
    decimal places and lossy for ten would pass a test written only against the measured
    body -- and would then be wrong on the day the vendor's upstream changed.
    """
    quotes = await fetch(priced(f'{{"price": {digits}}}'))

    assert quotes[0].amount == Decimal(digits)
    assert str(quotes[0].amount) == str(Decimal(digits))


def test_the_parser_alone_carries_the_digits_too() -> None:
    """`parse_price` without a client, so the boundary is located rather than assumed.

    If this passes and the `fetch` test above fails, the loss is in the transport or the
    endpoint set; if both fail, it is in the decoder. A suite that only tested through the
    client would leave that diagnosis to whoever read the failure.
    """
    quote = parse_price(KASPA_PRICE_BODY)

    assert quote == PriceQuote(
        asset_symbol=KAS,
        quote_currency=USD,
        amount=Decimal(KASPA_PRICE_DIGITS),
        source=KASPA,
    )
    assert str(quote.amount) == KASPA_PRICE_DIGITS


# --------------------------------------------------------------------------------------
# The currency is assumed, and the source behaves like something making an assumption
# --------------------------------------------------------------------------------------


def test_the_assumed_currency_is_usd_and_is_a_named_constant() -> None:
    """The guess has a name, so it has somewhere to be documented and something to assert.

    `USD` written inline at the call site would be indistinguishable from a currency read
    out of a body. A constant called `ASSUMED_CURRENCY` is a sentence in the code saying
    that nobody was told this, and it is what a future vendor change would delete rather
    than edit.
    """
    assert ASSUMED_CURRENCY == USD
    assert frozenset({(KAS, USD)}) == PRICE_PAIRS


async def test_a_eur_pair_is_never_answered_and_never_requested() -> None:
    """The blast radius of the guess, bounded: one asset, one currency, and no EUR at all.

    A source that answered KAS/EUR with this number would be valuing someone's holdings in
    a currency nobody named, and the error would be roughly fifteen percent with nothing
    anywhere saying so. Asserted as **no request** rather than as an empty answer, because
    an empty answer after a round trip still costs the budget and still means the source
    was willing to ask.
    """
    fake = priced()

    quotes = await fetch(fake, (KAS_EUR,))

    assert quotes == ()
    assert fake.requests == []


async def test_a_pair_it_cannot_answer_alongside_one_it_can() -> None:
    """A caller that did not filter gets the answerable pair and no error.

    `fetch_prices` filters already, so this is the second guard -- and the second guard is
    what stops a future caller (a CLI command, a one-off script) turning an unsupported pair
    into an exception that costs the supported pair its price.
    """
    quotes = await fetch(priced(), (KAS_EUR, KAS_USD))

    assert [quote.pair for quote in quotes] == [KAS_USD]


# --------------------------------------------------------------------------------------
# The request itself
# --------------------------------------------------------------------------------------


async def test_the_request_is_one_get_to_the_documented_path_with_the_right_label() -> None:
    """One request, the measured path, no query -- and the endpoint label the log allows.

    The label is asserted on the request's own extensions rather than by reading a log,
    because that is where the transport reads it from: a label that never reaches the
    extensions renders as `<unlabelled>` and no amount of correct spelling elsewhere helps.
    """
    fake = priced()

    await fetch(fake)

    assert fake.counts[KASPA_PRIMARY_HOST] == 1
    assert fake.paths_of(KASPA_PRIMARY_HOST) == [PRICE_PATH]
    assert fake.queries_of(KASPA_PRIMARY_HOST) == [""]
    request = fake.kaspa.requests[0]
    assert request.method == "GET"
    assert request.extensions[ENDPOINT_EXTENSION] == ASSET_PRICE


async def test_a_second_instance_answers_when_the_first_does_not() -> None:
    """The same two configured variables the chain provider reads, failing over.

    Counted per host rather than asserted on the result, for the reason the chain harness
    gives: a source that hammered a refusing primary and then succeeded on the fallback
    returns exactly the same quote as one that moved on after the first refusal, and only
    the per-host counts tell them apart.
    """
    fake = PriceFake(
        kaspa=ScriptedVendor(Reply(status=503)),
        kaspa_fallback=ScriptedVendor(Reply(body=KASPA_PRICE_BODY)),
    )
    source, client = source_over(fake, fallback_url=KASPA_FALLBACK_URL)

    async with client:
        quotes = await source.fetch([KAS_USD])

    assert quotes[0].amount == Decimal(KASPA_PRICE_DIGITS)
    assert fake.hosts_in_order[-1] == KASPA_FALLBACK_HOST
    assert fake.counts[KASPA_FALLBACK_HOST] == 1


# --------------------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json at all", id="not JSON"),
        pytest.param("[1, 2, 3]", id="a JSON array rather than an object"),
        pytest.param('{"cost": 0.04}', id="the field under another name"),
        pytest.param("{}", id="an empty object"),
        pytest.param('{"price": null}', id="an explicit null"),
        pytest.param('{"price": "0.04228645"}', id="a string, which this vendor never sends"),
    ],
)
async def test_a_body_that_is_not_a_price_is_a_typed_refusal(body: str) -> None:
    """`ProviderResponseError`, not a `KeyError` or a `TypeError` out of the parser.

    The string row is the interesting one and it is **accepted**, not refused: `require_price`
    takes a string because two of the four vendors send one, so a Kaspa node that started
    sending strings would keep working. It is listed here so that the behaviour is a
    decision rather than something nobody looked at -- see the assertion below.
    """
    if body == '{"price": "0.04228645"}':
        quotes = await fetch(priced(body))
        assert quotes[0].amount == Decimal(KASPA_PRICE_DIGITS)
        return

    with pytest.raises(ProviderResponseError):
        await fetch(priced(body))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("0", id="zero, which is not a price"),
        pytest.param("-1.5", id="negative, which is not a price either"),
        pytest.param("NaN", id="NaN, which json.loads accepts out of the box"),
        pytest.param("Infinity", id="infinity, likewise"),
        pytest.param("true", id="a bool, which is an int subclass"),
    ],
)
async def test_a_number_that_is_not_a_price_is_refused(value: str) -> None:
    """Zero is the one that matters, and it is refused for criterion 3's reason.

    A stored price of zero values every holding of that asset at nothing, and a total
    computed from it is a confident, wrong, believable number -- which is precisely the
    failure the issue's third criterion is about. A refusal leaves the pair unanswered and
    turns it into a reason instead.

    `NaN` and `Infinity` are here because `json.loads` accepts both by default and
    `Decimal("NaN")` constructs happily; a NaN in a money column compares false against
    itself forever.
    """
    with pytest.raises(ProviderResponseError):
        await fetch(priced(f'{{"price": {value}}}'))


async def test_a_refusal_never_quotes_the_body_or_names_a_host() -> None:
    """Rule 3 does not pause because the vendor misbehaved.

    A price is public market data, so the number itself is not a disclosure -- but the
    message is a string that reaches a log, and a parser that quotes bodies is a habit that
    leaks an address at the next boundary. The host is the other half: `providers/http.py`
    goes to some trouble to keep a URL out of the logs, and an exception message naming one
    walks around all of it.
    """
    with pytest.raises(ProviderResponseError) as caught:
        await fetch(priced('{"secret": "0.04228645"}'))

    rendered = f"{caught.value}{caught.value!r}"
    assert KASPA_PRIMARY_HOST not in rendered
    assert "secret" not in rendered


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(429, ProviderRateLimitedError, id="throttled"),
        pytest.param(503, ProviderUnavailableError, id="unavailable"),
        pytest.param(500, ProviderUnavailableError, id="a server error"),
        pytest.param(404, ProviderResponseError, id="the endpoint is not there"),
        pytest.param(401, ProviderResponseError, id="behind an auth proxy"),
    ],
)
async def test_a_failing_vendor_produces_the_typed_error_its_status_means(
    status: int,
    expected: type[Exception],
) -> None:
    """The three-way split, exercised through this source so nothing is assumed of it.

    `EndpointSet` decides these and `tests/providers/test_http.py` covers the decision. What
    is asserted here is that this source goes through it rather than around it -- a source
    that caught everything and raised one type would be invisible to that suite and would
    make every failure look the same to `fetch_prices`, which catches `ProviderError` and
    moves on regardless.
    """
    fake = PriceFake(kaspa=ScriptedVendor(Reply(status=status)))

    with pytest.raises(expected):
        await fetch(fake)


async def test_a_transport_failure_reaches_the_caller_as_a_provider_error() -> None:
    """`httpx` stops at this boundary, which is what the layering contract requires.

    A raw `httpx.ConnectError` escaping here would reach a service -- a layer
    `import-linter` forbids from importing `httpx` at all -- as an exception it cannot name
    in an `except` clause. The `__cause__` is asserted as well, because #7's fourth closing
    lesson was thirty-seven tests all checking the type and none checking what the
    traceback pointed at.
    """
    failure = httpx.ConnectError("the node did not answer")
    fake = PriceFake(kaspa=ScriptedVendor(Reply(error=failure)))

    with pytest.raises(ProviderUnavailableError) as caught:
        await fetch(fake)

    assert isinstance(caught.value.__cause__, httpx.TransportError)


# --------------------------------------------------------------------------------------
# Conformance, decided by mypy rather than by isinstance
# --------------------------------------------------------------------------------------
#
# `PriceSource` is deliberately not `@runtime_checkable`: an `isinstance` check compares
# attribute names and says nothing about whether `fetch` takes a sequence or whether it is
# a coroutine function. The real check is this annotated assignment, which `mypy --strict`
# decides in the gate -- the same mechanism `tests/providers/fakes.py` uses for
# `ChainProvider`, and the one `tests/providers/test_protocol.py` proves can fail.

_CONFORMS: PriceSource = KaspaPriceSource(
    httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
    settings=price_settings(),
)
