"""Criteria 2 and 4 to 7 and 9 of #14: the BingX spot fills provider, against a fake venue.

Every expected value here comes from outside the code under test: the documented sample,
copied verbatim from BingX's own docs source; two signatures computed with `openssl`, the
command beside each; timestamps converted with `date -u`; cursors worked out by hand from
the script and written as literals. The fake venue (`bingx_harness.FakeBingX`) verifies
every request with the standard library, so a signing mistake fails every test that makes a
request, not only the ones named after it. It behaves as the owner's live probe found the
venue behaving -- both bounds inclusive, ascending by time, no symbol needed, every refusal
on HTTP 200 -- and not as the documentation says, where the two differ.

The provider is driven through `fetch_fill_page` wherever the behaviour is observable there,
because that is what #15 calls. The pure functions are called directly for the golden
vectors, for `from_binary_float`'s own table, and for the property tests at the end, where
thousands of generated bodies make a round trip through HTTP pointless.
"""

from __future__ import annotations

import gzip
import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.providers.base import decode_json
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.exchanges.base import (
    MAX_RAW_PAYLOAD_DEPTH,
    CursorKind,
    ExchangeCapabilities,
    FillPage,
    FillWindow,
    RateLimit,
    clamp_to_retention,
)
from portfolio.providers.exchanges.bingx import (
    API_KEY_HEADER,
    BINGX_API_URL,
    BINGX_CAPABILITIES,
    BINGX_ERROR_MAP,
    FILLS_PATH,
    PAGE_LIMIT,
    build_fills_query,
    from_binary_float,
    parse_fill,
    parse_fills_page,
    unwrap_envelope,
)
from portfolio.providers.exchanges.errors import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeInsufficientScopeError,
    ExchangeInvalidRequestError,
    ExchangeRateLimitedError,
    ExchangeRetentionWindowError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
)
from portfolio.providers.exchanges.signing import hmac_sha256_hex
from portfolio.providers.http import request_target
from portfolio.services.exchange_sync_plan import HISTORY_GENESIS
from portfolio.services.exchanges import history_truncated
from tests.providers.exchanges.bingx_harness import (
    ACCESS_KEY_SENTINEL,
    DOCUMENTED_FILLS_PATH,
    GOLDEN_NOW,
    GOLDEN_TIMESTAMP_MS,
    HTML_BODY,
    SIGNING_SENTINEL,
    WINDOW,
    WINDOW_SINCE_MS,
    WINDOW_UNTIL_MS,
    FakeBingX,
    FixedClock,
    Reply,
    TickingClock,
    VenueFill,
    bingx_client,
    bingx_provider,
    envelope,
    error_body,
    fetch_page,
    fills_body,
    render_fill,
    spread_fills,
    walk,
    window_ms,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: The seven classes criterion 9 promises, and nothing else. `ExchangeError` itself is the
#: marker base and is never raised.
SEVEN_CLASSES: Final = frozenset(
    {
        ExchangeAuthError,
        ExchangeInsufficientScopeError,
        ExchangeRateLimitedError,
        ExchangeUnavailableError,
        ExchangeInvalidRequestError,
        ExchangeRetentionWindowError,
        ExchangeSchemaError,
    }
)

#: Built with `chr`, so the source stays ASCII: RUF001 rightly calls a literal fullwidth
#: digit ambiguous with the ASCII one.
FULLWIDTH_DIGITS: Final = chr(0xFF11) + chr(0xFF12)


def exception_chain(error: BaseException) -> Iterator[BaseException]:
    """Every exception reachable from `error` by `__cause__` or `__context__`, `error` first."""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        link = pending.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        yield link
        pending.extend(
            nested for nested in (link.__cause__, link.__context__) if nested is not None
        )


def exact(value: Decimal, text: str) -> bool:
    """Equal in value **and** in digits and exponent: `0.0007` is not `0.00070`."""
    return value == Decimal(text) and value.as_tuple() == Decimal(text).as_tuple()


def names_field(error: BaseException, name: str) -> bool:
    """Whether `error`'s message names `name` as a word: `id` must not match `valid`."""
    return re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", str(error)) is not None


async def refused(
    fake: FakeBingX, window: FillWindow = WINDOW, *, cursor: str | None = None
) -> ExchangeError:
    """The exchange error one fetch raises. Anything that is not an `ExchangeError` escapes."""
    with pytest.raises(ExchangeError) as caught:
        await fetch_page(fake, window, cursor=cursor)
    return caught.value


#: A millisecond inside `WINDOW` the one-fill venues below execute at: 01:00 on the first day.
INSIDE_MS: Final = WINDOW_SINCE_MS + 3_600_000


def one_fill_fake(**overrides: str | None) -> FakeBingX:
    """A venue whose only answer is one fill inside `WINDOW`, with `overrides` applied."""
    fill = VenueFill(trade_id=36_767_057, executed_ms=INSIDE_MS, overrides=overrides)
    return FakeBingX(replies=[Reply(body=fills_body([fill]))])


def scripted(*fills: VenueFill) -> FakeBingX:
    """A venue whose only answer is exactly `fills`, in the order given."""
    return FakeBingX(replies=[Reply(body=fills_body(list(fills)))])


# --------------------------------------------------------------------------------------
# The documented constants
# --------------------------------------------------------------------------------------


def test_the_venue_constants_are_the_documented_ones() -> None:
    """Pinned as literals, once. The fake reads the host off `BINGX_API_URL`.

    The path and the header are what the V3 and V1 docs both give (read 2026-09-26), and the
    page size is the smaller of the page's two statements, as spec 017 decides.
    """
    assert BINGX_API_URL == "https://open-api.bingx.com"
    assert FILLS_PATH == "/openApi/spot/v1/trade/myTrades"
    assert API_KEY_HEADER == "X-BX-APIKEY"
    assert PAGE_LIMIT == 500


def test_the_capabilities_are_the_specs() -> None:
    """Spec 017's declaration, field by field, from a literal written by hand.

    365 days is a declared bound, not a measured one: the documented 7 days was disproved by
    the probe reading fills older than that. 30-day windows are headroom below the 365-day
    spans the probe saw accepted. 5 a second per UID is the documented myTrades budget.
    """
    assert (
        ExchangeCapabilities(
            exchange_key=ExchangeKey.BINGX,
            retention=timedelta(days=365),
            max_query_window=timedelta(days=30),
            page_size=500,
            cursor_kind=CursorKind.TIME,
            rate_limit=RateLimit(max_requests=5, per_ms=1000),
            requires_symbol=False,
        )
        == BINGX_CAPABILITIES
    )


# --------------------------------------------------------------------------------------
# Criterion 2: the signature, against vectors computed outside this code
# --------------------------------------------------------------------------------------

#: V3's signature page, run verbatim: its "sorted signing string" and its `SECRET_KEY`.
DOCUMENTED_RECIPE_INPUT: Final = "recvWindow=0&symbol=BTC-USDT&timestamp=1696751141337"
DOCUMENTED_RECIPE_SIGNATURE: Final = (
    "fe041f159118c90ac13eab4d32f9e2d75b80ca6fe17ca8acd290aba864753ce2"
)
r"""Computed outside this code, with OpenSSL 3.5.4, which printed exactly the literal above:

    printf '%s' 'recvWindow=0&symbol=BTC-USDT&timestamp=1696751141337' \
      | openssl dgst -sha256 -hmac 'SECRET_KEY' -hex

V3 gives the input and this command but no output; V1's own example does not reproduce, so
it is not used.
"""

#: The query the provider builds for `VECTOR_WINDOW` at `GOLDEN_NOW`, without its signature.
PROVIDER_VECTOR_QUERY: Final = (
    "endTime=1695899999999&limit=500&startTime=1695800000000&timestamp=1684814440729"
)
PROVIDER_VECTOR_SIGNATURE: Final = (
    "c181531feee1cc42ce6ac986aafca6c9b590a7b746809b820df601860b2f9d6e"
)
r"""Computed outside this code, with OpenSSL 3.5.4, which printed exactly the literal above:

    printf '%s' \
      'endTime=1695899999999&limit=500&startTime=1695800000000&timestamp=1684814440729' \
      | openssl dgst -sha256 -hmac 'SECRET_KEY' -hex
"""

#: `since` is 1695800000000 ms (`date -u -d @1695800000` -> 2023-09-27T07:33:20Z), sent as
#: `startTime` itself. `until` is 1695900000000 ms (`date -u -d @1695900000` ->
#: 2023-09-28T11:20:00Z), sent as `endTime = until - 1`, because both bounds are inclusive.
VECTOR_WINDOW: Final = FillWindow(
    since=datetime(2023, 9, 27, 7, 33, 20, tzinfo=UTC),
    until=datetime(2023, 9, 28, 11, 20, tzinfo=UTC),
)


def test_the_golden_instant_is_the_vector_timestamp() -> None:
    """The premise of the vector tests: `GOLDEN_NOW` is 1684814440729 ms after the epoch."""
    assert (GOLDEN_NOW - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1) == (
        GOLDEN_TIMESTAMP_MS
    )


def test_the_signature_matches_the_documented_recipe() -> None:
    """The shared signer against the `openssl` literal, and the fake's verifier against it too.

    The second assertion is the verifier's own control: every other test trusts the fake to
    say whether a request verifies, so the fake must agree with a value computed outside
    both the provider and the fake.
    """
    signature = hmac_sha256_hex(SecretStr(SIGNING_SENTINEL), DOCUMENTED_RECIPE_INPUT)

    assert signature == DOCUMENTED_RECIPE_SIGNATURE
    assert FakeBingX().expected_signature(DOCUMENTED_RECIPE_INPUT) == DOCUMENTED_RECIPE_SIGNATURE


async def test_the_signature_of_the_request_the_provider_sends() -> None:
    """A fixed clock and window: the request on the wire, byte for byte, and its signature."""
    fake = FakeBingX()

    page = await fetch_page(fake, VECTOR_WINDOW)

    assert page.fills == ()
    (request,) = fake.requests
    assert request.method == "GET"
    assert request.url.raw_path.decode("ascii") == (
        f"{DOCUMENTED_FILLS_PATH}?{PROVIDER_VECTOR_QUERY}&signature={PROVIDER_VECTOR_SIGNATURE}"
    )
    assert fake.signature_failures == []
    assert fake.expected_signature(PROVIDER_VECTOR_QUERY) == PROVIDER_VECTOR_SIGNATURE
    assert (
        build_fills_query(VECTOR_WINDOW, cursor=None, timestamp_ms=GOLDEN_TIMESTAMP_MS)
        == PROVIDER_VECTOR_QUERY
    )


async def test_the_query_is_sent_exactly_as_signed_and_the_signature_is_last() -> None:
    """A two-page walk: each query sorted, all digits, signed exactly as sent, `signature` last.

    And the key travels in `X-BX-APIKEY` and nowhere else: no other header is named like a
    credential, and no header carries the secret or the signature.
    """
    fake = FakeBingX(spread_fills(700))

    await walk(fake)

    queries = fake.queries()
    assert len(queries) == 2
    assert fake.verified == fake.requests
    assert fake.signature_failures == []
    for request, query, start in zip(
        fake.requests, queries, ["1695772800000", "1695802800000"], strict=True
    ):
        signed, marker, signature = query.rpartition("&signature=")
        assert marker, "the signature is not the last parameter"
        keys = [pair.partition("=")[0] for pair in signed.split("&")]
        assert keys == ["endTime", "limit", "startTime", "timestamp"]
        assert keys == sorted(keys)
        assert all(
            pair.partition("=")[2].isascii() and pair.partition("=")[2].isdigit()
            for pair in signed.split("&")
        )
        assert signature == fake.expected_signature(signed)
        cursor = None if start == "1695772800000" else start
        assert signed == build_fills_query(WINDOW, cursor=cursor, timestamp_ms=GOLDEN_TIMESTAMP_MS)
        assert request.headers["X-BX-APIKEY"] == ACCESS_KEY_SENTINEL
        credential_like = [
            name
            for name in request.headers
            if any(word in name.lower() for word in ("key", "sign", "secret", "pass", "token"))
        ]
        assert credential_like == ["x-bx-apikey"]
        for name, value in request.headers.items():
            assert SIGNING_SENTINEL not in value, f"the secret travels in {name}"
            assert signature not in value, f"the signature travels in {name}"


async def test_the_fake_venue_refuses_a_request_signed_with_another_secret() -> None:
    """The control on the verifier: it can say no, so its yes above means something."""
    fake = FakeBingX(spread_fills(3), signing_key="another-secret-entirely-not-real")

    error = await refused(fake)

    assert type(error) is ExchangeAuthError
    assert error.venue_code == "100001"
    assert error.status == 200
    assert len(fake.signature_failures) == len(fake.requests) == 1
    assert "does not verify" in fake.signature_failures[0]


def test_the_fake_venue_refuses_a_signature_that_is_not_last() -> None:
    """The control on the "last" rule, which the provider can only fail by sending it wrong."""
    fake = FakeBingX()
    signature = fake.expected_signature(PROVIDER_VECTOR_QUERY)
    base = "https://open-api.bingx.com/openApi/spot/v1/trade/myTrades"
    headers = {"X-BX-APIKEY": ACCESS_KEY_SENTINEL}
    first = httpx.Request(
        "GET", f"{base}?signature={signature}&{PROVIDER_VECTOR_QUERY}", headers=headers
    )
    moved = PROVIDER_VECTOR_QUERY.replace("&timestamp=", f"&signature={signature}&timestamp=")
    middle = httpx.Request("GET", f"{base}?{moved}", headers=headers)
    right = httpx.Request(
        "GET", f"{base}?{PROVIDER_VECTOR_QUERY}&signature={signature}", headers=headers
    )
    unkeyed = httpx.Request("GET", f"{base}?{PROVIDER_VECTOR_QUERY}&signature={signature}")

    assert fake.verification_failure(first) is not None
    assert fake.verification_failure(middle) is not None
    assert fake.verification_failure(unkeyed) is not None
    assert fake.verification_failure(right) is None


async def test_each_request_is_labelled_for_the_log() -> None:
    """The label is the only thing about a request's target the transport logs."""
    fake = FakeBingX(spread_fills(1))

    await fetch_page(fake)

    (request,) = fake.requests
    assert request.extensions.get("endpoint") == "exchange_fills"
    assert request_target(request) == "https://open-api.bingx.com/exchange_fills"


# --------------------------------------------------------------------------------------
# Criterion 4: no symbol is needed, so none is sent and none is discovered
# --------------------------------------------------------------------------------------


async def test_no_request_names_a_symbol_and_discovery_is_empty() -> None:
    """The probe: without `symbol` the venue answers every symbol. So nothing is enumerated.

    The positive companion to "no `symbol` in any query" is that the walk returned fills of
    both symbols -- the absence is not an empty walk -- and that each query does carry its
    other four parameters.
    """
    fake = FakeBingX(spread_fills(700))

    async with bingx_client(fake) as client:
        provider = bingx_provider(client)
        assert BINGX_CAPABILITIES.requires_symbol is False
        assert provider.capabilities == BINGX_CAPABILITIES
        assert list(await provider.candidate_symbols()) == []
        assert fake.requests == [], "discovery made a request"

        with pytest.raises(ValueError, match="symbol") as caught:
            await provider.fetch_fill_page(WINDOW, cursor=None, symbol="ETH-USDT")
        assert not isinstance(caught.value, ExchangeError)
        assert fake.requests == [], "a caller's symbol cost a signed request"

    pages = await walk(fake)

    assert len(fake.requests) == 2
    for params in fake.params():
        assert "symbol" not in params
        assert {"endTime", "limit", "startTime", "timestamp", "signature"} <= set(params)
    assert {fill.symbol for page in pages for fill in page.fills} == {"ETH-USDT", "BTC-USDT"}


# --------------------------------------------------------------------------------------
# Criterion 5: pagination terminates, by construction
# --------------------------------------------------------------------------------------
#
# `spread_fills(n)` puts fill `i` at `since + 60000 * (i + 1)` ms, alternating ETH-USDT and
# BTC-USDT with overlapping ids. The venue serves ascending, 500 at a time, both bounds
# inclusive. Worked out by hand, for 1,200 fills:
#
# * page 1 asks `startTime = since` = 1695772800000 and gets fills 0..499; the newest is fill
#   499 at 1695772800000 + 60000 * 500 = 1695802800000;
# * page 2 asks from there **inclusive**, so it gets fills 499..998; the newest is fill 998
#   at 1695772800000 + 60000 * 999 = 1695832740000;
# * page 3 asks from there and gets fills 998..1199: 202 fills, short, so the window ends.

SINCE_TEXT: Final = "1695772800000"
FIRST_PAGE_CURSOR: Final = "1695802800000"
SECOND_PAGE_CURSOR: Final = "1695832740000"
END_TIME_TEXT: Final = "1695945599999"


async def test_a_multi_page_walk_returns_every_fill_and_stops() -> None:
    script = spread_fills(1200)
    fake = FakeBingX(script)

    pages = await walk(fake)

    assert len(fake.requests) == 3
    assert [len(page.fills) for page in pages] == [500, 500, 202]
    assert [page.next_cursor for page in pages] == [FIRST_PAGE_CURSOR, SECOND_PAGE_CURSOR, None]
    assert [params["startTime"] for params in fake.params()] == [
        SINCE_TEXT,
        FIRST_PAGE_CURSOR,
        SECOND_PAGE_CURSOR,
    ]
    assert {params["endTime"] for params in fake.params()} == {END_TIME_TEXT}
    ids = {fill.external_trade_id for page in pages for fill in page.fills}
    assert ids == {f"{fill.symbol}:{fill.trade_id}" for fill in script}
    assert len(ids) == 1200
    # The two fills at each page's newest millisecond are read twice, by design.
    assert sum(len(page.fills) for page in pages) == 1202


async def test_a_venue_enforcing_five_hundred_is_paged_past() -> None:
    """A venue capping at 500 whatever `limit` says: the docs' other statement of the limit.

    Asking for 1000 against such a venue would read its 500-fill page as short, and lose
    the rest of the window. Asking for 500 pages past it.
    """
    fake = FakeBingX(spread_fills(1200), silent_cap=500)

    pages = await walk(fake)

    assert [len(page.fills) for page in pages] == [500, 500, 202]
    assert {params["limit"] for params in fake.params()} == {"500"}


async def test_a_silent_cap_below_five_hundred_is_the_documented_risk() -> None:
    """Spec 017, Risks: a venue silently capping below 500 would look complete. Pinned.

    Nothing in the docs or the probe suggests such a cap, and `limit=5` was honoured
    exactly, so the provider calls a page of fewer than 500 fills the end of the window.
    This test fixes that decision where a reader can see it: a change to "page until an
    empty answer" must change this test deliberately.
    """
    fake = FakeBingX(spread_fills(150), silent_cap=100)

    page = await fetch_page(fake)

    assert len(page.fills) == 100
    assert page.next_cursor is None


async def test_fills_sharing_the_boundary_millisecond_are_all_read() -> None:
    """Three fills at the millisecond where a full page ends: the next page starts **at** it.

    498 fills at distinct minutes, then three at M = 1695772800000 + 60000 * 499 =
    1695802740000, then ten more. The venue orders by time then id, so page 1 holds 498 +
    two of the three; page 2 starts at M and holds the three again and the ten after.
    `startTime = M + 1` would lose the third fill for good.
    """
    boundary_ms = 1695802740000
    boundary = [
        VenueFill(trade_id=41_000_001, executed_ms=boundary_ms, symbol="ETH-USDT"),
        VenueFill(trade_id=41_000_001, executed_ms=boundary_ms, symbol="BTC-USDT"),
        VenueFill(trade_id=41_000_002, executed_ms=boundary_ms, symbol="ETH-USDT"),
    ]
    after = spread_fills(10, first_ms=boundary_ms + 60_000, first_id=42_000_000)
    fake = FakeBingX([*spread_fills(498), *boundary, *after])

    pages = await walk(fake)

    assert [len(page.fills) for page in pages] == [500, 13]
    assert pages[0].next_cursor == str(boundary_ms)
    assert [params["startTime"] for params in fake.params()] == [SINCE_TEXT, str(boundary_ms)]
    first = {fill.external_trade_id for fill in pages[0].fills}
    second = {fill.external_trade_id for fill in pages[1].fills}
    assert "ETH-USDT:41000002" not in first, "the premise: the third did not fit page 1"
    assert {"ETH-USDT:41000001", "BTC-USDT:41000001", "ETH-USDT:41000002"} <= second
    assert len(first | second) == 511


@pytest.mark.parametrize(
    ("cursor", "stuck_ms"),
    [(None, WINDOW_SINCE_MS), ("1695776400000", 1695776400000)],
    ids=["first page, all at since", "later page, all at the cursor"],
)
async def test_a_full_page_stuck_in_one_millisecond_raises(
    cursor: str | None, stuck_ms: int
) -> None:
    """500 fills at this request's `startTime`: a time cursor cannot page past them. Loud."""
    fake = FakeBingX(spread_fills(500, first_ms=stuck_ms, step_ms=0))

    error = await refused(fake, cursor=cursor)

    assert type(error) is ExchangeSchemaError
    assert len(fake.requests) == 1


async def test_a_millisecond_holding_more_than_a_page_stops_the_walk_on_its_second_request() -> (
    None
):
    """600 fills at one minute past `since`: page 1 is fine and points at it; page 2 is stuck."""
    stuck_ms = WINDOW_SINCE_MS + 60_000
    fake = FakeBingX(spread_fills(600, first_ms=stuck_ms, step_ms=0))

    with pytest.raises(ExchangeSchemaError):
        await walk(fake)

    assert [params["startTime"] for params in fake.params()] == [SINCE_TEXT, str(stuck_ms)]


async def test_a_full_page_one_millisecond_past_start_time_advances() -> None:
    """The companion: 500 fills one millisecond after `since` is progress, not a stall."""
    fake = FakeBingX(spread_fills(500, first_ms=WINDOW_SINCE_MS + 1, step_ms=0))

    page = await fetch_page(fake)

    assert len(page.fills) == 500
    assert page.next_cursor == "1695772800001"


async def test_a_venue_that_ignores_start_time_raises_instead_of_looping() -> None:
    """The oldest page served again whatever `startTime` says: refused on the second request."""
    fake = FakeBingX(spread_fills(1200), ignore_start_time=True)

    with pytest.raises(ExchangeSchemaError):
        await walk(fake)

    assert len(fake.requests) == 2
    assert fake.served[0] == fake.served[1], "the premise: the venue repeated itself"
    assert [params["startTime"] for params in fake.params()] == [SINCE_TEXT, FIRST_PAGE_CURSOR]


#: A cursor inside `WINDOW`: 01:00 on the first day (`date -u -d @1695776400`).
CURSOR_MS: Final = 1695776400000


@pytest.mark.parametrize("full", [False, True], ids=["short page", "full page"])
async def test_a_fill_before_the_cursor_is_refused(full: bool) -> None:
    """A fill one millisecond before the `startTime` it was asked from: the bound was ignored.

    The short page has no next cursor for a stall guard to look at, and the full page's
    newest fill is well past the cursor, so only this check can see either. Both fills are
    inside the window, so the window check cannot see them either.
    """
    early = VenueFill(trade_id=1, executed_ms=CURSOR_MS - 1)
    rest = spread_fills(499 if full else 1, first_ms=CURSOR_MS + 60_000, first_id=100)
    fake = scripted(early, *rest)

    error = await refused(fake, cursor=str(CURSOR_MS))

    assert type(error) is ExchangeSchemaError


async def test_a_fill_at_the_cursor_is_accepted() -> None:
    """The companion: `startTime` is inclusive, so a fill exactly at the cursor belongs."""
    fake = scripted(
        VenueFill(trade_id=1, executed_ms=CURSOR_MS),
        VenueFill(trade_id=2, executed_ms=CURSOR_MS + 60_000),
    )

    page = await fetch_page(fake, cursor=str(CURSOR_MS))

    assert [fill.external_trade_id for fill in page.fills] == ["BTC-USDT:1", "BTC-USDT:2"]
    assert page.next_cursor is None
    (params,) = fake.params()
    assert params["startTime"] == str(CURSOR_MS)


def _shuffled(fills: list[VenueFill]) -> list[VenueFill]:
    """A fixed permutation that is neither ascending nor descending."""
    return sorted(fills, key=lambda fill: (fill.executed_ms * 7919) % 10007)


@pytest.mark.parametrize("order", ["descending", "shuffled"])
async def test_the_cursor_is_the_newest_millisecond_whatever_order_the_page_is_in(
    order: str,
) -> None:
    """The newest `time` on the page, not the last fill served and not the oldest.

    The probe saw ascending order; the cursor must not depend on it. The newest of
    `spread_fills(500)` is fill 499 at 1695802800000.
    """
    fills = spread_fills(500)
    served = list(reversed(fills)) if order == "descending" else _shuffled(fills)
    assert served[-1].executed_ms != 1695802800000, "the premise: the last is not the newest"

    page = await fetch_page(scripted(*served))

    assert page.next_cursor == FIRST_PAGE_CURSOR


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, None), (499, None), (500, FIRST_PAGE_CURSOR)],
    ids=["empty", "499 fills", "500 fills"],
)
async def test_a_short_page_ends_the_window(count: int, expected: str | None) -> None:
    """Only a full page -- `limit` = 500 fills -- can have more behind it."""
    fake = FakeBingX(spread_fills(count))

    page = await fetch_page(fake)

    assert len(page.fills) == count
    assert page.next_cursor == expected


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("01", id="a leading zero"),
        pytest.param("-1", id="negative"),
        pytest.param("1.5", id="a decimal point"),
        pytest.param("", id="empty"),
        pytest.param(" 1695776400000", id="a leading space"),
        pytest.param("1695776400000 ", id="a trailing space"),
        pytest.param("1695776400000\n", id="a trailing newline"),
        pytest.param("0x1", id="hex"),
        pytest.param(FULLWIDTH_DIGITS, id="fullwidth digits"),
        pytest.param("\ud800", id="a lone surrogate"),
        pytest.param("1" * 16, id="sixteen digits"),
        pytest.param("0", id="zero, before since"),
        pytest.param("1695772799999", id="one millisecond before since"),
        pytest.param("1695945600000", id="at until"),
        pytest.param("1695945600001", id="after until"),
    ],
)
async def test_a_malformed_cursor_from_the_caller_costs_no_request(cursor: str) -> None:
    """A caller's mistake costs no signed request, and is not dressed up as a venue's."""
    fake = FakeBingX()

    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the class is the contract here
        await fetch_page(fake, cursor=cursor)

    assert not isinstance(caught.value, ExchangeError)
    assert fake.requests == []


@pytest.mark.parametrize("cursor", ["1695772800000", "1695945599999"], ids=["since", "until - 1"])
async def test_a_cursor_at_either_end_of_the_window_is_sent(cursor: str) -> None:
    """The companion: both ends of `[since, until - 1 ms]` are cursors, sent as `startTime`."""
    fake = FakeBingX()

    page = await fetch_page(fake, cursor=cursor)

    assert page.fills == ()
    (params,) = fake.params()
    assert params["startTime"] == cursor
    assert params["endTime"] == END_TIME_TEXT


#: A fill on each side of each edge of `WINDOW`.
EDGE_FILLS: Final = (
    VenueFill(trade_id=1, executed_ms=WINDOW_SINCE_MS - 1),
    VenueFill(trade_id=2, executed_ms=WINDOW_SINCE_MS),
    VenueFill(trade_id=3, executed_ms=WINDOW_UNTIL_MS - 1),
    VenueFill(trade_id=4, executed_ms=WINDOW_UNTIL_MS),
)


async def test_the_bounds_are_since_and_until_minus_one() -> None:
    """Against an inclusive venue, `[since, until - 1 ms]` is exactly `[since, until)`.

    The fills at `since` and at `until - 1 ms` are kept; the ones at `since - 1 ms` and at
    `until` are never asked for. Asking for `endTime = until` would be served the fill at
    `until`, which belongs to the next window, and the page would be refused.
    """
    fake = FakeBingX(EDGE_FILLS)

    page = await fetch_page(fake)

    (params,) = fake.params()
    assert params["startTime"] == SINCE_TEXT
    assert params["endTime"] == END_TIME_TEXT
    assert [fill.trade_id for fill in fake.served[0]] == [2, 3], "the premise: inclusive"
    assert [fill.external_trade_id for fill in page.fills] == ["BTC-USDT:2", "BTC-USDT:3"]


@pytest.mark.parametrize(
    "executed_ms",
    [WINDOW_SINCE_MS - 1, WINDOW_UNTIL_MS, WINDOW_UNTIL_MS + 1],
    ids=["since - 1 ms", "until", "until + 1 ms"],
)
async def test_a_fill_outside_the_window_is_refused(executed_ms: int) -> None:
    """A venue answering outside the question: refused, never dropped quietly."""
    fake = scripted(
        VenueFill(trade_id=1, executed_ms=INSIDE_MS),
        VenueFill(trade_id=2, executed_ms=executed_ms),
    )

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError


async def test_a_window_longer_than_thirty_days_costs_no_request() -> None:
    """The caller's mistake, refused before signing; exactly thirty days is sent."""
    since = datetime(2023, 9, 1, tzinfo=UTC)
    fake = FakeBingX()

    async with bingx_client(fake) as client:
        provider = bingx_provider(client)
        with pytest.raises(ValueError, match="window"):
            await provider.fetch_fill_page(
                FillWindow(since=since, until=since + timedelta(days=30, milliseconds=1)),
                cursor=None,
                symbol=None,
            )
        assert fake.requests == []

        await provider.fetch_fill_page(
            FillWindow(since=since, until=since + timedelta(days=30)), cursor=None, symbol=None
        )
    assert len(fake.requests) == 1


async def test_more_fills_than_the_page_size_are_refused() -> None:
    """501 fills in answer to `limit=500`: the venue answered a different question."""
    error = await refused(scripted(*spread_fills(501)))

    assert type(error) is ExchangeSchemaError


# --------------------------------------------------------------------------------------
# Criterion 6: every failure lands in its class
# --------------------------------------------------------------------------------------

#: Spec 017's table, written by hand from the spec: every code on any status. `100441`
#: (V3 spot, "account abnormal", KYC required) and `100401` (V1's AUTHENTICATION_FAIL) were
#: added to the auth row during implementation, with the tech lead.
DOCUMENTED_CODES: Final[dict[int, type[ExchangeError]]] = {
    # The owner has to fix the key.
    100001: ExchangeAuthError,
    100412: ExchangeAuthError,
    100413: ExchangeAuthError,
    100419: ExchangeAuthError,
    100414: ExchangeAuthError,
    100441: ExchangeAuthError,
    100401: ExchangeAuthError,
    # The key lacks Read.
    100004: ExchangeInsufficientScopeError,
    # A timestamp the venue refused: never auth, because the transport replays.
    100421: ExchangeUnavailableError,
    # The in-band throttle, under its old and its new number.
    100410: ExchangeRateLimitedError,
    109429: ExchangeRateLimitedError,
    # Busy.
    100500: ExchangeUnavailableError,
    100503: ExchangeUnavailableError,
    # A backend that is down: V3 moved a sibling endpoint from an empty success to this
    # code on 2026-09-05 (spec 017, R3). Mapped defensively.
    109500: ExchangeUnavailableError,
    # A request we built.
    100400: ExchangeInvalidRequestError,
    100204: ExchangeInvalidRequestError,
    100404: ExchangeInvalidRequestError,
    100490: ExchangeInvalidRequestError,
}

#: A code BingX does not use and the map does not hold.
UNMAPPED_CODE: Final = 100999


def test_the_error_map_is_exactly_the_specs_table() -> None:
    """Every code keyed `(None, code)`, plus `(418, None)`; nothing more, nothing less."""
    expected: dict[tuple[int | None, str | None], type[ExchangeError]] = {
        (None, str(code)): error_class for code, error_class in DOCUMENTED_CODES.items()
    }
    expected[(418, None)] = ExchangeRateLimitedError

    assert dict(BINGX_ERROR_MAP) == expected
    assert isinstance(BINGX_ERROR_MAP, MappingProxyType)
    assert (None, str(UNMAPPED_CODE)) not in BINGX_ERROR_MAP


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize(("code", "expected"), sorted(DOCUMENTED_CODES.items()))
async def test_each_in_band_code_maps_to_its_class(
    code: int, expected: type[ExchangeError], status: int
) -> None:
    """On a 200, as the probe saw every refusal arrive, and on a 400: the code decides."""
    fake = FakeBingX(replies=[Reply(status=status, body=error_body(code))])

    error = await refused(fake)

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == str(code)
    assert len(fake.requests) == 1, "an in-band refusal is not retried"


STATUS_BODIES: Final = {"html": HTML_BODY, "json": error_body(UNMAPPED_CODE)}


@pytest.mark.parametrize("body", sorted(STATUS_BODIES))
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ExchangeAuthError),
        (403, ExchangeAuthError),
        (418, ExchangeRateLimitedError),
        (429, ExchangeRateLimitedError),
        (500, ExchangeUnavailableError),
        (503, ExchangeUnavailableError),
        (504, ExchangeUnavailableError),
    ],
)
async def test_each_status_maps_to_its_class(
    status: int, expected: type[ExchangeError], body: str
) -> None:
    """The exact class, never a superclass, and never chained from an HTTP status error.

    A 418 is BingX's "IP banned after 429": rate limited, never a refused request a person
    has to fix.
    """
    fake = FakeBingX(
        replies=[Reply(status=status, body=STATUS_BODIES[body], headers={"Retry-After": "7"})]
    )

    error = await refused(fake)

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == (str(UNMAPPED_CODE) if body == "json" else None)
    if isinstance(error, ExchangeRateLimitedError):
        assert error.retry_after_ms == 7000
    assert not any(isinstance(link, httpx.HTTPStatusError) for link in exception_chain(error))


async def test_a_rate_limit_without_retry_after_carries_none() -> None:
    """`None` is "the venue said nothing", which is not `0`, "immediately"."""
    error = await refused(FakeBingX(replies=[Reply(status=429, body=error_body(100410))]))

    assert type(error) is ExchangeRateLimitedError
    assert error.retry_after_ms is None


@pytest.mark.parametrize("status", [200, 400, 401])
async def test_a_timestamp_error_is_never_auth(status: int) -> None:
    """`100421` on any status: a replayed request can arrive stale, and a skewed clock is not
    a bad key. #15 must not mark a working key `auth_failed` for it."""
    error = await refused(FakeBingX(replies=[Reply(status=status, body=error_body(100421))]))

    assert type(error) is ExchangeUnavailableError
    assert not isinstance(error, ExchangeAuthError)
    assert error.venue_code == "100421"


async def test_an_unmapped_code_on_a_200_is_a_schema_error() -> None:
    """A code nobody mapped, on a 200, is an answer not understood: loud, never a success.

    The companion: the same code on a 400 is left to the status, an invalid request.
    """
    on_200 = await refused(FakeBingX(replies=[Reply(body=error_body(UNMAPPED_CODE))]))
    on_400 = await refused(FakeBingX(replies=[Reply(status=400, body=error_body(UNMAPPED_CODE))]))

    assert type(on_200) is ExchangeSchemaError
    assert on_200.venue_code == str(UNMAPPED_CODE)
    assert type(on_400) is ExchangeInvalidRequestError


async def test_a_code_with_two_documented_meanings_is_left_unmapped() -> None:
    """`100403` means different things in different BingX pages, so nothing guesses which.

    On a 200 it is a schema error, loud; on a 403 the status decides, which is auth.
    """
    assert (None, "100403") not in BINGX_ERROR_MAP

    on_200 = await refused(FakeBingX(replies=[Reply(body=error_body(100403))]))
    on_403 = await refused(FakeBingX(replies=[Reply(status=403, body=error_body(100403))]))

    assert type(on_200) is ExchangeSchemaError
    assert type(on_403) is ExchangeAuthError


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('{"code":0,"msg":"","debugMsg":""}', id="data absent"),
        pytest.param('{"code":0,"msg":"","debugMsg":"","data":null}', id="data null"),
        pytest.param('{"code":0,"msg":"","debugMsg":"","data":[]}', id="data an array"),
        pytest.param('{"code":0,"msg":"","debugMsg":"","data":"fills"}', id="data a string"),
        pytest.param('{"code":0,"msg":"","debugMsg":"","data":{}}', id="fills absent"),
        pytest.param('{"code":0,"msg":"","data":{"fills":null}}', id="fills null"),
        pytest.param('{"code":0,"msg":"","data":{"fills":{}}}', id="fills an object"),
        pytest.param('{"code":0,"msg":"","data":{"fills":"[]"}}', id="fills a string"),
        pytest.param('{"code":0,"msg":"","data":{"fills":[1]}}', id="a fill not an object"),
        pytest.param('{"code":0,"msg":"","data":{"fills":[[]]}}', id="a fill an array"),
        pytest.param('{"code":false,"msg":"","data":{"fills":[]}}', id="code false"),
        pytest.param('{"code":"0","msg":"","data":{"fills":[]}}', id="code the string 0"),
        pytest.param('{"code":0.0,"msg":"","data":{"fills":[]}}', id="code 0.0"),
        pytest.param('{"code":null,"msg":"","data":{"fills":[]}}', id="code null"),
        pytest.param('{"msg":"","data":{"fills":[]}}', id="code absent"),
    ],
)
async def test_code_zero_without_fills_is_a_schema_error(body: str) -> None:
    """Success is HTTP 200, the integer `0`, and a `data` object holding a `fills` array.

    A missing list is never read as "no fills": that reading would end a window as empty,
    the sync would move its checkpoint past it, and the fills would never be read.
    """
    fake = FakeBingX(replies=[Reply(body=body)])

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError
    assert error.__cause__ is None
    assert len(fake.requests) == 1, "the positive companion: the page was fetched"


async def test_an_empty_window_is_code_zero_with_an_empty_list() -> None:
    """The probe's empty answer: code 0, `data.fills: []`. An empty page, not an error."""
    fake = FakeBingX(replies=[Reply(body=envelope('{"fills":[]}'))])

    page = await fetch_page(fake)

    assert page.fills == ()
    assert page.next_cursor is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json at all", id="not JSON"),
        pytest.param(HTML_BODY, id="HTML"),
        pytest.param("[]", id="an array"),
        pytest.param("0", id="a number"),
        pytest.param('"0"', id="a string"),
        pytest.param("", id="empty"),
        pytest.param('{"code":0,"data":{"fills":[', id="truncated"),
    ],
)
async def test_a_success_status_with_a_body_that_is_not_the_envelope_is_a_schema_error(
    body: str,
) -> None:
    """HTTP 200 and a body that is not a JSON object: schema, with nothing chained."""
    error = await refused(FakeBingX(replies=[Reply(body=body)]))

    assert type(error) is ExchangeSchemaError
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_an_unexpected_extra_field_is_ignored_and_kept_in_the_payload() -> None:
    """Fields nobody documented, at every level: the page still parses, and the fill's own
    extra field is kept in `raw_payload`, because that is the venue's record as sent."""
    fill = VenueFill(
        trade_id=36_767_057, executed_ms=INSIDE_MS, overrides={"newField": '{"a":[1,"b"]}'}
    )
    body = (
        '{"code":0,"msg":"","debugMsg":"","retryable":false,"timestamp":1695865274510,'
        '"data":{"fills":[' + render_fill(fill) + '],"total":1}}'
    )

    page = await fetch_page(FakeBingX(replies=[Reply(body=body)]))

    (parsed,) = page.fills
    raw = decode_json(parsed.raw_payload)
    assert isinstance(raw, dict)
    assert raw["newField"] == {"a": [1, "b"]}
    for envelope_field in ("code", "msg", "retryable", "timestamp", "data", "fills", "total"):
        assert envelope_field not in raw


async def test_a_transport_failure_is_unavailable_and_carries_no_url() -> None:
    cause = httpx.ConnectError("the fake venue refused the connection")
    fake = FakeBingX(replies=[Reply(error=cause)])

    error = await refused(fake)

    assert type(error) is ExchangeUnavailableError
    assert error.status is None
    assert error.__cause__ is cause
    assert not any(isinstance(link, httpx.HTTPStatusError) for link in exception_chain(error))
    signatures = [query.rpartition("&signature=")[2] for query in fake.queries()]
    assert all(signatures), "the positive companion: the requests were signed and sent"
    assert len(fake.requests) == 3, "and retried before failing"
    for link in exception_chain(error):
        rendered = f"{link}|{link!r}|{link.args!r}"
        for fragment in (
            "signature",
            "startTime",
            "timestamp=",
            "open-api.bingx.com",
            "myTrades",
            *signatures,
        ):
            assert fragment not in rendered


async def test_a_local_protocol_error_is_refused_and_carries_nothing() -> None:
    """h11 quotes the whole illegal header value, which here would be the key.

    Reachable only through a bug, since the constructor refuses such a key; a mock transport
    raising it is the only way here, and the sentinel stands for the key it quotes.
    """
    sentinel = "LOCAL-PROTOCOL-SENTINEL-7319"
    fake = FakeBingX(
        replies=[Reply(error=httpx.LocalProtocolError(f"Illegal header value b'{sentinel}'"))]
    )

    error = await refused(fake)

    assert type(error) in SEVEN_CLASSES
    for link in exception_chain(error):
        assert sentinel not in f"{link}{link!r}{link.args}"
    assert fake.requests, "the positive companion: the request was attempted"


async def test_the_transport_replays_the_same_signed_request() -> None:
    """A 429 then a 200: the retry resends the request as signed. The decision, pinned.

    The clock moves a second on every read, so a provider that re-signed per attempt would
    send a different timestamp and signature, and this would say so.
    """
    fake = FakeBingX(
        replies=[Reply(status=429, body=error_body(100410)), Reply(body=fills_body([]))]
    )

    page = await fetch_page(fake, clock=TickingClock())

    assert page.fills == ()
    first, second = fake.queries()
    assert first == second
    assert fake.signature_failures == []
    assert len(fake.verified) == 2


async def test_a_replay_refused_as_stale_is_unavailable() -> None:
    """Past the venue's 5-second window a replay is refused with `100421`: the next run signs
    a fresh one, so it is unavailable, never auth."""
    fake = FakeBingX(replies=[Reply(status=503, body=HTML_BODY), Reply(body=error_body(100421))])

    error = await refused(fake)

    assert type(error) is ExchangeUnavailableError
    assert error.venue_code == "100421"
    assert len(fake.requests) == 2


UNDECODABLE: Final = b"this is not a compressed body"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("status", [200, 500])
async def test_a_body_that_does_not_decompress_is_unavailable_and_carries_nothing(
    status: int, encoding: str
) -> None:
    """`httpx.DecodingError` is raised above the transport and is not a `TransportError`."""
    fake = FakeBingX(
        replies=[Reply(status=status, headers={"Content-Encoding": encoding}, wire=UNDECODABLE)]
    )

    error = await refused(fake)

    assert type(error) is ExchangeUnavailableError
    assert error.__cause__ is None
    assert error.__context__ is None
    assert fake.requests, "the positive companion: the call was made"


async def test_a_body_that_does_decompress_is_read() -> None:
    body = gzip.compress(fills_body(spread_fills(2)).encode("ascii"))
    fake = FakeBingX(replies=[Reply(headers={"Content-Encoding": "gzip"}, wire=body)])

    page = await fetch_page(fake)

    assert len(page.fills) == 2


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param(
            Reply(
                body=fills_body(spread_fills(2)),
                headers={"X-RateLimit-Requests-Remain": "1" * 5000},
            ),
            None,
            id="a success with a 5000-digit X-RateLimit-Requests-Remain",
        ),
        pytest.param(
            Reply(status=429, body=error_body(100410), headers={"Retry-After": "1" * 5000}),
            ExchangeRateLimitedError,
            id="a 429 with a 5000-digit Retry-After",
        ),
        pytest.param(
            Reply(status=503, body=HTML_BODY, headers={"X-RateLimit-Expire": "9" * 5000}),
            ExchangeUnavailableError,
            id="a 503 with a 5000-digit X-RateLimit-Expire",
        ),
    ],
)
async def test_a_hostile_header_on_a_bingx_answer_ends_in_one_of_the_seven(
    reply: Reply, expected: type[ExchangeError] | None
) -> None:
    """A header the transport reads on every response never escapes as a bare `ValueError`."""
    fake = FakeBingX(replies=[reply])

    if expected is None:
        page = await fetch_page(fake)
        assert len(page.fills) == 2
        return
    error = await refused(fake)
    assert type(error) is expected


# --------------------------------------------------------------------------------------
# Criterion 7: fills normalize to exact `Decimal` values
# --------------------------------------------------------------------------------------

#: The myTrades response sample, verbatim: the `responseBody` of Spot > Trade > "Query
#: transaction details" in the V3 docs bundle (the site last updated 2026-09-19), which
#: V1's `077_get_trade_fill_details.json` repeats character for character. Split only to
#: fit the line length; the concatenation is the documented text, checked against both.
DOCUMENTED_SAMPLE_BODY: Final = (
    '{"code":0,"msg":"","debugMsg":"","data":{"fills":[{"symbol":"BTC-USDT","id":36767057,'
    '"orderId":1745362930595004400,"price":"46820.155","qty":"0.1430254",'
    '"quoteQty":"6696.471396937","commission":-0.000046483255,"commissionAsset":"BTC",'
    '"time":1704961925000,"isBuyer":true,"isMaker":false},{"symbol":"BTC-USDT",'
    '"id":36767058,"orderId":1745362930595004400,"price":"46820.155","qty":"0.0003844",'
    '"quoteQty":"17.997667582000002","commission":-1.2493e-7,"commissionAsset":"BTC",'
    '"time":1704961925000,"isBuyer":true,"isMaker":false}]}}'
)

#: The first fill inside it, duplicated rather than sliced out, so the round-trip assertion
#: compares against text the code under test never touched.
DOCUMENTED_SAMPLE_FILL: Final = (
    '{"symbol":"BTC-USDT","id":36767057,"orderId":1745362930595004400,"price":"46820.155",'
    '"qty":"0.1430254","quoteQty":"6696.471396937","commission":-0.000046483255,'
    '"commissionAsset":"BTC","time":1704961925000,"isBuyer":true,"isMaker":false}'
)

#: The day of the sample: `date -u -d 2024-01-11T00:00:00Z +%s` is 1704931200, and the
#: sample's `time`, 1704961925000, is 2024-01-11T08:32:05Z (`date -u -d @1704961925`).
SAMPLE_WINDOW: Final = window_ms(1704931200000, 1705017600000)


async def test_the_documented_sample_fill_normalizes_exactly() -> None:
    """Every value by hand from the sample.

    `0.1430254 x 46820.155 = 6696.471396937` exactly, so the first `quoteQty` is clean and
    kept as sent. The commission is 0.0325% of the quantity in BTC, reported negative, so the
    fee is positive. The second fill carries the docs' own float noise, `17.997667582000002`
    (`0.0003844 x 46820.155 = 17.997667582`), and a fee of `-1.2493e-7`.
    """
    assert DOCUMENTED_SAMPLE_FILL in DOCUMENTED_SAMPLE_BODY
    fake = FakeBingX(replies=[Reply(body=DOCUMENTED_SAMPLE_BODY)])

    page = await fetch_page(fake, SAMPLE_WINDOW)

    first, second = page.fills
    assert first.external_trade_id == "BTC-USDT:36767057"
    assert first.external_order_id == "1745362930595004400"
    assert first.symbol == "BTC-USDT"
    assert first.base_asset == "BTC"
    assert first.quote_asset == "USDT"
    assert first.side is FillSide.BUY
    assert exact(first.quantity, "0.1430254")
    assert exact(first.price, "46820.155")
    assert exact(first.quote_quantity, "6696.471396937")
    assert first.quote_quantity_derived is False
    assert exact(first.fee_amount, "0.000046483255")
    assert first.fee_asset == "BTC"
    assert first.executed_at == datetime(2024, 1, 11, 8, 32, 5, tzinfo=UTC)
    raw = decode_json(first.raw_payload)
    assert raw == decode_json(DOCUMENTED_SAMPLE_FILL)
    assert isinstance(raw, dict)
    assert raw["isMaker"] is False, "isMaker is kept in the payload only"
    for envelope_field in ("code", "msg", "debugMsg", "data", "fills"):
        assert envelope_field not in raw

    assert second.external_trade_id == "BTC-USDT:36767058"
    assert exact(second.quantity, "0.0003844")
    assert exact(second.quote_quantity, "17.997667582")
    assert exact(second.fee_amount, "1.2493E-7")
    assert second.fee_asset == "BTC"
    assert page.next_cursor is None


#: `from_binary_float`'s table. Each rounding worked out by hand: count fifteen significant
#: digits, look at the sixteenth, and round half to even.
FLOAT_TABLE: Final = [
    pytest.param("17.997667582000002", "17.997667582", id="the docs' own float noise"),
    pytest.param("-0.00005820000000000001", "-0.0000582", id="a captured fee, sign kept"),
    pytest.param("-1.2493e-7", "-1.2493E-7", id="the docs' second fee, unchanged"),
    pytest.param("6696.471396937", "6696.471396937", id="a clean value, unchanged"),
    pytest.param("-0.000046483255", "-0.000046483255", id="a clean fee, unchanged"),
    pytest.param("1.234567890123447", "1.23456789012345", id="sixteenth digit 7: up"),
    pytest.param("1.234567890123443", "1.23456789012344", id="sixteenth digit 3: down"),
    pytest.param("1.234567890123445", "1.23456789012344", id="a tie after an even digit"),
    pytest.param("1.234567890123455", "1.23456789012346", id="a tie after an odd digit"),
    pytest.param("123456789012345", "123456789012345", id="fifteen integer digits"),
    pytest.param(
        "1234567890123456789",
        "1234567890123460000",
        id="nineteen integer digits, in plain notation after rounding",
    ),
    pytest.param("1.50000000000000000", "1.50000000000000000", id="zeros only past the 15th"),
    pytest.param("0", "0", id="zero"),
    pytest.param(
        "-1.2345678901234567e-10",
        "-1.23456789012346E-10",
        id="seventeen digits ten places down, rounded and still 24 places",
    ),
]


@pytest.mark.parametrize(("given", "expected"), FLOAT_TABLE)
def test_from_binary_float_rounds_to_fifteen_significant_digits_half_even(
    given: str, expected: str
) -> None:
    """The rule on its own: 15 significant digits, half to even, in `Decimal` arithmetic.

    Exact in digits and exponent too, so a clean value is returned as it came, and a
    rounded one without the zeros rounding would leave.
    """
    result = from_binary_float(Decimal(given))

    assert exact(result, expected)


def test_from_binary_float_ignores_the_callers_decimal_context() -> None:
    """A thread whose context is 3 digits rounding down still gets the stated rule."""
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        noise = from_binary_float(Decimal("17.997667582000002"))
        rounded = from_binary_float(Decimal("1.234567890123447"))

    assert exact(noise, "17.997667582")
    assert exact(rounded, "1.23456789012345")


@pytest.mark.parametrize(
    ("field", "fragment", "expected"),
    [
        pytest.param("quoteQty", '"17.997667582000002"', "17.997667582", id="quoteQty noise"),
        pytest.param("quoteQty", '"1.234567890123445"', "1.23456789012344", id="quoteQty tie"),
        pytest.param("quoteQty", '"1.234567890123447"', "1.23456789012345", id="quoteQty up"),
        pytest.param("quoteQty", '"6696.471396937"', "6696.471396937", id="quoteQty clean"),
        pytest.param("commission", "-0.00005820000000000001", "0.0000582", id="commission noise"),
        pytest.param("commission", "-1.2493e-7", "1.2493E-7", id="commission exponent"),
        pytest.param(
            "commission", "-0.0001234567890123455", "0.000123456789012346", id="commission tie"
        ),
        pytest.param(
            "commission", "-0.0001234567890123443", "0.000123456789012344", id="commission down"
        ),
        pytest.param(
            "commission", '"-0.00005820000000000001"', "0.0000582", id="commission as a string"
        ),
    ],
)
async def test_float_artefacts_are_rounded_to_fifteen_significant_digits(
    field: str, fragment: str, expected: str
) -> None:
    """Through the provider, on the two float-encoded fields. The fee arrives negated.

    Without the rounding, `-0.00005820000000000001` has twenty places, `NormalizedFill`
    refuses anything past eighteen, and every page holding such a fee fails on every run.
    """
    page = await fetch_page(one_fill_fake(**{field: fragment}))

    (fill,) = page.fills
    read = fill.quote_quantity if field == "quoteQty" else fill.fee_amount
    assert exact(read, expected)
    assert fill.quote_quantity_derived is False


@pytest.mark.parametrize(
    ("field", "fragment"),
    [
        pytest.param("commission", "-1.2345678901234567e-10", id="commission, rounded"),
        pytest.param("quoteQty", '"1.2345678901234567e-10"', id="quoteQty, rounded"),
        pytest.param("commission", "-1.2345e-20", id="commission, already short"),
        pytest.param("quoteQty", '"1.2345e-20"', id="quoteQty, already short"),
    ],
)
async def test_a_value_still_finer_than_the_fill_scale_after_rounding_is_refused(
    field: str, fragment: str
) -> None:
    """Rounding removes float noise; it never makes a value fit the column.

    `1.2345678901234567e-10` has seventeen significant digits, so rounding **does** change
    it: to `1.23456789012346E-10` (the sixteenth digit is 6, so the fifteenth, 5, rounds up;
    `from_binary_float`'s table pins exactly that). That is still twenty-four places, past
    `FILL_SCALE`'s eighteen, so it is refused rather than rounded further. The short cases are
    five digits twenty-four places down, which rounding leaves alone.
    """
    error = await refused(one_fill_fake(**{field: fragment}))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "quote_quantity" if field == "quoteQty" else "fee_amount")


#: Nineteen significant digits: a quantity no double could carry.
NINETEEN_DIGITS: Final = "1234567890.123456789"
NINETEEN_PLACES: Final = "0.0000000000000000001"
EIGHTEEN_PLACES: Final = "0.000000000000000001"


@pytest.mark.parametrize("field", ["qty", "price"])
async def test_price_and_qty_are_never_rounded(field: str) -> None:
    """Exact strings the venue formats: nineteen significant digits survive; nineteen places,
    one past `FILL_SCALE`, are refused rather than rounded; eighteen are kept exactly."""
    kept = await fetch_page(one_fill_fake(**{field: f'"{NINETEEN_DIGITS}"'}))
    finest = await fetch_page(one_fill_fake(**{field: f'"{EIGHTEEN_PLACES}"'}))
    error = await refused(one_fill_fake(**{field: f'"{NINETEEN_PLACES}"'}))

    (fill,) = kept.fills
    assert exact(fill.quantity if field == "qty" else fill.price, NINETEEN_DIGITS)
    (fine,) = finest.fills
    assert exact(fine.quantity if field == "qty" else fine.price, EIGHTEEN_PLACES)
    assert type(error) is ExchangeSchemaError


@pytest.mark.parametrize("fragment", [None, "null", '""'], ids=["absent", "null", "empty string"])
async def test_a_missing_quote_quantity_is_derived_and_flagged(fragment: str | None) -> None:
    """`0.1430254 x 46820.155 = 6696.471396937` exactly, written at eighteen places."""
    page = await fetch_page(one_fill_fake(quoteQty=fragment))

    (fill,) = page.fills
    assert fill.quote_quantity_derived is True
    assert exact(fill.quote_quantity, "6696.471396937000000000")


async def test_a_positive_commission_is_refused() -> None:
    """The docs and the probe report a fee paid as negative. A positive one is not guessed at:
    read as a rebate, it would record every fee as income, silently. The companion is the
    zero-commission test below."""
    error = await refused(one_fill_fake(commission="0.000046483255"))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "commission")
    assert "0.000046483255" not in f"{error}{error!r}{error.args}"


@pytest.mark.parametrize(
    ("commission", "asset", "expected_asset"),
    [
        pytest.param("0", None, None, id="zero, no asset"),
        pytest.param("0", '""', None, id="zero, empty asset"),
        pytest.param("0", "null", None, id="zero, null asset"),
        pytest.param("-0", '""', None, id="negative zero"),
        pytest.param("0", '"BTC"', "BTC", id="zero, asset kept"),
    ],
)
async def test_a_zero_commission_needs_no_asset_and_is_never_a_negative_zero(
    commission: str, asset: str | None, expected_asset: str | None
) -> None:
    page = await fetch_page(one_fill_fake(commission=commission, commissionAsset=asset))

    (fill,) = page.fills
    assert fill.fee_amount == 0
    assert not fill.fee_amount.is_signed()
    assert fill.fee_asset == expected_asset


@pytest.mark.parametrize("asset", [None, "null", '""'], ids=["absent", "null", "empty"])
async def test_a_non_zero_commission_needs_its_asset(asset: str | None) -> None:
    error = await refused(one_fill_fake(commissionAsset=asset))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "commissionAsset")


async def test_the_trade_id_is_namespaced_and_overlapping_ids_stay_distinct() -> None:
    """`ETH-USDT:7` and `BTC-USDT:7` on one page are two fills, not one and a duplicate."""
    fake = scripted(
        VenueFill(trade_id=7, executed_ms=INSIDE_MS, symbol="ETH-USDT"),
        VenueFill(trade_id=7, executed_ms=INSIDE_MS, symbol="BTC-USDT"),
    )

    page = await fetch_page(fake)

    assert [fill.external_trade_id for fill in page.fills] == ["ETH-USDT:7", "BTC-USDT:7"]
    assert [(fill.base_asset, fill.quote_asset) for fill in page.fills] == [
        ("ETH", "USDT"),
        ("BTC", "USDT"),
    ]


async def test_the_same_id_twice_in_one_symbol_is_still_refused() -> None:
    """The companion: namespacing does not hide a real duplicate."""
    fake = scripted(
        VenueFill(trade_id=7, executed_ms=INSIDE_MS, symbol="ETH-USDT"),
        VenueFill(trade_id=7, executed_ms=INSIDE_MS + 1, symbol="ETH-USDT"),
    )

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('"36767057"', id="a string"),
        pytest.param("0", id="zero"),
        pytest.param("-1", id="negative"),
        pytest.param("9223372036854775808", id="two to the sixty-third"),
        pytest.param("36767057.0", id="a float with a point"),
        pytest.param("3.6767057e7", id="a float with an exponent"),
        pytest.param("null", id="null"),
        pytest.param("true", id="a boolean"),
        pytest.param(None, id="absent"),
    ],
)
async def test_a_malformed_trade_id_is_a_schema_error(fragment: str | None) -> None:
    error = await refused(one_fill_fake(id=fragment))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "id")


@pytest.mark.parametrize("trade_id", ["1", "9223372036854775807"])
async def test_the_trade_id_range_ends_at_the_signed_64_bit_maximum(trade_id: str) -> None:
    """The companion: the smallest and the largest ids the rule admits, kept exactly."""
    page = await fetch_page(one_fill_fake(id=trade_id))

    assert [fill.external_trade_id for fill in page.fills] == [f"BTC-USDT:{trade_id}"]


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        pytest.param(None, None, id="absent"),
        pytest.param("null", None, id="null"),
        pytest.param("1", "1", id="one"),
        pytest.param("9223372036854775807", "9223372036854775807", id="the 64-bit maximum"),
    ],
)
async def test_the_order_id_is_an_integer_rendered_or_none(
    fragment: str | None, expected: str | None
) -> None:
    page = await fetch_page(one_fill_fake(orderId=fragment))

    (fill,) = page.fills
    assert fill.external_order_id == expected


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('"1745362930595004400"', id="a string"),
        pytest.param("0", id="zero"),
        pytest.param("-1", id="negative"),
        pytest.param("9223372036854775808", id="two to the sixty-third"),
        pytest.param("1.5", id="a float"),
        pytest.param("true", id="a boolean"),
        pytest.param("{}", id="an object"),
    ],
)
async def test_a_malformed_order_id_is_a_schema_error(fragment: str) -> None:
    error = await refused(one_fill_fake(orderId=fragment))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "orderId")


#: Built with `chr`, so the source stays ASCII: `M`, a capital O with a stroke, `TH`.
O_STROKE: Final = chr(0xD8)


@pytest.mark.parametrize(
    ("symbol", "base", "quote"),
    [
        pytest.param("ETH-USDT", "ETH", "USDT", id="ETH-USDT"),
        pytest.param("AB12-CD3", "AB12", "CD3", id="digits on both sides"),
        pytest.param("1INCH-USDT", "1INCH", "USDT", id="a base starting with a digit"),
        # The reviewer's live examples (spec 017, R1), read off BingX's public symbol list on
        # 2026-09-27: a renamed pair, an underscore, a dollar sign, dots, parentheses, and a
        # letter outside ASCII. Each must parse, or a page holding it fails on every run.
        pytest.param("STRK-OLD-USDT", "STRK-OLD", "USDT", id="a hyphen in the base"),
        pytest.param("H_OLD-USDT", "H_OLD", "USDT", id="an underscore"),
        pytest.param("$U-USDT", "$U", "USDT", id="a dollar sign"),
        pytest.param("D.O.G.E.-USDT", "D.O.G.E.", "USDT", id="dots"),
        pytest.param("ATOM(ARC20)-USDT", "ATOM(ARC20)", "USDT", id="parentheses"),
        pytest.param(f"M{O_STROKE}TH-USDT", f"M{O_STROKE}TH", "USDT", id="a non-ASCII letter"),
        pytest.param("A-B-C", "A-B", "C", id="split on the last hyphen"),
        pytest.param("eth-USDT", "eth", "USDT", id="a lower-case base"),
        pytest.param("A" * 40 + "-" + "B" * 20, "A" * 40, "B" * 20, id="the longest of each"),
    ],
)
async def test_the_symbol_is_split_on_its_last_hyphen(symbol: str, base: str, quote: str) -> None:
    """The quote is the upper-case run after the **last** hyphen; the base is the rest.

    The id is namespaced by the symbol exactly as sent, whatever it holds.
    """
    fake = scripted(VenueFill(trade_id=7, executed_ms=INSIDE_MS, symbol=symbol))

    page = await fetch_page(fake)

    (fill,) = page.fills
    assert (fill.symbol, fill.base_asset, fill.quote_asset) == (symbol, base, quote)
    assert fill.external_trade_id == f"{symbol}:7"


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('"ETHUSDT"', id="no hyphen"),
        pytest.param('"ETH_USDT"', id="an underscore and no hyphen"),
        pytest.param('"eth-usdt"', id="lower case"),
        pytest.param('"ETH-usdt"', id="a lower-case quote"),
        pytest.param('"ETH-US.DT"', id="a dot in the quote"),
        pytest.param('"ETH-' + O_STROKE + 'USD"', id="a non-ASCII quote"),
        pytest.param('""', id="empty"),
        pytest.param('"-USDT"', id="no base"),
        pytest.param('"ETH-"', id="no quote"),
        pytest.param('"STRK-OLD-"', id="no quote after the last hyphen"),
        pytest.param('"' + "A" * 41 + '-USDT"', id="a 41-character base"),
        pytest.param('"ETH-' + "U" * 21 + '"', id="a 21-character quote"),
        pytest.param('"ETH -USDT"', id="a space in the base"),
        pytest.param('" ETH-USDT"', id="a leading space"),
        pytest.param('"ETH-USDT "', id="a trailing space"),
        pytest.param('"ETH\\t-USDT"', id="a tab"),
        pytest.param('"ETH\\u00a0-USDT"', id="a no-break space"),
        pytest.param('"ETH\\u0001-USDT"', id="a control character"),
        pytest.param('"ETH\\u200b-USDT"', id="a zero-width space, format"),
        pytest.param('"\\ue000-USDT"', id="a private-use character"),
        pytest.param('"\\u0378-USDT"', id="an unassigned code point"),
        pytest.param('"\\ud800-USDT"', id="a lone surrogate in the base"),
        pytest.param('"\\ud800"', id="a lone surrogate alone"),
        pytest.param("12345", id="a number"),
        pytest.param("null", id="null"),
        pytest.param(None, id="absent"),
    ],
)
async def test_a_symbol_that_is_not_base_hyphen_quote_is_refused(fragment: str | None) -> None:
    """No hyphen, a quote that is not upper-case ASCII, an empty or over-long side, or a base
    holding whitespace, a control, format, private-use, unassigned or surrogate character."""
    error = await refused(one_fill_fake(symbol=fragment))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "symbol")
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_an_unencodable_commission_asset_is_refused_naming_the_venues_field() -> None:
    """The provider checks UTF-8 itself, where it still knows the venue's name for the field.

    Left to `NormalizedFill`, the refusal would name `fee_asset` rather than
    `commissionAsset`, and carry the `UnicodeEncodeError` as its context -- whose `args` hold
    the whole string.
    """
    error = await refused(one_fill_fake(commissionAsset='"\\ud800"'))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "commissionAsset")
    assert "fee_asset" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_a_sell_is_a_sell() -> None:
    page = await fetch_page(one_fill_fake(isBuyer="false"))

    (fill,) = page.fills
    assert fill.side is FillSide.SELL


@pytest.mark.parametrize(
    "fragment",
    ['"true"', "1", "0", "null", '"buy"', None],
    ids=["a string", "one", "zero", "null", "buy", "absent"],
)
async def test_is_buyer_must_be_exactly_true_or_false(fragment: str | None) -> None:
    error = await refused(one_fill_fake(isBuyer=fragment))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "isBuyer")


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param(f'"{INSIDE_MS}"', id="a string of digits"),
        pytest.param(f"{INSIDE_MS}.0", id="a float"),
        pytest.param("-1", id="negative"),
        pytest.param("true", id="a boolean"),
        pytest.param("null", id="null"),
        pytest.param(None, id="absent"),
    ],
)
async def test_the_time_must_be_a_json_integer_of_milliseconds(fragment: str | None) -> None:
    error = await refused(one_fill_fake(time=fragment))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, "time")


async def test_a_seconds_timestamp_fails_the_page() -> None:
    """Seconds read as milliseconds land in 1970, outside the window: refused, not stored."""
    error = await refused(one_fill_fake(time=str(INSIDE_MS // 1000)))
    page = await fetch_page(one_fill_fake(time=str(INSIDE_MS)))

    assert type(error) is ExchangeSchemaError
    (fill,) = page.fills
    assert fill.executed_at == datetime(2023, 9, 27, 1, 0, tzinfo=UTC)


#: Every field the parser needs, and one mistyped value for each that carries a sentinel,
#: so a message quoting the value would be caught.
MISTYPED_SENTINEL: Final = "MISTYPED-VALUE-SENTINEL-5821"
REQUIRED_FIELDS: Final = ("id", "symbol", "price", "qty", "commission", "time", "isBuyer")


@pytest.mark.parametrize("shape", ["missing", "mistyped"])
@pytest.mark.parametrize("name", REQUIRED_FIELDS)
async def test_a_missing_or_mistyped_field_is_a_schema_error_naming_the_field(
    name: str, shape: str
) -> None:
    """Never a `KeyError`, `TypeError` or `AttributeError`; the field named, the value not."""
    fragment = None if shape == "missing" else f'{{"sentinel":"{MISTYPED_SENTINEL}"}}'

    error = await refused(one_fill_fake(**{name: fragment}))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, name)
    assert MISTYPED_SENTINEL not in f"{error}{error!r}{error.args}"


@pytest.mark.parametrize("name", ["quoteQty", "commissionAsset", "orderId"])
async def test_an_optional_field_of_the_wrong_type_is_a_schema_error_naming_it(name: str) -> None:
    error = await refused(one_fill_fake(**{name: f'{{"sentinel":"{MISTYPED_SENTINEL}"}}'}))

    assert type(error) is ExchangeSchemaError
    assert names_field(error, name)
    assert MISTYPED_SENTINEL not in f"{error}{error!r}{error.args}"


# -- the interpreter's own limits (spec 012, "What the plan got wrong") -----------------

EXPONENT_PAST_THE_LIMIT: Final = "1e1000000000000000000"


def past_the_integer_digit_limit() -> str:
    """A JSON integer with one digit more than this process will convert, read not assumed."""
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        message = "This interpreter has no integer string conversion limit to exceed."
        raise RuntimeError(message)
    return "1" * (limit + 1)


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def _one_fill(**overrides: str | None) -> str:
    return fills_body([VenueFill(trade_id=36_767_057, executed_ms=INSIDE_MS, overrides=overrides)])


#: Far past any scanner's recursion on any platform: `test_bitget.py` probes the real limit
#: and measured it near 3000 on Windows and past 5000 on the Pi.
UNREADABLY_DEEP: Final = 200_000

INTERPRETER_LIMIT_BODIES: Final[dict[str, Callable[[], str]]] = {
    "qty, a 5000-digit string": lambda: _one_fill(qty='"' + "1" * 5000 + '"'),
    "commission, a number with 5000 places": lambda: _one_fill(commission="-0." + "1" * 5000),
    "quoteQty, a 5000-digit string": lambda: _one_fill(quoteQty='"' + "1" * 5000 + '"'),
    "id, an integer past the digit limit": lambda: _one_fill(id=past_the_integer_digit_limit()),
    "time, an integer past the digit limit": lambda: _one_fill(time=past_the_integer_digit_limit()),
    "a field nested 1500 deep": lambda: _one_fill(nested=nested(1500)),
    "a field nested past any scanner": lambda: _one_fill(nested=nested(UNREADABLY_DEEP)),
    "qty, an exponent Decimal cannot hold, as a number": lambda: _one_fill(
        qty=EXPONENT_PAST_THE_LIMIT
    ),
    "price, an exponent Decimal cannot hold, as a string": lambda: _one_fill(
        price=f'"{EXPONENT_PAST_THE_LIMIT}"'
    ),
    "commission, a tiny exponent": lambda: _one_fill(commission="-1e-1000000000000000000"),
    "symbol, a lone surrogate": lambda: _one_fill(symbol='"\\ud800"'),
    "commissionAsset, a lone surrogate": lambda: _one_fill(commissionAsset='"\\ud800"'),
}


def test_the_nesting_cases_straddle_what_the_encoder_and_the_decoder_refuse() -> None:
    """The premises: 1500 deep decodes and is far past the encoder's bound of 32; the deep
    one does not decode; the long integer is refused by the decoder itself."""
    assert 10 * MAX_RAW_PAYLOAD_DEPTH < 1500
    decode_json(_one_fill(nested=nested(1500)))
    with pytest.raises(ProviderResponseError):
        decode_json(_one_fill(nested=nested(UNREADABLY_DEEP)))
    with pytest.raises(ProviderResponseError):
        decode_json(f'{{"n": {past_the_integer_digit_limit()}}}')


@pytest.mark.parametrize("case", sorted(INTERPRETER_LIMIT_BODIES))
async def test_the_interpreter_limits_are_schema_errors(case: str) -> None:
    """Integer digits, recursion depth, `Decimal`'s exponent and UTF-8: each a schema error."""
    fake = FakeBingX(replies=[Reply(body=INTERPRETER_LIMIT_BODIES[case]())])

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError


# -- the public pure functions refuse their own callers' mistakes -----------------------
#
# The provider never reaches these: it passes a clock's milliseconds, a `Decimal` from
# `require_fill_amount`, a fill object `_fill_items` has already checked, and the list
# `unwrap_envelope` returned. They are public so that they can be tested without HTTP, which
# makes them callable without the provider too, and a direct caller's mistake is refused
# rather than half-handled.


@pytest.mark.parametrize(
    "timestamp_ms",
    [True, -1, "1684814440729", Decimal(1684814440729)],
    ids=["a boolean", "negative", "a string", "a Decimal"],
)
def test_build_fills_query_refuses_a_timestamp_that_is_not_milliseconds(
    timestamp_ms: object,
) -> None:
    with pytest.raises(ValueError, match="timestamp_ms"):
        build_fills_query(WINDOW, cursor=None, timestamp_ms=timestamp_ms)  # type: ignore[arg-type]

    # The companion: zero is a timestamp, the epoch itself.
    assert build_fills_query(WINDOW, cursor=None, timestamp_ms=0).endswith("&timestamp=0")


@pytest.mark.parametrize(
    "value",
    [float("17.997667582000002"), "17.997667582000002", 17, None],
    ids=["a float", "a string", "an int", "None"],
)
def test_from_binary_float_takes_only_a_decimal(value: object) -> None:
    """Parse with `require_fill_amount` first; a float here would already have lost digits."""
    with pytest.raises(TypeError, match="Decimal"):
        from_binary_float(value)


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_from_binary_float_takes_only_a_finite_decimal(value: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        from_binary_float(Decimal(value))


def test_parse_fill_refuses_a_fill_that_is_not_an_object() -> None:
    with pytest.raises(ExchangeSchemaError, match="JSON object"):
        parse_fill([36767057])


@pytest.mark.parametrize("fills", [None, {}, "[]", 0], ids=["None", "a dict", "a string", "zero"])
def test_parse_fills_page_refuses_fills_that_are_not_an_array(fills: object) -> None:
    """Never read as "no fills": the same rule `unwrap_envelope` applies to the envelope."""
    with pytest.raises(ExchangeSchemaError, match="array"):
        parse_fills_page(fills, window=WINDOW, cursor=None)

    # The companion: an empty array is an empty page.
    page = parse_fills_page([], window=WINDOW, cursor=None)
    assert page.fills == ()
    assert page.next_cursor is None


EPOCH_INSTANT: Final = datetime(1970, 1, 1, tzinfo=UTC)


async def test_a_window_ending_at_the_epoch_is_a_caller_mistake_that_costs_no_request() -> None:
    """`endTime` would be `-1`, which is not a time: refused, not sent."""
    window = FillWindow(since=EPOCH_INSTANT - timedelta(milliseconds=2), until=EPOCH_INSTANT)
    fake = FakeBingX()

    with pytest.raises(ValueError, match="epoch"):
        build_fills_query(window, cursor=None, timestamp_ms=GOLDEN_TIMESTAMP_MS)
    with pytest.raises(ValueError, match="epoch"):
        parse_fills_page([], window=window, cursor=None)
    with pytest.raises(ValueError, match="epoch"):
        await fetch_page(fake, window)

    assert fake.requests == []


async def test_a_window_starting_before_the_epoch_sends_zero() -> None:
    """The companion: only the end must be after the epoch; the start is held at `0`, never
    sent negative. A day after the epoch is 86400000 ms, so `endTime` is 86399999."""
    window = FillWindow(
        since=EPOCH_INSTANT - timedelta(days=1), until=EPOCH_INSTANT + timedelta(days=1)
    )
    fake = FakeBingX()

    page = await fetch_page(fake, window)

    assert page.fills == ()
    (params,) = fake.params()
    assert params["startTime"] == "0"
    assert params["endTime"] == "86399999"


# --------------------------------------------------------------------------------------
# The retention bound: a first backfill reaches back a year, and says so
# --------------------------------------------------------------------------------------

#: 2023-10-01T00:00:00Z. 365 days before it is 2022-10-01T00:00:00Z (no 29 February in
#: between), and five minutes later is 00:05:00Z: `date -u -d 2022-10-01T00:05:00Z +%s` ->
#: 1664582700.
RETENTION_NOW: Final = datetime(2023, 10, 1, tzinfo=UTC)


async def test_retention_clamps_a_first_backfill_to_a_year() -> None:
    """A backfill from 2009 is clamped to a year and five minutes ago, and truncation says so.

    The clamped start is also a window the provider sends as it is.
    """
    clamp = clamp_to_retention(HISTORY_GENESIS, now=RETENTION_NOW, capabilities=BINGX_CAPABILITIES)

    assert clamp.clamped
    assert clamp.effective_since == datetime(2022, 10, 1, 0, 5, tzinfo=UTC)
    assert history_truncated(HISTORY_GENESIS, clamp.effective_since) is True
    assert history_truncated(clamp.effective_since, clamp.effective_since) is False

    fake = FakeBingX()
    window = FillWindow(
        since=clamp.effective_since, until=clamp.effective_since + timedelta(days=30)
    )
    page = await fetch_page(fake, window, clock=FixedClock(RETENTION_NOW))

    assert page.fills == ()
    (params,) = fake.params()
    assert params["startTime"] == "1664582700000"


def test_nothing_maps_to_the_retention_class() -> None:
    """What BingX answers for a window older than it keeps is unknown, so nothing is guessed."""
    assert ExchangeRetentionWindowError not in set(BINGX_ERROR_MAP.values())


# --------------------------------------------------------------------------------------
# Criterion 9: whatever the venue sends, one of the seven classes
# --------------------------------------------------------------------------------------

#: Text that can sit between two quotes in a hand-built body without ending the string.
QUOTABLE_TEXT: Final = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters='"\\'), max_size=12
)

#: Raw JSON fragments a venue could put where a field goes: every JSON type, the shapes the
#: parser distinguishes, and a few that have broken a parser in this repository before.
FRAGMENTS: Final = st.one_of(
    st.sampled_from(
        [
            "null",
            "true",
            "false",
            "0",
            "-0",
            "-1",
            "1e400",
            "1.5",
            "-1.2493e-7",
            "[]",
            "{}",
            '""',
            '"0"',
            '"BTC-USDT"',
            '"ETH-USDT"',
            '"17.997667582000002"',
            "1704961925000",
            "9223372036854775807",
            "9223372036854775808",
            '"\\ud800"',
            EXPONENT_PAST_THE_LIMIT,
        ]
    ),
    st.integers().map(str),
    st.decimals(allow_nan=False, allow_infinity=False).map(str),
    QUOTABLE_TEXT.map(lambda text: f'"{text}"'),
)

FIELD_NAMES: Final = st.sampled_from(
    [
        "symbol",
        "id",
        "orderId",
        "price",
        "qty",
        "quoteQty",
        "commission",
        "commissionAsset",
        "time",
        "isBuyer",
        "isMaker",
        "extra",
    ]
)


def outcome_of(call: Callable[[], object]) -> object:
    """What `call` returned, or the exchange error it raised. Anything else escapes."""
    try:
        return call()
    except ExchangeError as error:
        return error


def _parse_like_the_provider(status: int, body: str | bytes) -> FillPage:
    """Every step of `fetch_fill_page` after the response arrives, synchronously."""
    fills = unwrap_envelope(status, body)
    return parse_fills_page(fills, window=SAMPLE_WINDOW, cursor=None)


def test_the_sample_itself_parses_through_the_pure_steps() -> None:
    """The control for the properties below: with no edit, a page comes back."""
    page = _parse_like_the_provider(200, DOCUMENTED_SAMPLE_BODY)

    assert [fill.external_trade_id for fill in page.fills] == [
        "BTC-USDT:36767057",
        "BTC-USDT:36767058",
    ]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    edits=st.lists(st.tuples(FIELD_NAMES, st.one_of(st.none(), FRAGMENTS)), max_size=4),
    status=st.sampled_from([200, 200, 200, 400, 401, 403, 418, 429, 500, 503, 302]),
)
def test_whatever_the_venue_sends_only_the_seven_classes_escape(
    edits: list[tuple[str, str | None]], status: int
) -> None:
    """The documented sample fill with up to four fields replaced by anything at all.

    Either a page comes back, or one of the seven classes is raised: never a `KeyError`, a
    `TypeError`, a `ValueError` from the interpreter or a `ProviderResponseError` from the
    decoder.
    """
    fill = VenueFill(trade_id=36_767_057, executed_ms=1704961925000, overrides=dict(edits))
    body = fills_body([fill])

    outcome = outcome_of(lambda: _parse_like_the_provider(status, body))

    if isinstance(outcome, ExchangeError):
        assert type(outcome) in SEVEN_CLASSES
    else:
        assert isinstance(outcome, FillPage)
        assert status == 200


@settings(max_examples=200, deadline=None)
@given(status=st.integers(min_value=100, max_value=599), body=st.binary(max_size=64))
def test_any_status_with_any_body_is_one_of_the_seven_or_a_success(
    status: int, body: bytes
) -> None:
    outcome = outcome_of(lambda: unwrap_envelope(status, body))

    if isinstance(outcome, ExchangeError):
        assert type(outcome) in SEVEN_CLASSES
    else:
        assert status == 200, "only a 200 can be a success"


@settings(max_examples=200, deadline=None)
@given(code=st.one_of(st.integers(), FRAGMENTS), status=st.sampled_from([200, 400, 418, 503]))
def test_any_code_in_an_envelope_is_one_of_the_seven_or_the_integer_zero(
    code: int | str, status: int
) -> None:
    """Only `code: 0` on a 200 is a success; every other code is classified, never raised raw."""
    body = f'{{"code":{code},"msg":"","data":{{"fills":[]}}}}'

    outcome = outcome_of(lambda: unwrap_envelope(status, body))

    if isinstance(outcome, ExchangeError):
        assert type(outcome) in SEVEN_CLASSES
    else:
        assert status == 200
        document = decode_json(body)
        assert isinstance(document, dict)
        assert type(document["code"]) is int
        assert document["code"] == 0
