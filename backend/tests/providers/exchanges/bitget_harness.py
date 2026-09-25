"""A fake Bitget venue behind one `httpx.MockTransport`, and the bodies it sends.

The same shape as `tests/providers/prices/harness.py`, for the same reasons, plus the one
thing a signed venue needs that a price vendor does not.

**Every signed request is verified here, with `hmac`, `hashlib` and `base64` directly.**
Never with `portfolio.providers.exchanges.signing`: a verifier that called the signer would
agree with it whatever it did. The pre-hash is rebuilt from the bytes the request actually
carried (`request.url.raw_path`, which is the path and the query exactly as sent), so a
provider that signs one query and sends another fails here even if both are sorted. A
request that does not verify is answered the way Bitget documents, `40009`, which the
provider maps to an auth error, so a signing bug fails every test that makes a request and
not only the tests about signing. `signature_failures` records what was wrong.

**The documented facts are written here as literals, not read off the source.** The paths
and the header names are what Bitget documents, and a fake that read `FILLS_PATH` from the
provider would route whatever path the provider chose. The host is the exception, read off
`BITGET_API_URL` as the price harness reads its hosts, and pinned once as a literal in
`test_bitget.py`.

**The bodies are hand-written strings, never `json.dumps` of a Python value.** A fill carries
amounts, and the whole point of the parser is that the venue's digits arrive intact. A body
built by serialising a `Decimal` or a `float` would have gone through the conversion the
parser exists to avoid, and the test's expectation would share it.

**The venue behaves the way the documentation says, and each undocumented behaviour is a
switch.** `startTime`/`endTime` are inclusive or exclusive (`Bounds`), the order within a
page is descending, ascending or shuffled (`Order`), and `ignore_cursor` is a venue that
answers the first page again whatever `idLessThan` says. What is documented is fixed:
`idLessThan` pages to older trade ids, and `limit` caps the page.

**The order ids are chosen so that paging by `orderId` goes wrong.** Every order id is
larger than every trade id, so a provider that sent an order id as `idLessThan` would be
answered the first page again: the loop the issue calls trap 1.

**Nothing here sleeps, and nothing reads a clock.** The provider's clock is injected
(`fixed_clock`), and the client is `tests/providers/harness.py`'s, whose sleep and jitter are
injected too.

No real credential appears anywhere. The three synthetic values are sentences, low in
entropy and obviously fake, and none is assigned to a name containing the venue's name,
which is the shape `.gitleaks.toml`'s `exchange-api-credential` rule refuses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import httpx
from pydantic import SecretStr

from portfolio.providers.exchanges.base import FillWindow
from portfolio.providers.exchanges.bitget import BITGET_API_URL, BitgetProvider
from portfolio.providers.exchanges.credentials import Credentials
from tests.providers.harness import RecordingSleep, retrying_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from portfolio.providers.exchanges.base import FillPage

# --------------------------------------------------------------------------------------
# What Bitget documents, written down rather than read off the provider
# --------------------------------------------------------------------------------------

BITGET_HOST: Final = httpx.URL(BITGET_API_URL).host

#: Get Fills and Get Symbol Info, from the Classic (v2) documentation read on 2026-09-25.
DOCUMENTED_FILLS_PATH: Final = "/api/v2/spot/trade/fills"
DOCUMENTED_SYMBOLS_PATH: Final = "/api/v2/spot/public/symbols"

#: The four access headers, spelt as the REST introduction spells them. `httpx.Headers`
#: matches case-insensitively, so the spelling asserted is the one on the wire only where a
#: test reads `request.headers.raw`.
KEY_HEADER: Final = "ACCESS-KEY"
SIGN_HEADER: Final = "ACCESS-SIGN"
TIMESTAMP_HEADER: Final = "ACCESS-TIMESTAMP"
PHRASE_HEADER: Final = "ACCESS-PASSPHRASE"
ACCESS_HEADERS: Final = (KEY_HEADER, SIGN_HEADER, TIMESTAMP_HEADER, PHRASE_HEADER)

#: The page limit Get Fills documents: "default 100, max 100".
DOCUMENTED_LIMIT: Final = 100

#: What the documented envelopes carry as `requestTime`. Arbitrary but fixed, so no body
#: depends on a clock.
REQUEST_TIME: Final = 1695865274510

# --------------------------------------------------------------------------------------
# Synthetic credentials
# --------------------------------------------------------------------------------------

#: The secret the golden vectors were computed under, with `openssl`, outside this code.
SIGNING_SENTINEL: Final = "dummy-secret-not-a-real-key"
ACCESS_KEY_SENTINEL: Final = "synthetic-access-key-for-tests-only"
PHRASE_SENTINEL: Final = "synthetic-passphrase-for-tests-only"


def synthetic_credentials(
    *,
    api_key: str = ACCESS_KEY_SENTINEL,
    api_secret: str = SIGNING_SENTINEL,
    passphrase: str | None = PHRASE_SENTINEL,
) -> Credentials:
    """`Credentials` holding the three sentinels, each wrapped as production wraps it."""
    return Credentials(
        api_key=SecretStr(api_key),
        api_secret=SecretStr(api_secret),
        passphrase=None if passphrase is None else SecretStr(passphrase),
    )


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------

#: The SDK vector's timestamp, `1684814440729`, as an instant. Converted by hand with
#: `date -u -d @1684814440.729 '+%Y-%m-%dT%H:%M:%S.%3NZ'` -> 2023-05-23T04:00:40.729Z.
GOLDEN_TIMESTAMP_MS: Final = 1684814440729
GOLDEN_NOW: Final = datetime(2023, 5, 23, 4, 0, 40, 729000, tzinfo=UTC)

#: The window most tests ask about: two days around the documented example fill, whose
#: `cTime` is 1695865232579 (2023-09-28T01:40:32.579Z). `date -u -d 2023-09-27T00:00:00Z +%s`
#: is 1695772800 and `date -u -d 2023-09-29T00:00:00Z +%s` is 1695945600.
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


# --------------------------------------------------------------------------------------
# Fills, rendered by hand
# --------------------------------------------------------------------------------------

#: Every order id is at least this, and every trade id the tests script is below it. A
#: provider that sent an order id as `idLessThan` would therefore be answered with the
#: first page again.
ORDER_ID_BASE: Final = 9_000_000_000


@dataclass(frozen=True, slots=True)
class VenueFill:
    """One fill as the fake venue holds it, rendered in the documented field order.

    The defaults are the documented example's values. `overrides` replaces a field with a
    raw JSON fragment -- `{"tradeId": '"012"'}` -- or removes it with `None`; a dotted key
    (`"feeDetail.totalFee"`) reaches into the fee object, and a key the venue does not send
    is appended. Fragments are raw text, so a test can send a JSON number where a string is
    documented, a `null`, or an escape the parser has to survive.

    `trade_id` stays an `int` because the fake pages over it; a malformed `tradeId` is an
    override, and the fake still pages by the `int`.
    """

    trade_id: int
    executed_ms: int
    order_id: int | None = None
    symbol: str = "BTCUSDT"
    side: str = "buy"
    price_avg: str = "13000"
    size: str = "0.0007"
    amount: str = "9.1"
    total_fee: str = "-0.0000007"
    fee_coin: str = "BTC"
    overrides: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def order(self) -> int:
        """The order id sent: three fills to an order, all above every trade id."""
        return self.order_id if self.order_id is not None else ORDER_ID_BASE + self.trade_id // 3


def _render_object(fields: Mapping[str, str]) -> str:
    return "{" + ",".join(f'"{name}":{fragment}' for name, fragment in fields.items()) + "}"


def _apply(fields: dict[str, str], name: str, fragment: str | None) -> None:
    if fragment is None:
        fields.pop(name, None)
    else:
        fields[name] = fragment


def render_fill(fill: VenueFill) -> str:
    """The fill object as Bitget's Get Fills example lays it out, as text."""
    fee: dict[str, str] = {
        "deduction": '"no"',
        "feeCoin": f'"{fill.fee_coin}"',
        "totalDeductionFee": '""',
        "totalFee": f'"{fill.total_fee}"',
    }
    top: dict[str, str] = {
        "userId": '"**********"',
        "symbol": f'"{fill.symbol}"',
        "orderId": f'"{fill.order}"',
        "tradeId": f'"{fill.trade_id}"',
        "orderType": '"market"',
        "side": f'"{fill.side}"',
        "priceAvg": f'"{fill.price_avg}"',
        "size": f'"{fill.size}"',
        "amount": f'"{fill.amount}"',
        "feeDetail": "",
        "tradeScope": '"taker"',
        "cTime": f'"{fill.executed_ms}"',
        "uTime": f'"{fill.executed_ms + 448}"',
    }
    for name, fragment in fill.overrides.items():
        if name.startswith("feeDetail."):
            _apply(fee, name.removeprefix("feeDetail."), fragment)
    top["feeDetail"] = _render_object(fee)
    for name, fragment in fill.overrides.items():
        if not name.startswith("feeDetail."):
            _apply(top, name, fragment)
    return _render_object(top)


def envelope(data: str, *, code: str = "00000", msg: str = "success") -> str:
    """Bitget's documented envelope around a raw `data` fragment."""
    return f'{{"code":"{code}","msg":"{msg}","requestTime":{REQUEST_TIME},"data":{data}}}'


def fills_body(fills: Sequence[VenueFill]) -> str:
    """A successful Get Fills answer carrying exactly `fills`, in the order given."""
    return envelope("[" + ",".join(render_fill(fill) for fill in fills) + "]")


def error_body(code: str, msg: str = "request refused") -> str:
    """A refusal as Bitget documents one: the code, a message, and a `null` data."""
    return f'{{"code":"{code}","msg":"{msg}","requestTime":{REQUEST_TIME},"data":null}}'


HTML_BODY: Final = "<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"


def symbol_entry(symbol: str, base: str, quote: str, *, status: str = "online") -> str:
    """One Get Symbol Info entry, with the documented neighbours of the two fields read."""
    return (
        "{"
        f'"symbol":"{symbol}","baseCoin":"{base}","quoteCoin":"{quote}",'
        '"minTradeAmount":"0","maxTradeAmount":"10000000000",'
        '"takerFeeRate":"0.002","makerFeeRate":"0.002",'
        '"pricePrecision":"2","quantityPrecision":"4","quotePrecision":"6",'
        f'"status":"{status}","minTradeUSDT":"5",'
        '"buyLimitPriceRatio":"0.05","sellLimitPriceRatio":"0.05",'
        '"areaSymbol":"no","orderQuantity":"200","openTime":"1532454360000","offTime":""'
        "}"
    )


def symbols_body(*entries: str) -> str:
    """A successful Get Symbol Info answer carrying exactly `entries`."""
    return envelope("[" + ",".join(entries) + "]")


#: The pairs the fake knows, as its own answer. `AB12CD` splits where no list of quote
#: coins would guess, so a base and quote that come back right came from the venue.
DEFAULT_SYMBOLS: Final[Mapping[str, tuple[str, str]]] = {
    "BTCUSDT": ("BTC", "USDT"),
    "KASUSDT": ("KAS", "USDT"),
    "AB12CD": ("AB1", "2CD"),
}


# --------------------------------------------------------------------------------------
# The venue
# --------------------------------------------------------------------------------------


class Bounds(StrEnum):
    """Whether `startTime` and `endTime` include their own millisecond. Undocumented."""

    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"
    IGNORED = "ignored"
    """Serve every fill whatever the window says: a venue answering outside the question."""


class Order(StrEnum):
    """The order of the fills within a page. Undocumented."""

    DESCENDING = "descending"
    ASCENDING = "ascending"
    SHUFFLED = "shuffled"


class WireStream(httpx.AsyncByteStream):
    """A body handed to the client as raw bytes off the wire, still encoded.

    `httpx.Response(content=...)` reads -- and so decodes -- its body in the constructor,
    which would raise a `Content-Encoding` failure inside the fake instead of where a real
    one arises: in the client, reading the body, above every transport. A stream that is not
    an `httpx.ByteStream` is left for the client to read, as a socket's would be.
    """

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._raw


@dataclass(frozen=True, slots=True)
class Reply:
    """One scripted answer: a status, a body and headers, or an exception to raise.

    `wire`, when given, is sent instead of `body` as raw, still-encoded bytes (`WireStream`),
    so a test can script a `Content-Encoding` the body does not honour.
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


def _shuffled(fills: list[VenueFill]) -> list[VenueFill]:
    """A fixed permutation that is neither ascending nor descending by trade id."""
    return sorted(fills, key=lambda fill: (fill.trade_id * 7919) % 10007)


class FakeBitget:
    """Get Fills and Get Symbol Info, answering from a script of fills.

    `fill_replies` and `symbol_replies`, when given, replace the computed answers with a
    scripted sequence whose last entry repeats -- for statuses, refusals and bodies no
    honest venue would compute. Verification still runs first, so a scripted answer is only
    reached by a request that was correctly signed.
    """

    def __init__(
        self,
        fills: Sequence[VenueFill] = (),
        *,
        bounds: Bounds = Bounds.INCLUSIVE,
        order: Order = Order.DESCENDING,
        ignore_cursor: bool = False,
        symbols: Mapping[str, tuple[str, str]] = DEFAULT_SYMBOLS,
        fill_replies: Sequence[Reply] = (),
        symbol_replies: Sequence[Reply] = (),
        signing_key: str = SIGNING_SENTINEL,
        access_key: str = ACCESS_KEY_SENTINEL,
        passphrase: str = PHRASE_SENTINEL,
    ) -> None:
        self.fills = tuple(fills)
        self.bounds = bounds
        self.order = order
        self.ignore_cursor = ignore_cursor
        self.symbols = dict(symbols)
        self._fill_replies = tuple(fill_replies)
        self._symbol_replies = tuple(symbol_replies)
        self._signing_key = signing_key
        self._access_key = access_key
        self._passphrase = passphrase
        #: Every request, in order, across both endpoints.
        self.requests: list[httpx.Request] = []
        self.fill_requests: list[httpx.Request] = []
        self.symbol_requests: list[httpx.Request] = []
        #: The fills requests that verified, in order.
        self.verified: list[httpx.Request] = []
        #: What was wrong with each fills request that did not verify.
        self.signature_failures: list[str] = []
        #: The trade ids each computed fills answer carried, in the order served.
        self.served_trade_ids: list[tuple[int, ...]] = []

    # -- routing -------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != BITGET_HOST:  # pragma: no cover - a bug in a test or the source
            message = f"the provider called an unscripted host: {request.url.host}"
            raise AssertionError(message)
        if request.url.path == DOCUMENTED_FILLS_PATH:
            self.fill_requests.append(request)
            return self._answer_fills(request)
        if request.url.path == DOCUMENTED_SYMBOLS_PATH:
            self.symbol_requests.append(request)
            return self._answer_symbols(request)
        message = f"the provider called an undocumented path: {request.url.path}"
        raise AssertionError(message)

    # -- verification --------------------------------------------------------------------

    def expected_signature(self, timestamp: str, method: str, raw_target: str) -> str:
        """Bitget's documented signature, computed with the standard library alone.

        `timestamp + METHOD + requestPath + "?" + queryString + body`, the body empty for a
        GET. `raw_target` is the path and the query exactly as sent, `?` included, so the
        verification is over the bytes on the wire and nothing reassembled.
        """
        prehash = f"{timestamp}{method.upper()}{raw_target}"
        digest = hmac.new(
            self._signing_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def verification_failure(self, request: httpx.Request) -> str | None:
        """Why this request would be refused as unsigned, or `None` if it verifies."""
        headers = request.headers
        missing = [name for name in ACCESS_HEADERS if name not in headers]
        if missing:
            return f"missing header(s): {missing}"
        if headers[KEY_HEADER] != self._access_key:
            return f"{KEY_HEADER} is not the configured key"
        if headers[PHRASE_HEADER] != self._passphrase:
            return f"{PHRASE_HEADER} is not the configured passphrase"
        timestamp = headers[TIMESTAMP_HEADER]
        if not (timestamp.isascii() and timestamp.isdigit()):
            return f"{TIMESTAMP_HEADER} is not a millisecond count"
        raw_target = request.url.raw_path.decode("ascii")
        expected = self.expected_signature(timestamp, request.method, raw_target)
        if headers[SIGN_HEADER] != expected:
            return f"{SIGN_HEADER} does not verify over {request.method} {raw_target}"
        return None

    # -- answers -------------------------------------------------------------------------

    def _answer_fills(self, request: httpx.Request) -> httpx.Response:
        failure = self.verification_failure(request)
        if failure is not None:
            self.signature_failures.append(failure)
            return httpx.Response(400, content=error_body("40009", "sign signature error"))
        self.verified.append(request)
        if self._fill_replies:
            index = min(len(self.verified), len(self._fill_replies)) - 1
            return self._fill_replies[index].respond()
        page = self._page_for(request.url.params)
        self.served_trade_ids.append(tuple(fill.trade_id for fill in page))
        return httpx.Response(200, content=fills_body(page))

    def _page_for(self, params: httpx.QueryParams) -> list[VenueFill]:
        start = int(params["startTime"])
        end = int(params["endTime"])
        limit = int(params.get("limit", str(DOCUMENTED_LIMIT)))
        before = params.get("idLessThan")

        def in_window(moment: int) -> bool:
            if self.bounds is Bounds.INCLUSIVE:
                return start <= moment <= end
            if self.bounds is Bounds.EXCLUSIVE:
                return start < moment < end
            return True

        def below_cursor(trade_id: int) -> bool:
            return self.ignore_cursor or before is None or trade_id < int(before)

        matching = [
            fill
            for fill in self.fills
            if in_window(fill.executed_ms) and below_cursor(fill.trade_id)
        ]
        # `idLessThan` pages to older data, so a page is the highest ids below the cursor.
        newest = sorted(matching, key=lambda fill: fill.trade_id, reverse=True)[:limit]
        if self.order is Order.ASCENDING:
            return sorted(newest, key=lambda fill: fill.trade_id)
        if self.order is Order.SHUFFLED:
            return _shuffled(newest)
        return newest

    def _answer_symbols(self, request: httpx.Request) -> httpx.Response:
        if self._symbol_replies:
            index = min(len(self.symbol_requests), len(self._symbol_replies)) - 1
            return self._symbol_replies[index].respond()
        asked = request.url.params.get("symbol", "")
        known = self.symbols.get(asked)
        if known is None:
            return httpx.Response(400, content=error_body("40102", "symbol does not exist"))
        base, quote = known
        return httpx.Response(200, content=symbols_body(symbol_entry(asked, base, quote)))

    # -- what a test reads ---------------------------------------------------------------

    def fill_queries(self) -> list[dict[str, str]]:
        """The query of every fills request, parsed, in order."""
        return [dict(request.url.params) for request in self.fill_requests]

    def access_headers_on_symbol_requests(self) -> list[str]:
        """Every access header any symbol request carried. Public endpoint: none."""
        return [
            name
            for request in self.symbol_requests
            for name in request.headers
            if name.lower().startswith("access-")
        ]


# --------------------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------------------


def spread_fills(
    count: int,
    *,
    first_id: int = 1_000_003,
    step: int = 3,
    symbols: Sequence[str] = ("BTCUSDT",),
) -> list[VenueFill]:
    """`count` fills inside `WINDOW`, one a minute, trade ids `first_id, first_id + step, ...`.

    Gaps between ids, because a venue's trade ids are a sequence shared with every other
    account and never contiguous for one. The symbols rotate through `symbols`, so a page
    of several symbols is one argument away.
    """
    return [
        VenueFill(
            trade_id=first_id + step * index,
            executed_ms=WINDOW_SINCE_MS + 60_000 * (index + 1),
            symbol=symbols[index % len(symbols)],
        )
        for index in range(count)
    ]


# --------------------------------------------------------------------------------------
# Building the provider under test
# --------------------------------------------------------------------------------------


def bitget_client(fake: FakeBitget, *, sleep: RecordingSleep | None = None) -> httpx.AsyncClient:
    """The production client factory over the fake, with every duration injected."""
    return retrying_client(httpx.MockTransport(fake.handler), sleep=sleep)


def bitget_provider(
    client: httpx.AsyncClient,
    *,
    clock: Callable[[], datetime] | None = None,
    credentials: Credentials | None = None,
) -> BitgetProvider:
    """The provider under test, signing with the synthetic credentials at a fixed instant."""
    return BitgetProvider(
        client,
        credentials if credentials is not None else synthetic_credentials(),
        clock=clock if clock is not None else FixedClock(),
    )


async def fetch_page(
    fake: FakeBitget,
    window: FillWindow = WINDOW,
    *,
    cursor: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FillPage:
    """One `fetch_fill_page` against the fake, on a client opened and closed around it."""
    async with bitget_client(fake) as client:
        provider = bitget_provider(client, clock=clock)
        return await provider.fetch_fill_page(window, cursor=cursor, symbol=None)


#: The most pages `walk` follows before calling the pagination a loop. Far above any walk a
#: test scripts, and small enough that a loop ends in milliseconds.
MAX_PAGES_WALKED: Final = 10


async def walk(fake: FakeBitget, window: FillWindow = WINDOW) -> list[FillPage]:
    """Page through `window` the way #15 will, on one provider, until `next_cursor` is `None`.

    Bounded: a walk that has not ended after `MAX_PAGES_WALKED` pages is an
    `AssertionError`, so a provider that loops fails a test instead of hanging it.
    """
    pages: list[FillPage] = []
    async with bitget_client(fake) as client:
        provider = bitget_provider(client)
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
