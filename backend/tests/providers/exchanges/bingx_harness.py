"""A fake BingX venue behind one `httpx.MockTransport`, and the bodies it sends.

The shape of `bitget_harness.py`, for the same reasons, with the differences BingX makes.

**It behaves like the owner's live probe, not like the documentation, where the two
differ.** Spec 017's first table records what the probe of 2026-09-26/27 found, and this fake
is written from that table:

* `startTime` and `endTime` are **both inclusive**: `[T, T]` returns the fill at `T`;
* no `symbol` is needed: without one, every symbol's fills come back, each naming its own;
* the page is **ascending by time**, and by id within a millisecond;
* every refusal arrives on **HTTP 200** with a non-zero integer `code`;
* an empty window is code `0` with `data.fills: []`, never `100204`.

What the probe could not establish is a switch: `silent_cap` is a venue that serves fewer
fills than `limit` without saying so, and `ignore_start_time` is a venue that answers from
the oldest fill whatever `startTime` says.

**Every request is verified here, with `hmac` and `hashlib` directly.** Never with
`portfolio.providers.exchanges.signing`: a verifier that called the signer would agree with
it whatever it did. The signed string is taken from the bytes the request actually carried
-- `request.url.query`, everything before `&signature=` -- so a provider that signs one
query and sends another fails here even if both are sorted, and a `signature` that is not
the last parameter fails too. A request that does not verify is answered the way the probe
saw BingX answer one: code `100001` on HTTP 200, which the provider maps to an auth error, so
a signing bug fails every test that makes a request and not only the tests about signing.
`signature_failures` records what was wrong.

**The documented facts are written here as literals, not read off the source.** The path
and the header name are what BingX documents, and a fake that read `FILLS_PATH` from the
provider would route whatever path the provider chose. The host is the exception, read off
`BINGX_API_URL` as the Bitget harness reads its host, and pinned once as a literal in
`test_bingx.py`.

**The bodies are hand-written strings, never `json.dumps` of a Python value.** Two of the
fill's fields are float64 on the wire (`commission`) or carry float noise (`quoteQty`), and
the whole point of the parser is that the venue's digits arrive as the venue wrote them. A
body built by serialising a `float` would have gone through the very conversion the
provider has to undo, and the test's expectation would share it.

**Two symbols, with overlapping trade ids.** `spread_fills` alternates `ETH-USDT` and
`BTC-USDT` and gives the two fills of each pair the same `id`, because spec 017 infers ids are
per symbol and a provider that did not namespace them would silently merge two fills.

**Nothing here sleeps, and nothing reads a clock.** The provider's clock is injected
(`FixedClock`), and the client is `tests/providers/harness.py`'s, whose sleep and jitter are
injected too.

No real credential appears anywhere. The secret is `SECRET_KEY`, the literal BingX's own V3
signature page signs its example with, and the key is a sentence; neither is assigned to a
name containing the venue's name, which is the shape `.gitleaks.toml`'s
`exchange-api-credential` rule refuses.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import httpx
from pydantic import SecretStr

from portfolio.providers.exchanges.base import FillWindow
from portfolio.providers.exchanges.bingx import BINGX_API_URL, BingXProvider
from portfolio.providers.exchanges.credentials import Credentials
from tests.providers.harness import RecordingSleep, retrying_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from portfolio.providers.exchanges.base import FillPage

# --------------------------------------------------------------------------------------
# What BingX documents, written down rather than read off the provider
# --------------------------------------------------------------------------------------

BINGX_HOST: Final = httpx.URL(BINGX_API_URL).host

#: Query transaction details, V3 and V1 alike (read 2026-09-26).
DOCUMENTED_FILLS_PATH: Final = "/openApi/spot/v1/trade/myTrades"

#: The one credential header, as the V3 signature page spells it.
KEY_HEADER: Final = "X-BX-APIKEY"

#: "Default 500, maximum 1000". A request without `limit` gets the default, and one asking
#: for more gets the maximum.
DOCUMENTED_DEFAULT_LIMIT: Final = 500
DOCUMENTED_MAX_LIMIT: Final = 1000

#: The parameter the signature travels in, and where it must be: last.
SIGNATURE_PARAMETER: Final = "signature"

#: What an error envelope carries as `timestamp`. Arbitrary but fixed, so no body depends on
#: a clock.
SERVER_TIMESTAMP: Final = 1695865274510

# --------------------------------------------------------------------------------------
# Synthetic credentials
# --------------------------------------------------------------------------------------

#: The secret both golden vectors were computed under, with `openssl`, outside this code. It
#: is the literal V3's signature page uses in its own `openssl` command.
SIGNING_SENTINEL: Final = "SECRET_KEY"
ACCESS_KEY_SENTINEL: Final = "synthetic-access-key-for-tests-only"


def synthetic_credentials(
    *,
    api_key: str = ACCESS_KEY_SENTINEL,
    api_secret: str = SIGNING_SENTINEL,
    passphrase: str | None = None,
) -> Credentials:
    """`Credentials` holding the sentinels, each wrapped as production wraps it.

    No passphrase by default, because BingX has none; a test passes one to see it refused.
    """
    return Credentials(
        api_key=SecretStr(api_key),
        api_secret=SecretStr(api_secret),
        passphrase=None if passphrase is None else SecretStr(passphrase),
    )


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------

#: The second golden vector's timestamp, `1684814440729`, as an instant. Converted by hand
#: with `date -u -d @1684814440.729 '+%Y-%m-%dT%H:%M:%S.%3NZ'` -> 2023-05-23T04:00:40.729Z.
GOLDEN_TIMESTAMP_MS: Final = 1684814440729
GOLDEN_NOW: Final = datetime(2023, 5, 23, 4, 0, 40, 729000, tzinfo=UTC)

#: The window most tests ask about: two days. `date -u -d 2023-09-27T00:00:00Z +%s` is
#: 1695772800 and `date -u -d 2023-09-29T00:00:00Z +%s` is 1695945600.
WINDOW_SINCE_MS: Final = 1695772800000
WINDOW_UNTIL_MS: Final = 1695945600000
WINDOW: Final = FillWindow(
    since=datetime(2023, 9, 27, tzinfo=UTC),
    until=datetime(2023, 9, 29, tzinfo=UTC),
)


class FixedClock:
    """A clock that always answers the same instant, and counts how often it was read."""

    def __init__(self, moment: datetime = GOLDEN_NOW) -> None:
        self.moment = moment
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.moment


class TickingClock:
    """A clock one second later on every read: a provider that re-signed would show it."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return GOLDEN_NOW + timedelta(seconds=self.reads)


# --------------------------------------------------------------------------------------
# Fills, rendered by hand
# --------------------------------------------------------------------------------------

#: The order id of the documented sample. Every scripted fill carries it unless told
#: otherwise: BingX's order ids are 61-bit numbers, and one this size is what a parser that
#: went through a float would round.
SAMPLE_ORDER_ID: Final = 1745362930595004400


@dataclass(frozen=True, slots=True)
class VenueFill:
    """One fill as the fake venue holds it, rendered in the documented field order.

    The defaults are the first fill of the documented sample, whose `commission` is a bare
    JSON number because the docs type it `float64`. `overrides` replaces a field with a raw
    JSON fragment -- `{"id": '"36767057"'}` -- or removes it with `None`, and a key the venue
    does not send is appended. Fragments are raw text, so a test can send a string where an
    integer is documented, a `null`, or an escape the parser has to survive.

    `trade_id`, `executed_ms` and `symbol` stay typed because the fake filters and orders by
    them; a malformed `id`, `time` or `symbol` is an override, and the fake still pages by
    the typed value.
    """

    trade_id: int
    executed_ms: int
    symbol: str = "BTC-USDT"
    order_id: int = SAMPLE_ORDER_ID
    price: str = "46820.155"
    qty: str = "0.1430254"
    quote_qty: str = "6696.471396937"
    commission: str = "-0.000046483255"
    commission_asset: str = "BTC"
    is_buyer: bool = True
    overrides: Mapping[str, str | None] = field(default_factory=dict)


def _render_object(fields: Mapping[str, str]) -> str:
    return "{" + ",".join(f'"{name}":{fragment}' for name, fragment in fields.items()) + "}"


def render_fill(fill: VenueFill) -> str:
    """The fill object as the documented sample lays it out, as text."""
    fields: dict[str, str] = {
        "symbol": f'"{fill.symbol}"',
        "id": str(fill.trade_id),
        "orderId": str(fill.order_id),
        "price": f'"{fill.price}"',
        "qty": f'"{fill.qty}"',
        "quoteQty": f'"{fill.quote_qty}"',
        "commission": fill.commission,
        "commissionAsset": f'"{fill.commission_asset}"',
        "time": str(fill.executed_ms),
        "isBuyer": "true" if fill.is_buyer else "false",
        "isMaker": "false",
    }
    for name, fragment in fill.overrides.items():
        if fragment is None:
            fields.pop(name, None)
        else:
            fields[name] = fragment
    return _render_object(fields)


def envelope(data: str) -> str:
    """BingX's documented success envelope around a raw `data` fragment."""
    return f'{{"code":0,"msg":"","debugMsg":"","data":{data}}}'


def fills_body(fills: Sequence[VenueFill]) -> str:
    """A successful answer carrying exactly `fills`, in the order given."""
    return envelope('{"fills":[' + ",".join(render_fill(fill) for fill in fills) + "]}")


def error_body(code: int | str, msg: str = "request refused") -> str:
    """A refusal as the probe saw one: a non-zero code, a message, a timestamp, no `data`.

    `code` is rendered as given, so a test can send `"100001"` as a string, or `false`.
    """
    return f'{{"code":{code},"msg":"{msg}","debugMsg":"","timestamp":{SERVER_TIMESTAMP}}}'


HTML_BODY: Final = "<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"


# --------------------------------------------------------------------------------------
# The venue
# --------------------------------------------------------------------------------------


class WireStream(httpx.AsyncByteStream):
    """A body handed to the client as raw bytes off the wire, still encoded.

    `httpx.Response(content=...)` decodes eagerly, which would raise a `Content-Encoding`
    failure inside the fake instead of in the client, where a real one arises.
    """

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._raw


@dataclass(frozen=True, slots=True)
class Reply:
    """One scripted answer: a status, a body and headers, or an exception to raise.

    `wire`, when given, is sent instead of `body` as raw, still-encoded bytes.
    """

    status: int = 200
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    error: BaseException | None = None
    wire: bytes | None = None

    def respond(self) -> httpx.Response:
        if self.error is not None:
            raise self.error
        if self.wire is not None:
            return httpx.Response(
                self.status, headers=dict(self.headers), stream=WireStream(self.wire)
            )
        return httpx.Response(self.status, headers=dict(self.headers), content=self.body)


def _is_lower_hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in "0123456789abcdef" for character in value)


class FakeBingX:
    """Query transaction details, answering from a script of fills.

    `replies`, when given, replaces the computed answers with a scripted sequence whose last
    entry repeats -- for statuses, refusals and bodies no honest venue would compute.
    Verification still runs first, so a scripted answer is only reached by a request that
    was correctly signed.
    """

    def __init__(
        self,
        fills: Sequence[VenueFill] = (),
        *,
        silent_cap: int | None = None,
        ignore_start_time: bool = False,
        replies: Sequence[Reply] = (),
        signing_key: str = SIGNING_SENTINEL,
        access_key: str = ACCESS_KEY_SENTINEL,
    ) -> None:
        self.fills = tuple(fills)
        self.silent_cap = silent_cap
        self.ignore_start_time = ignore_start_time
        self._replies = tuple(replies)
        self._signing_key = signing_key
        self._access_key = access_key
        #: Every request, in order.
        self.requests: list[httpx.Request] = []
        #: The requests that verified, in order.
        self.verified: list[httpx.Request] = []
        #: What was wrong with each request that did not verify.
        self.signature_failures: list[str] = []
        #: The fills each computed answer carried, in the order served.
        self.served: list[tuple[VenueFill, ...]] = []

    # -- routing -------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != BINGX_HOST:  # pragma: no cover - a bug in a test or the source
            message = f"the provider called an unscripted host: {request.url.host}"
            raise AssertionError(message)
        if request.url.path != DOCUMENTED_FILLS_PATH:  # pragma: no cover - same
            message = f"the provider called an undocumented path: {request.url.path}"
            raise AssertionError(message)
        failure = self.verification_failure(request)
        if failure is not None:
            self.signature_failures.append(failure)
            return httpx.Response(200, content=error_body(100001, "Signature verification failed"))
        self.verified.append(request)
        if self._replies:
            index = min(len(self.verified), len(self._replies)) - 1
            return self._replies[index].respond()
        page = self._page_for(request.url.params)
        self.served.append(tuple(page))
        return httpx.Response(200, content=fills_body(page))

    # -- verification --------------------------------------------------------------------

    def expected_signature(self, signed: str) -> str:
        """HMAC-SHA256 of `signed` under the fake's secret, lower-case hex, stdlib alone."""
        return hmac.new(
            self._signing_key.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def verification_failure(self, request: httpx.Request) -> str | None:
        """Why this request would be refused as unsigned, or `None` if it verifies.

        Over the query **as received**: the string before `&signature=` is what was signed,
        `signature` must be the last parameter and appear once, and the rest must carry a
        `timestamp` of digits. The key header must be the configured key.
        """
        if request.headers.get(KEY_HEADER) != self._access_key:
            return f"{KEY_HEADER} is missing or is not the configured key"
        query = request.url.query.decode("ascii")
        signed, marker, signature = query.rpartition(f"&{SIGNATURE_PARAMETER}=")
        if not marker:
            return "no signature parameter follows the signed query"
        keys = [pair.partition("=")[0] for pair in signed.split("&")]
        if SIGNATURE_PARAMETER in keys or "&" in signature:
            return "signature is not the last parameter, or appears twice"
        if "timestamp" not in keys:
            return "the signed query carries no timestamp"
        timestamp = request.url.params.get("timestamp", "")
        if not (timestamp.isascii() and timestamp.isdigit()):
            return "timestamp is not a millisecond count"
        if not _is_lower_hex(signature, 64):
            return "signature is not 64 lower-case hex characters"
        if not hmac.compare_digest(signature, self.expected_signature(signed)):
            return f"signature does not verify over the {len(signed)} bytes before it"
        return None

    # -- answers -------------------------------------------------------------------------

    def _page_for(self, params: httpx.QueryParams) -> list[VenueFill]:
        """Both bounds inclusive, ascending by time then id, at most `limit` fills."""
        start_text = params.get("startTime")
        end_text = params.get("endTime")
        start = None if start_text is None or self.ignore_start_time else int(start_text)
        end = None if end_text is None else int(end_text)
        limit = min(int(params.get("limit", str(DOCUMENTED_DEFAULT_LIMIT))), DOCUMENTED_MAX_LIMIT)
        if self.silent_cap is not None:
            limit = min(limit, self.silent_cap)
        symbol = params.get("symbol")
        matching = [
            fill
            for fill in self.fills
            if (start is None or start <= fill.executed_ms)
            and (end is None or fill.executed_ms <= end)
            and (symbol is None or fill.symbol == symbol)
        ]
        ordered = sorted(matching, key=lambda fill: (fill.executed_ms, fill.trade_id, fill.symbol))
        return ordered[:limit]

    # -- what a test reads ---------------------------------------------------------------

    def queries(self) -> list[str]:
        """The raw query of every request, as the bytes arrived, in order."""
        return [request.url.query.decode("ascii") for request in self.requests]

    def params(self) -> list[dict[str, str]]:
        """The query of every request, parsed, in order."""
        return [dict(request.url.params) for request in self.requests]

    def start_times(self) -> list[int]:
        return [int(params["startTime"]) for params in self.params()]


# --------------------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------------------

#: The two symbols a multi-symbol script alternates between.
TWO_SYMBOLS: Final = ("ETH-USDT", "BTC-USDT")


def spread_fills(
    count: int,
    *,
    first_ms: int = WINDOW_SINCE_MS + 60_000,
    step_ms: int = 60_000,
    first_id: int = 36_767_057,
    symbols: Sequence[str] = TWO_SYMBOLS,
) -> list[VenueFill]:
    """`count` fills, one every `step_ms` from `first_ms`, rotating through `symbols`.

    **Ids overlap across symbols**: fill `i` has id `first_id + i // len(symbols)`, so with
    two symbols each id is used once by each. Namespaced, they stay distinct; not, they
    collide on the page and the page is refused.
    """
    width = len(symbols)
    return [
        VenueFill(
            trade_id=first_id + index // width,
            executed_ms=first_ms + step_ms * index,
            symbol=symbols[index % width],
        )
        for index in range(count)
    ]


# --------------------------------------------------------------------------------------
# Building the provider under test
# --------------------------------------------------------------------------------------


def bingx_client(fake: FakeBingX, *, sleep: RecordingSleep | None = None) -> httpx.AsyncClient:
    """The production client factory over the fake, with every duration injected."""
    return retrying_client(httpx.MockTransport(fake.handler), sleep=sleep)


def bingx_provider(
    client: httpx.AsyncClient,
    *,
    clock: Callable[[], datetime] | None = None,
    credentials: Credentials | None = None,
) -> BingXProvider:
    """The provider under test, signing with the synthetic credentials at a fixed instant."""
    return BingXProvider(
        client,
        credentials if credentials is not None else synthetic_credentials(),
        clock=clock if clock is not None else FixedClock(),
    )


async def fetch_page(
    fake: FakeBingX,
    window: FillWindow = WINDOW,
    *,
    cursor: str | None = None,
    clock: Callable[[], datetime] | None = None,
    credentials: Credentials | None = None,
) -> FillPage:
    """One `fetch_fill_page` against the fake, on a client opened and closed around it."""
    async with bingx_client(fake) as client:
        provider = bingx_provider(client, clock=clock, credentials=credentials)
        return await provider.fetch_fill_page(window, cursor=cursor, symbol=None)


#: The most pages `walk` follows before calling the pagination a loop. Far above any walk a
#: test scripts, and small enough that a loop ends in milliseconds.
MAX_PAGES_WALKED: Final = 10


async def walk(fake: FakeBingX, window: FillWindow = WINDOW) -> list[FillPage]:
    """Page through `window` the way #15 does, on one provider, until `next_cursor` is `None`.

    Bounded: a walk that has not ended after `MAX_PAGES_WALKED` pages is an
    `AssertionError`, so a provider that loops fails a test instead of hanging it.
    """
    pages: list[FillPage] = []
    async with bingx_client(fake) as client:
        provider = bingx_provider(client)
        cursor: str | None = None
        for _ in range(MAX_PAGES_WALKED):
            page = await provider.fetch_fill_page(window, cursor=cursor, symbol=None)
            pages.append(page)
            if page.next_cursor is None:
                return pages
            cursor = page.next_cursor
    message = f"pagination did not end within {MAX_PAGES_WALKED} pages"
    raise AssertionError(message)


def ms(value: int) -> datetime:
    """An epoch-millisecond count as an aware instant, built without the code under test."""
    return datetime.fromtimestamp(value // 1000, tz=UTC).replace(microsecond=value % 1000 * 1000)


def window_ms(since_ms: int, until_ms: int) -> FillWindow:
    return FillWindow(since=ms(since_ms), until=ms(until_ms))
