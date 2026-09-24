"""Four scripted price vendors behind one mock transport, and the bodies they send.

The same shape as `tests/providers/chains/harness.py`, and for the same reasons, with one
difference that matters.

**The bodies are hand-written strings, never `json.dumps` of a Python value.** A price is a
number in a document, and this issue exists because one vendor sends it as a JSON *number*
whose digits `json.loads` destroys before any of our code runs. Building a body by
serialising `Decimal("0.04228645")` -- or, worse, `0.04228645` -- would mean the test's
expectation and the code under test had both been through the same conversion, which is the
verifier sharing state with its subject. A literal in a triple-quoted string has been
through nothing.

**Routing is by host, and the hosts are read off the shipped constants.** `KRAKEN_API_URL`
and its siblings are module constants rather than settings, so a fake that matched a
hard-coded `api.kraken.com` would pass for a source pointed anywhere at all. Reading
`httpx.URL(KRAKEN_API_URL).host` means the fake follows the source; the shipped values
themselves are pinned once, as literals, in `test_registry.py`.

**The Kaspa source is the exception and is given fictional hosts**, because it reads
`settings.kaspa_api_url` -- the same two variables the chain provider reads. `example` is
reserved by RFC 2606 and resolves nowhere, so a test that somehow escaped its mock
transport fails to connect rather than reaching somebody's real node.

**Nothing here sleeps and nothing here reads a clock.** Every duration is injected through
`tests/providers/harness.py`'s conventions, so no assertion in this suite is a measurement
of how fast the machine running it happened to be.

No API key appears anywhere in this file, including a fake one shaped like a real one. The
CoinGecko source is driven with a `SecretStr` wrapping an obviously synthetic sentinel, and
`test_coingecko.py` asserts that sentinel reaches no log and no message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import httpx
from pydantic import SecretStr

from portfolio.config import Settings
from portfolio.providers.http import HostRateLimiter, RetryPolicy, build_http_client
from portfolio.providers.prices.coinbase import COINBASE_API_URL
from portfolio.providers.prices.coingecko import COINGECKO_DEMO_API_URL
from portfolio.providers.prices.kraken import KRAKEN_API_URL

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

# --------------------------------------------------------------------------------------
# Where each vendor lives, derived rather than copied
# --------------------------------------------------------------------------------------

KRAKEN_HOST: Final = httpx.URL(KRAKEN_API_URL).host
COINBASE_HOST: Final = httpx.URL(COINBASE_API_URL).host
COINGECKO_HOST: Final = httpx.URL(COINGECKO_DEMO_API_URL).host

#: The Kaspa node's two configured instances. Fictional, because this source reads them out
#: of `Settings` and a test may therefore choose them -- which is what lets the failover
#: arms script a primary and a fallback without naming anybody's real node.
KASPA_PRIMARY_HOST: Final = "kaspa-primary.example"
KASPA_FALLBACK_HOST: Final = "kaspa-fallback.example"
KASPA_PRIMARY_URL: Final = f"https://{KASPA_PRIMARY_HOST}"
KASPA_FALLBACK_URL: Final = f"https://{KASPA_FALLBACK_HOST}"

#: A CoinGecko key that is obviously not one. Rule 3 forbids a real key and also forbids a
#: fake one shaped like a real one -- a plausible-looking string is what a secret scanner
#: has to flag, and a fixture that trips the scanner is a fixture somebody weakens the
#: scanner for. This is a sentence, and `test_coingecko.py` asserts it reaches nothing.
SYNTHETIC_COINGECKO_KEY: Final = "not-a-real-key-for-tests-only"


# --------------------------------------------------------------------------------------
# Bodies, written by hand
# --------------------------------------------------------------------------------------

#: The Kaspa node's measured body, byte for byte, with the price as a JSON **number**.
#:
#: Written as a literal and never assembled, because its digits are the subject of this
#: whole issue: `test_kaspa.py` asserts against the characters between the colon and the
#: brace, and an assembled body would let a `float` into the expectation by the same door
#: it would enter the implementation.
KASPA_PRICE_BODY: Final = '{"price": 0.04228645}'

#: The digits inside that body. Duplicated deliberately: a test that sliced them back out
#: of `KASPA_PRICE_BODY` would agree with any body at all.
KASPA_PRICE_DIGITS: Final = "0.04228645"

#: Kraken's measured prices, as the **strings** it sends them as, keyed by its own pair
#: code. Trailing zeros included, because that is what the vendor sends and a value that
#: went through a double comes back without them.
KRAKEN_PRICES: Final[Mapping[str, str]] = {
    "XXBTZUSD": "86000.10000",
    "XXBTZEUR": "79000.34000",
    "KASUSD": "0.04228645",
    "KASEUR": "0.03885120",
}


def kraken_body(prices: Mapping[str, str], *, errors: Sequence[str] = ()) -> str:
    """Kraken's ticker envelope: `error`, `result`, and `c` as `[price, lot volume]`.

    The whole ticker entry is rendered, not just `c`, so a parser reaching for the wrong
    field -- `a` is the ask, `b` the bid, `o` the open -- gets a plausible number rather
    than a `KeyError`, and the test that says "the last trade, not the mid-point" has
    something to actually distinguish.

    The numbers beside the price are deliberately different from it for the same reason.
    """
    entries = ",".join(
        f'"{code}":{{'
        f'"a":["{price}1","1","1.000"],'
        f'"b":["{price}2","1","1.000"],'
        f'"c":["{price}","0.00100000"],'
        f'"v":["100.00000000","200.00000000"],'
        f'"p":["{price}3","{price}4"],'
        f'"t":[1000,2000],'
        f'"l":["{price}5","{price}6"],'
        f'"h":["{price}7","{price}8"],'
        f'"o":"{price}9"'
        f"}}"
        for code, price in prices.items()
    )
    rendered_errors = ",".join(f'"{error}"' for error in errors)
    return f'{{"error":[{rendered_errors}],"result":{{{entries}}}}}'


def kraken_echo(prices: Mapping[str, str] = KRAKEN_PRICES) -> Callable[[httpx.Request], str]:
    """A Kraken that answers about exactly the pair codes the request asked for.

    Which is what the real one does, and what makes
    `test_an_entry_for_a_pair_nobody_asked_about_is_refused` a real test: an unrequested
    entry has to be scripted on purpose rather than being the accident a fixed body makes
    of every call. Codes the table has no price for are simply absent from the answer,
    which is the partial-answer case failover exists for.
    """

    def render(request: httpx.Request) -> str:
        asked = request.url.params.get("pair", "").split(",")
        return kraken_body({code: prices[code] for code in asked if code in prices})

    return render


def coinbase_body(*, base: str, currency: str, amount: str) -> str:
    """Coinbase's spot envelope, with the amount as the **string** it sends.

    `base` and `currency` are arguments rather than derived from the request, so that a
    response about the wrong pair has to be asked for deliberately -- which is what makes
    the test for the echoed-pair check a real test rather than an accident of the fixture.
    """
    return f'{{"data":{{"amount":"{amount}","base":"{base}","currency":"{currency}"}}}}'


def coingecko_body(entries: Mapping[str, Mapping[str, str]]) -> str:
    """CoinGecko's simple-price document, with every price as a JSON **number**.

    The second float boundary in this change, and it gets its own body rather than sharing
    Kaspa's: the two vendors are different shapes and the assertion that CoinGecko's digits
    survive has to be made against CoinGecko's document.
    """
    rendered = ",".join(
        f'"{coin}":{{'
        + ",".join(f'"{currency}":{amount}' for currency, amount in prices.items())
        + "}"
        for coin, prices in entries.items()
    )
    return f"{{{rendered}}}"


def coingecko_echo(
    entries: Mapping[str, Mapping[str, str]],
) -> Callable[[httpx.Request], str]:
    """A CoinGecko answering about exactly the coin ids the query asked for.

    Which is what the real one does, and what makes
    `test_a_coin_nobody_asked_about_is_refused` a real test rather than something every
    other call in the file trips over. The currencies are left alone: `vs_currencies`
    applies to the whole call, so a coin's extra currency is ours rather than the vendor's
    -- an asymmetry the parser deliberately keeps and that this renderer must not hide.
    """

    def render(request: httpx.Request) -> str:
        asked = request.url.params.get("ids", "").split(",")
        return coingecko_body({coin: entries[coin] for coin in asked if coin in entries})

    return render


# --------------------------------------------------------------------------------------
# The script
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    """One scripted answer from one vendor.

    The default is a 200 with an empty JSON object, so a test that only cares *which* host
    was asked writes `Reply()` and says nothing else. Everything a test does spell out is
    therefore the thing under test.
    """

    status: int = 200
    body: str = "{}"
    headers: Mapping[str, str] = field(default_factory=dict)
    error: BaseException | None = None
    """Raised instead of answering, for the transport-failure arms."""
    renderer: Callable[[httpx.Request], str] | None = None
    """Builds the body from the request, for a vendor that echoes what it was asked.

    The chain harness makes the same choice and for the same reason: a fixed body means a
    fake answers about whatever the fixture author believed the source would ask for, and a
    test for "an answer about something nobody requested is refused" would then pass by
    accident on every other test in the file. With a renderer, answering about an
    unrequested pair has to be asked for **deliberately**, with `body=`.
    """

    def render(self, request: httpx.Request) -> str:
        return self.body if self.renderer is None else self.renderer(request)


class ScriptedVendor:
    """One vendor: a queue of replies whose last entry repeats.

    `ScriptedVendor(Reply(status=429))` is "always throttled" and
    `ScriptedVendor(Reply(status=429), Reply(body=...))` is "throttled once, then fine".
    The repetition matters for the retry arms: the transport may attempt three times, and a
    script that ran out would answer `IndexError` and turn a retry test into a crash.
    """

    def __init__(self, *replies: Reply) -> None:
        self._replies: tuple[Reply, ...] = replies or (Reply(),)
        self.requests: list[httpx.Request] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        if reply.error is not None:
            raise reply.error
        return httpx.Response(
            reply.status, headers=dict(reply.headers), content=reply.render(request)
        )


class PriceFake:
    """Every vendor behind one `httpx.MockTransport`, routed by host.

    Routing on the host rather than on a whole URL is deliberate, for the reason the chain
    harness gives: a source builds its own URL out of a constant and a path, and a fake
    that matched the whole URL would pass only for the exact string the test already
    believed the source would produce. Matching the host means the test finds out what path
    and what query the source actually sent -- which is how criterion 8's "one call returns
    every configured pair" can assert the request rather than assume it.

    A request to an unscripted host is an `AssertionError` rather than a 404, because a
    source calling a vendor nobody scripted is a bug in the test or in the source, and a
    404 would be indistinguishable from a vendor refusing.
    """

    def __init__(
        self,
        *,
        kraken: ScriptedVendor | None = None,
        coinbase: ScriptedVendor | None = None,
        coingecko: ScriptedVendor | None = None,
        kaspa: ScriptedVendor | None = None,
        kaspa_fallback: ScriptedVendor | None = None,
    ) -> None:
        self.kraken = kraken if kraken is not None else ScriptedVendor()
        self.coinbase = coinbase if coinbase is not None else ScriptedVendor()
        self.coingecko = coingecko if coingecko is not None else ScriptedVendor()
        self.kaspa = kaspa if kaspa is not None else ScriptedVendor()
        self.kaspa_fallback = kaspa_fallback if kaspa_fallback is not None else ScriptedVendor()
        self._vendors: dict[str, ScriptedVendor] = {
            KRAKEN_HOST: self.kraken,
            COINBASE_HOST: self.coinbase,
            COINGECKO_HOST: self.coingecko,
            KASPA_PRIMARY_HOST: self.kaspa,
            KASPA_FALLBACK_HOST: self.kaspa_fallback,
        }
        #: Every request in the order it was made, across every host. Order is what tells
        #: "the fallback was asked" apart from "the fallback was asked first".
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        vendor = self._vendors.get(request.url.host)
        if vendor is None:  # pragma: no cover - a test asking an unscripted host is a bug
            message = f"a price source called an unscripted host: {request.url.host}"
            raise AssertionError(message)
        return vendor.answer(request)

    @property
    def counts(self) -> dict[str, int]:
        """How many requests each host received, which is the call budget in one number."""
        return {host: len(vendor.requests) for host, vendor in self._vendors.items()}

    @property
    def hosts_in_order(self) -> list[str]:
        """The sequence of hosts asked, so failover order is asserted rather than inferred."""
        return [request.url.host for request in self.requests]

    def queries_of(self, host: str) -> list[str]:
        """Every query string one vendor was sent, in order and as text.

        Text rather than a parsed mapping: what criterion 8 claims is that **one** request
        carried every pair, and a mapping would hide whether the codes arrived in one
        `pair=` parameter or in four.
        """
        return [request.url.query.decode() for request in self._vendors[host].requests]

    def paths_of(self, host: str) -> list[str]:
        """Every path one vendor was sent, in order and without its query."""
        return [request.url.path for request in self._vendors[host].requests]


# --------------------------------------------------------------------------------------
# Building the sources under test
# --------------------------------------------------------------------------------------


def price_settings(
    *,
    coingecko_api_key: str | None = None,
    kaspa_api_url: str = KASPA_PRIMARY_URL,
    kaspa_api_fallback_url: str = "",
) -> Settings:
    """A real `Settings`, not a stub, so pydantic's own validation is exercised too.

    Built directly rather than through the environment: `get_settings` is cached
    process-wide and a test that monkeypatched `PORTFOLIO_*` would be sharing state with
    every other test in the run. Every source takes the object, so nothing global moves.

    `coingecko_api_key` defaults to `None`, which is the unkeyed deployment -- the shape
    criterion 5 calls "without a key", and the one in which the CoinGecko source is not
    constructed at all.
    """
    return Settings(
        coingecko_api_key=None if coingecko_api_key is None else SecretStr(coingecko_api_key),
        kaspa_api_url=kaspa_api_url,
        kaspa_api_fallback_url=kaspa_api_fallback_url,
    )


def price_client(fake: PriceFake, *, max_attempts: int = 3) -> httpx.AsyncClient:
    """The production client, over the fake, with every duration injected.

    `build_http_client` rather than an assembled transport, because that is what #10 will
    hand these sources and it is the wiring whose timeouts and retry loop the failover
    behaviour composes with. A zero backoff and a zero interval, because this suite's
    assertions are about how many requests were made and to whom; how long they waited is
    `tests/providers/test_http.py`'s subject and is already covered there.
    """

    async def no_sleep(_milliseconds: int) -> None:
        return

    return build_http_client(
        transport=httpx.MockTransport(fake.handler),
        policy=RetryPolicy(max_attempts=max_attempts, base_backoff_ms=0, max_backoff_ms=0),
        limiter=HostRateLimiter(min_interval_ms=0, clock=lambda: 0, sleep=no_sleep),
        jitter=lambda bound: bound,
        sleep=no_sleep,
    )
