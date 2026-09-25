"""Criteria 2 to 6 and 9 of #13: the Bitget spot fills provider, against a fake venue.

Every expected value here comes from outside the code under test: the documented example
fill, copied verbatim from Bitget's Get Fills page; two signatures computed with `openssl`,
the command beside each; timestamps converted with `date -u`; trade ids and cursors worked
out by hand from the script and written as literals. The fake venue
(`bitget_harness.FakeBitget`) verifies every signed request with the standard library, so a
signing mistake fails every test that makes a request, not only the ones named after it.

The provider is driven through `fetch_fill_page` wherever the behaviour is observable there,
because that is what #15 will call. The pure functions are called directly only for the golden
vector (the pre-hash is not otherwise observable with a symbol in it) and for the property
test at the end, where thousands of generated bodies make a round trip through HTTP pointless.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
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
from portfolio.providers.exchanges.bitget import (
    ACCESS_KEY_HEADER,
    ACCESS_PASSPHRASE_HEADER,
    ACCESS_SIGN_HEADER,
    ACCESS_TIMESTAMP_HEADER,
    BITGET_API_URL,
    BITGET_CAPABILITIES,
    BITGET_ERROR_MAP,
    FILLS_PATH,
    SYMBOLS_PATH,
    SymbolAssets,
    build_fills_query,
    build_prehash,
    fill_symbols,
    parse_fills_page,
    require_trade_id,
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
from portfolio.providers.exchanges.signing import hmac_sha256_base64
from portfolio.providers.http import request_target
from tests.providers.exchanges.bitget_harness import (
    ACCESS_KEY_SENTINEL,
    DOCUMENTED_FILLS_PATH,
    GOLDEN_NOW,
    GOLDEN_TIMESTAMP_MS,
    HTML_BODY,
    PHRASE_SENTINEL,
    SIGNING_SENTINEL,
    WINDOW,
    WINDOW_SINCE_MS,
    WINDOW_UNTIL_MS,
    Bounds,
    FakeBitget,
    FixedClock,
    Order,
    Reply,
    VenueFill,
    bitget_client,
    bitget_provider,
    error_body,
    fetch_page,
    fills_body,
    spread_fills,
    symbol_entry,
    symbols_body,
    synthetic_credentials,
    walk,
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


#: Built with `chr`, so the source stays ASCII and nobody has to squint at a diff: RUF001
#: rightly calls a literal fullwidth digit ambiguous with the ASCII one.
FULLWIDTH_DIGITS: Final = chr(0xFF11) + chr(0xFF12)
E_ACUTE: Final = chr(0xE9)


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


async def refused(
    fake: FakeBitget, window: FillWindow = WINDOW, *, cursor: str | None = None
) -> ExchangeError:
    """The exchange error one fetch raises. Anything that is not an `ExchangeError` escapes."""
    with pytest.raises(ExchangeError) as caught:
        await fetch_page(fake, window, cursor=cursor)
    return caught.value


def one_fill_fake(**overrides: str | None) -> FakeBitget:
    """A venue whose only answer is one fill inside `WINDOW`, with `overrides` applied.

    Keyword names cannot carry a dot, so `feeDetail__totalFee` stands for
    `feeDetail.totalFee`.
    """
    fill = VenueFill(
        trade_id=1_000_003,
        executed_ms=WINDOW_SINCE_MS + 60_000,
        overrides={name.replace("__", "."): fragment for name, fragment in overrides.items()},
    )
    return FakeBitget(fill_replies=[Reply(body=fills_body([fill]))])


# --------------------------------------------------------------------------------------
# The documented constants
# --------------------------------------------------------------------------------------


def test_the_venue_constants_are_the_documented_ones() -> None:
    """Pinned as literals, once. The fake reads the host off `BITGET_API_URL`.

    The paths and the header names are what Get Fills, Get Symbol Info and the REST
    introduction document (read 2026-09-25), so a typo here is a request the venue refuses.
    """
    assert BITGET_API_URL == "https://api.bitget.com"
    assert FILLS_PATH == "/api/v2/spot/trade/fills"
    assert SYMBOLS_PATH == "/api/v2/spot/public/symbols"
    assert ACCESS_KEY_HEADER == "ACCESS-KEY"
    assert ACCESS_SIGN_HEADER == "ACCESS-SIGN"
    assert ACCESS_TIMESTAMP_HEADER == "ACCESS-TIMESTAMP"
    assert ACCESS_PASSPHRASE_HEADER == "ACCESS-PASSPHRASE"  # noqa: S105 - a header's name


def test_the_capabilities_are_the_documented_ones() -> None:
    """Spec 014's declaration, field by field, from a literal written by hand.

    `max_query_window` is 30 days on purpose, below the documented 90; `retention` is the
    documented 90; the rate limit is Get Fills' 10 requests a second per UID.
    """
    assert (
        ExchangeCapabilities(
            exchange_key=ExchangeKey.BITGET,
            retention=timedelta(days=90),
            max_query_window=timedelta(days=30),
            page_size=100,
            cursor_kind=CursorKind.TRADE_ID_BEFORE,
            rate_limit=RateLimit(max_requests=10, per_ms=1000),
            requires_symbol=False,
        )
        == BITGET_CAPABILITIES
    )


async def test_the_provider_declares_them_and_needs_no_symbol_list() -> None:
    """`symbol` is optional on Get Fills, so there is nothing to enumerate and nothing asked."""
    fake = FakeBitget()
    async with bitget_client(fake) as client:
        provider = bitget_provider(client)

        assert provider.capabilities == BITGET_CAPABILITIES
        assert list(await provider.candidate_symbols()) == []

    assert fake.requests == []


# --------------------------------------------------------------------------------------
# Criterion 2: the signature, against vectors computed outside this code
# --------------------------------------------------------------------------------------

#: The pre-hash of Bitget's official Python SDK for a signed GET, with its sample query.
SDK_VECTOR_PREHASH: Final = (
    "1684814440729GET/api/v2/spot/trade/fills?idLessThan=12345678910&limit=100&symbol=BTCUSDT"
)
SDK_VECTOR_SIGNATURE: Final = "EhrSzSzM7SVAe3w2KKc1QWKmBHbLWu2l/jdM3Qa7mxY="
r"""Computed outside this code, with OpenSSL 3.5.4, which printed exactly the literal above:

    printf '%s' \
      '1684814440729GET/api/v2/spot/trade/fills?idLessThan=12345678910&limit=100&symbol=BTCUSDT' \
      | openssl dgst -sha256 -hmac 'dummy-secret-not-a-real-key' -binary | openssl base64 -A
"""

#: The request the provider builds for `VECTOR_WINDOW` and cursor `12345678910` at
#: `GOLDEN_NOW`: path and query exactly as they go on the wire.
PROVIDER_VECTOR_TARGET: Final = (
    "/api/v2/spot/trade/fills"
    "?endTime=1695900000000&idLessThan=12345678910&limit=100&startTime=1695800000000"
)
PROVIDER_VECTOR_SIGNATURE: Final = "incJ9meeHk+l8ZNsbae3jCvckiICFIAPvapsdTcQ6AA="
r"""Computed outside this code, with OpenSSL 3.5.4, which printed exactly the literal above.

The pre-hash is too long for one line, so it is given to `printf` as two arguments, which
`'%s'` prints back to back:

    printf '%s' \
      '1684814440729GET/api/v2/spot/trade/fills?endTime=1695900000000&idLessThan=12345678910' \
      '&limit=100&startTime=1695800000000' \
      | openssl dgst -sha256 -hmac 'dummy-secret-not-a-real-key' -binary | openssl base64 -A
"""

#: `startTime` is `since` minus one millisecond, so `since` is 1695800000001 ms:
#: `date -u -d @1695800000.001` -> 2023-09-27T07:33:20.001Z. `endTime` is `until`, 1695900000000
#: ms: `date -u -d @1695900000` -> 2023-09-28T11:20:00Z.
VECTOR_WINDOW: Final = FillWindow(
    since=datetime(2023, 9, 27, 7, 33, 20, 1000, tzinfo=UTC),
    until=datetime(2023, 9, 28, 11, 20, tzinfo=UTC),
)


def test_the_golden_instant_is_the_vector_timestamp() -> None:
    """The premise of the next two tests: `GOLDEN_NOW` is 1684814440729 ms after the epoch."""
    assert (GOLDEN_NOW - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1) == (
        GOLDEN_TIMESTAMP_MS
    )


def test_the_signature_matches_the_sdk_golden_vector() -> None:
    """The provider's pre-hash and the shared signer, against the `openssl` literal.

    Bitget publishes no vector of its own (its samples use an empty secret and print no
    output), so this one was computed outside the code, with the SDK's pre-hash algorithm.
    """
    prehash = build_prehash(
        GOLDEN_TIMESTAMP_MS,
        "/api/v2/spot/trade/fills",
        "idLessThan=12345678910&limit=100&symbol=BTCUSDT",
    )

    assert prehash == SDK_VECTOR_PREHASH
    assert hmac_sha256_base64(SecretStr(SIGNING_SENTINEL), prehash) == SDK_VECTOR_SIGNATURE


async def test_the_signature_of_the_request_the_provider_sends() -> None:
    """A fixed clock, window and cursor: the request on the wire, byte for byte, and its sign."""
    fake = FakeBitget()

    page = await fetch_page(fake, VECTOR_WINDOW, cursor="12345678910")

    assert page.fills == ()
    (request,) = fake.fill_requests
    assert request.method == "GET"
    assert request.url.raw_path.decode("ascii") == PROVIDER_VECTOR_TARGET
    assert request.headers["ACCESS-TIMESTAMP"] == "1684814440729"
    assert request.headers["ACCESS-SIGN"] == PROVIDER_VECTOR_SIGNATURE
    assert fake.signature_failures == []


async def test_every_request_carries_the_four_access_headers_and_a_verifiable_signature() -> None:
    """A three-page walk over two symbols: every fills request signed, no symbol request signed.

    The symbol-info endpoint is public, and a credential sent to an endpoint that does not
    need it is a credential sent for nothing.
    """
    fills = spread_fills(250, symbols=("BTCUSDT", "KASUSDT"))
    fake = FakeBitget(fills)

    await walk(fake)

    assert len(fake.fill_requests) == 3
    assert fake.verified == fake.fill_requests
    assert fake.signature_failures == []
    for request in fake.fill_requests:
        assert request.headers["ACCESS-KEY"] == ACCESS_KEY_SENTINEL
        assert request.headers["ACCESS-PASSPHRASE"] == PHRASE_SENTINEL
        assert request.headers["ACCESS-TIMESTAMP"] == str(GOLDEN_TIMESTAMP_MS)
        assert request.headers["ACCESS-SIGN"]
    # The positive companion to the absence below: the symbol endpoint was asked.
    assert len(fake.symbol_requests) == 2
    assert fake.access_headers_on_symbol_requests() == []
    for request in fake.symbol_requests:
        carried = " ".join(f"{name}: {value}" for name, value in request.headers.items())
        for sentinel in (ACCESS_KEY_SENTINEL, PHRASE_SENTINEL, SIGNING_SENTINEL):
            assert sentinel not in carried


async def test_each_request_is_labelled_for_the_log() -> None:
    """The label is the only thing about a request's target the transport logs.

    A label off `ENDPOINT_LABELS` renders `<unlabelled>`, so asserting the rendered target
    too is what shows the two labels were added to the allowlist and not only passed.
    """
    fake = FakeBitget(spread_fills(1))

    await fetch_page(fake)

    assert [request.extensions.get("endpoint") for request in fake.fill_requests] == [
        "exchange_fills"
    ]
    assert [request.extensions.get("endpoint") for request in fake.symbol_requests] == [
        "exchange_symbol"
    ]
    assert request_target(fake.fill_requests[0]) == "https://api.bitget.com/exchange_fills"
    assert request_target(fake.symbol_requests[0]) == "https://api.bitget.com/exchange_symbol"


async def test_the_fake_venue_refuses_a_request_signed_with_another_secret() -> None:
    """The control on the verifier: it can say no, so its yes above means something."""
    fake = FakeBitget(spread_fills(3), signing_key="another-secret-entirely-not-real")

    error = await refused(fake)

    assert type(error) is ExchangeAuthError
    assert error.venue_code == "40009"
    assert len(fake.signature_failures) == len(fake.fill_requests) == 1
    assert "does not verify" in fake.signature_failures[0]


async def test_the_query_is_sent_exactly_as_signed() -> None:
    """The query on the wire is sorted, needs no encoding, and is the text that was signed."""
    fake = FakeBitget()

    await fetch_page(fake, VECTOR_WINDOW, cursor="12345678910")
    await fetch_page(fake, VECTOR_WINDOW)

    queries = [request.url.query.decode("ascii") for request in fake.fill_requests]
    assert queries == [
        "endTime=1695900000000&idLessThan=12345678910&limit=100&startTime=1695800000000",
        "endTime=1695900000000&limit=100&startTime=1695800000000",
    ]
    for request, query in zip(fake.fill_requests, queries, strict=True):
        keys = [pair.partition("=")[0] for pair in query.split("&")]
        assert keys == sorted(keys)
        assert all(
            character.isascii() and character.isalnum()
            for character in query.replace("&", "").replace("=", "")
        )
        signed_over = f"{DOCUMENTED_FILLS_PATH}?{query}"
        timestamp = request.headers["ACCESS-TIMESTAMP"]
        assert request.headers["ACCESS-SIGN"] == fake.expected_signature(
            timestamp, "GET", signed_over
        )
    assert build_fills_query(VECTOR_WINDOW, cursor="12345678910") == queries[0]
    assert build_fills_query(VECTOR_WINDOW, cursor=None) == queries[1]


# --------------------------------------------------------------------------------------
# Criterion 3: pagination terminates, by construction
# --------------------------------------------------------------------------------------
#
# `spread_fills(250)` holds trade ids 1000003, 1000006, ..., 1000750 (step 3). The venue pages
# to older ids, so the first page is the 100 highest -- 1000453 to 1000750 -- the second is
# 1000153 to 1000450, and the third the remaining 50. Worked out by hand, not by the code.

FIRST_PAGE_CURSOR: Final = "1000453"
SECOND_PAGE_CURSOR: Final = "1000153"


async def test_a_multi_page_walk_returns_every_fill_once_and_stops() -> None:
    script = spread_fills(250)
    fake = FakeBitget(script)

    pages = await walk(fake)

    assert len(fake.fill_requests) == 3
    assert [len(page.fills) for page in pages] == [100, 100, 50]
    ids = [fill.external_trade_id for page in pages for fill in page.fills]
    assert len(ids) == len(set(ids)) == 250
    assert set(ids) == {str(fill.trade_id) for fill in script}
    assert [page.next_cursor for page in pages] == [FIRST_PAGE_CURSOR, SECOND_PAGE_CURSOR, None]


async def test_the_cursor_is_the_smallest_trade_id_never_the_order_id() -> None:
    """`idLessThan` takes a `tradeId`. Every order id in the script is above every trade id.

    So a provider sending an order id would be answered the first page again: a repeat the
    cursor guard refuses, or duplicates in the walk. Either way the literals below fail.
    """
    script = spread_fills(250)
    fake = FakeBitget(script)

    await walk(fake)

    sent = [query.get("idLessThan") for query in fake.fill_queries()]
    assert sent == [None, FIRST_PAGE_CURSOR, SECOND_PAGE_CURSOR]
    # Recomputed from what the venue served, which the provider never saw.
    for served, cursor in zip(fake.served_trade_ids, sent[1:], strict=False):
        assert cursor == str(min(served))
    order_ids = {str(fill.order) for fill in script}
    assert min(int(order) for order in order_ids) > max(fill.trade_id for fill in script)
    assert not order_ids.intersection(cursor for cursor in sent if cursor is not None)


@pytest.mark.parametrize("order", list(Order), ids=lambda order: order.value)
async def test_the_cursor_is_the_smallest_id_whatever_order_the_page_is_in(order: Order) -> None:
    """The order within a page is undocumented; the smallest id is right under any order.

    Under the ascending order the last fill is the *largest* id, so "the last fill" as a
    cursor would skip 99 fills; the shuffled order catches every other positional choice.
    """
    fake = FakeBitget(spread_fills(250), order=order)

    page = await fetch_page(fake)

    assert page.next_cursor == FIRST_PAGE_CURSOR
    (served,) = fake.served_trade_ids
    # The premise: the venue really served that order.
    if order is Order.ASCENDING:
        assert list(served) == sorted(served)
    elif order is Order.DESCENDING:
        assert list(served) == sorted(served, reverse=True)
    else:
        assert list(served) not in (sorted(served), sorted(served, reverse=True))
    assert served[-1] != min(served) or order is Order.DESCENDING


async def test_a_venue_that_ignores_the_cursor_raises_instead_of_looping() -> None:
    """The same full page served again: refused on the second request, never a third."""
    fake = FakeBitget(spread_fills(250), ignore_cursor=True)

    with pytest.raises(ExchangeSchemaError) as caught:
        await walk(fake)

    assert type(caught.value) is ExchangeSchemaError
    assert len(fake.fill_requests) == 2
    assert fake.served_trade_ids[0] == fake.served_trade_ids[1], "the premise: a repeat"


async def test_a_venue_that_cycles_between_two_pages_is_refused() -> None:
    """A -> B -> A, which `require_cursor_advanced` alone cannot see. Strict decrease can."""
    newer = spread_fills(100, first_id=2001, step=1)
    older = spread_fills(100, first_id=1001, step=1)
    fake = FakeBitget(
        fill_replies=[
            Reply(body=fills_body(newer)),
            Reply(body=fills_body(older)),
            Reply(body=fills_body(newer)),
        ]
    )

    with pytest.raises(ExchangeSchemaError):
        await walk(fake)

    assert [query.get("idLessThan") for query in fake.fill_queries()] == [None, "2001", "1001"]


@pytest.mark.parametrize(
    "offending_id",
    [7_654_321, 7_654_322],
    ids=["equal to the cursor", "above the cursor"],
)
async def test_a_page_with_an_id_at_or_above_the_cursor_is_refused(offending_id: int) -> None:
    """A short page, so no next cursor exists for the repeat guard to catch: only this check."""
    page = [
        VenueFill(trade_id=7_654_000, executed_ms=WINDOW_SINCE_MS + 60_000),
        VenueFill(trade_id=offending_id, executed_ms=WINDOW_SINCE_MS + 120_000),
    ]
    fake = FakeBitget(fill_replies=[Reply(body=fills_body(page))])

    error = await refused(fake, cursor="7654321")

    assert type(error) is ExchangeSchemaError
    assert str(offending_id) not in f"{error}{error!r}", "a message never names a trade id"


async def test_a_page_wholly_below_the_cursor_is_accepted() -> None:
    """The companion: the same page one id lower passes, so the refusal above is the rule."""
    page = [
        VenueFill(trade_id=7_654_000, executed_ms=WINDOW_SINCE_MS + 60_000),
        VenueFill(trade_id=7_654_320, executed_ms=WINDOW_SINCE_MS + 120_000),
    ]
    fake = FakeBitget(fill_replies=[Reply(body=fills_body(page))])

    result = await fetch_page(fake, cursor="7654321")

    assert {fill.external_trade_id for fill in result.fills} == {"7654000", "7654320"}
    assert result.next_cursor is None


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, None), (99, None), (100, "1000003")],
    ids=["empty", "99 fills", "100 fills"],
)
async def test_a_short_page_has_no_next_cursor(count: int, expected: str | None) -> None:
    """Only a full page (`limit` = 100 fills) can have more behind it."""
    fake = FakeBitget(spread_fills(count))

    page = await fetch_page(fake)

    assert len(page.fills) == count
    assert page.next_cursor == expected


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('"0"', id="zero"),
        pytest.param('"012"', id="leading zero"),
        pytest.param('"12a"', id="a letter"),
        pytest.param('"-1"', id="negative"),
        pytest.param('"1.0"', id="a decimal point"),
        pytest.param('" 1"', id="a leading space"),
        pytest.param('""', id="empty"),
        pytest.param(f'"{FULLWIDTH_DIGITS}"', id="fullwidth digits"),
        pytest.param('"9223372036854775808"', id="one past the signed 64-bit maximum"),
        pytest.param('"' + "1" * 20 + '"', id="twenty digits"),
        pytest.param('"' + "1" * 5000 + '"', id="5000 digits"),
        pytest.param("12345678910", id="a JSON number"),
        pytest.param("null", id="null"),
    ],
)
async def test_a_malformed_trade_id_is_a_schema_error(fragment: str) -> None:
    error = await refused(one_fill_fake(tradeId=fragment))

    assert type(error) is ExchangeSchemaError
    assert "tradeId" in str(error)


@pytest.mark.parametrize("trade_id", ["1", "9223372036854775807"])
async def test_the_trade_id_range_ends_at_the_signed_64_bit_maximum(trade_id: str) -> None:
    """The companion: the smallest and the largest ids the rule admits, kept as sent."""
    page = await fetch_page(one_fill_fake(tradeId=f'"{trade_id}"'))

    assert [fill.external_trade_id for fill in page.fills] == [trade_id]


@pytest.mark.parametrize(
    "cursor",
    [
        "0",
        "012",
        "12a",
        "-1",
        "",
        " 12",
        "12 ",
        "1.5",
        FULLWIDTH_DIGITS,
        "\ud800",
        "1" * 20,
        "9223372036854775808",
    ],
)
async def test_a_malformed_cursor_from_the_caller_is_a_value_error_and_sends_nothing(
    cursor: str,
) -> None:
    """A caller's mistake costs no signed request, and is not dressed up as a venue's."""
    fake = FakeBitget()

    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the class is the contract here
        await fetch_page(fake, cursor=cursor)

    assert not isinstance(caught.value, ExchangeError)
    assert fake.requests == []


def test_the_trade_id_rule_is_the_pattern_and_the_64_bit_bound() -> None:
    """The pure rule, both edges, in one place."""
    assert require_trade_id("9223372036854775807") == "9223372036854775807"
    with pytest.raises(ExchangeSchemaError):
        require_trade_id("9223372036854775808")
    with pytest.raises(ExchangeSchemaError):
        require_trade_id(9223372036854775807)


# --------------------------------------------------------------------------------------
# Criterion 4: every failure lands in its class
# --------------------------------------------------------------------------------------

#: A code Bitget does not use and the map does not hold, so a JSON body carrying it leaves
#: the classification to the status. `test_the_unmapped_code_is_unmapped` is the premise.
UNMAPPED_CODE: Final = "99999"

STATUS_BODIES: Final = {"html": HTML_BODY, "json": error_body(UNMAPPED_CODE)}


def test_the_unmapped_code_is_unmapped() -> None:
    assert all(code != UNMAPPED_CODE for _status, code in BITGET_ERROR_MAP)


@pytest.mark.parametrize("body", sorted(STATUS_BODIES))
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ExchangeAuthError),
        (403, ExchangeAuthError),
        (429, ExchangeRateLimitedError),
        (500, ExchangeUnavailableError),
        (502, ExchangeUnavailableError),
        (503, ExchangeUnavailableError),
    ],
)
async def test_each_status_maps_to_its_taxonomy_class(
    status: int, expected: type[ExchangeError], body: str
) -> None:
    """The exact class, never a superclass, and never chained from an HTTP status error."""
    fake = FakeBitget(
        fill_replies=[Reply(status=status, body=STATUS_BODIES[body], headers={"Retry-After": "7"})]
    )

    error = await refused(fake)

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == (UNMAPPED_CODE if body == "json" else None)
    if isinstance(error, ExchangeRateLimitedError):
        assert error.retry_after_ms == 7000
    assert not any(isinstance(link, httpx.HTTPStatusError) for link in exception_chain(error))


async def test_a_rate_limit_without_retry_after_carries_none() -> None:
    """`None` is "the venue said nothing", which is not `0`, "immediately"."""
    error = await refused(FakeBitget(fill_replies=[Reply(status=429, body=error_body("429"))]))

    assert type(error) is ExchangeRateLimitedError
    assert error.retry_after_ms is None


#: Spec 014's table, one code per row entry, written by hand from the spec.
DOCUMENTED_CODES: Final[dict[str, type[ExchangeError]]] = {
    # The owner has to fix the key.
    "40006": ExchangeAuthError,
    "40037": ExchangeAuthError,
    "40041": ExchangeAuthError,
    "40012": ExchangeAuthError,
    "40036": ExchangeAuthError,
    "40009": ExchangeAuthError,
    "40038": ExchangeAuthError,
    "40018": ExchangeAuthError,
    # The key lacks read permission.
    "40014": ExchangeInsufficientScopeError,
    "40025": ExchangeInsufficientScopeError,
    "40040": ExchangeInsufficientScopeError,
    # A timestamp the venue refused: not auth, because a replayed request expires.
    "40008": ExchangeUnavailableError,
    "40005": ExchangeUnavailableError,
    # The in-band throttle.
    "429": ExchangeRateLimitedError,
    # Older than the history the venue keeps.
    "40704": ExchangeRetentionWindowError,
    # A request we built wrongly.
    "00001": ExchangeInvalidRequestError,
    "40705": ExchangeInvalidRequestError,
    "40707": ExchangeInvalidRequestError,
    "40017": ExchangeInvalidRequestError,
    "40019": ExchangeInvalidRequestError,
    "40020": ExchangeInvalidRequestError,
    "40034": ExchangeInvalidRequestError,
    "40102": ExchangeInvalidRequestError,
    # Deploy-time errors the FAQ says to retry.
    "45001": ExchangeUnavailableError,
    "40725": ExchangeUnavailableError,
    "40808": ExchangeUnavailableError,
    "40015": ExchangeUnavailableError,
}


def test_the_error_map_is_exactly_the_documented_table() -> None:
    """Every key `(None, code)`, because the docs tie no code to a status; nothing extra."""
    assert dict(BITGET_ERROR_MAP) == {(None, code): cls for code, cls in DOCUMENTED_CODES.items()}
    assert isinstance(BITGET_ERROR_MAP, MappingProxyType)


@pytest.mark.parametrize("status", [400, 200])
@pytest.mark.parametrize(("code", "expected"), sorted(DOCUMENTED_CODES.items()))
async def test_each_in_band_code_maps_to_its_class(
    code: str, expected: type[ExchangeError], status: int
) -> None:
    """On a 400 and on a 200: the code decides, so a refusal on a 200 is not a schema error."""
    fake = FakeBitget(fill_replies=[Reply(status=status, body=error_body(code))])

    error = await refused(fake)

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == code


@pytest.mark.parametrize("status", [400, 401, 200])
@pytest.mark.parametrize("code", ["40008", "40005"])
async def test_a_timestamp_error_is_never_an_auth_error(code: str, status: int) -> None:
    """A replayed request can arrive expired; #15 must not mark a working key `auth_failed`."""
    error = await refused(FakeBitget(fill_replies=[Reply(status=status, body=error_body(code))]))

    assert type(error) is ExchangeUnavailableError
    assert not isinstance(error, ExchangeAuthError)


@pytest.mark.parametrize("code", ["40001", "40002", "40003", "40011"])
async def test_a_header_missing_code_is_not_mapped(code: str) -> None:
    """The provider always sends every header: one of these is our bug, not a bad key."""
    assert (None, code) not in BITGET_ERROR_MAP

    on_400 = await refused(FakeBitget(fill_replies=[Reply(status=400, body=error_body(code))]))
    on_200 = await refused(FakeBitget(fill_replies=[Reply(status=200, body=error_body(code))]))

    assert type(on_400) is ExchangeInvalidRequestError
    assert type(on_200) is ExchangeSchemaError


async def test_a_transport_failure_is_unavailable_and_chains_only_the_transport_error() -> None:
    cause = httpx.ConnectError("the fake venue refused the connection")
    fake = FakeBitget(fill_replies=[Reply(error=cause)])

    error = await refused(fake)

    assert type(error) is ExchangeUnavailableError
    assert error.status is None
    assert error.__cause__ is cause
    assert not any(isinstance(link, httpx.HTTPStatusError) for link in exception_chain(error))
    rendered = f"{error}{error!r}{error.args}"
    for fragment in ("idLessThan", "startTime", "api.bitget.com", "/api/v2"):
        assert fragment not in rendered
    # The positive companion: the request was made, and retried, before it failed.
    assert len(fake.fill_requests) == 3


async def test_a_local_protocol_error_is_an_invalid_request_that_carries_nothing() -> None:
    """h11 quotes the whole illegal header value, which would be the key. Raised `from None`.

    Reachable only through a bug, since the constructor refuses such a key; a mock transport
    raising it is the only way to get here, and the sentinel stands for the key it quotes.
    """
    sentinel = "LOCAL-PROTOCOL-SENTINEL-7319"
    fake = FakeBitget(
        fill_replies=[Reply(error=httpx.LocalProtocolError(f"Illegal header value b'{sentinel}'"))]
    )

    error = await refused(fake)

    assert type(error) is ExchangeInvalidRequestError
    assert error.status is None
    assert error.__cause__ is None
    for link in exception_chain(error):
        assert sentinel not in f"{link}{link!r}{link.args}"
    assert fake.fill_requests, "the positive companion: the request was attempted"


async def test_the_transport_replays_the_same_signed_request() -> None:
    """A 429 then a 200: the retry resends the request as signed. The decision, pinned.

    The clock moves a second on every read, so a provider that re-signed per attempt would
    send a different timestamp and a different signature, and this would say so.
    """
    clock = TickingClock()
    fake = FakeBitget(
        fill_replies=[Reply(status=429, body=error_body("429")), Reply(body=fills_body([]))]
    )

    page = await fetch_page(fake, clock=clock)

    assert page.fills == ()
    first, second = fake.fill_requests
    assert first.headers["ACCESS-TIMESTAMP"] == second.headers["ACCESS-TIMESTAMP"]
    assert first.headers["ACCESS-SIGN"] == second.headers["ACCESS-SIGN"]
    assert fake.signature_failures == []


class TickingClock:
    """A clock one second later on every read."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return GOLDEN_NOW + timedelta(seconds=self.reads)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json at all", id="not JSON"),
        pytest.param(HTML_BODY, id="HTML"),
        pytest.param("[]", id="an array"),
        pytest.param('"00000"', id="a string"),
        pytest.param('{"code":"00000","msg":"success","requestTime":1}', id="no data"),
        pytest.param('{"code":"00000","msg":"success","data":{}}', id="data an object"),
        pytest.param('{"code":"00000","msg":"success","data":null}', id="data null"),
        pytest.param('{"code":0,"msg":"success","data":[]}', id="code a number"),
        pytest.param('{"msg":"success","data":[]}', id="no code"),
        pytest.param('{"code":"00000","data":[1]}', id="a fill that is not an object"),
        pytest.param('{"code":"00000","data":[[]]}', id="a fill that is an array"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_success_status_with_a_body_of_the_wrong_shape_is_a_schema_error(body: str) -> None:
    """HTTP 200 and a body that is not the documented envelope: schema, `from None`."""
    error = await refused(FakeBitget(fill_replies=[Reply(body=body)]))

    assert type(error) is ExchangeSchemaError
    assert error.__cause__ is None


# --------------------------------------------------------------------------------------
# Criterion 5: fills normalize to exact `Decimal` values
# --------------------------------------------------------------------------------------

#: Bitget's Get Fills response example, verbatim (read 2026-09-25). Split only to fit the
#: line length; the concatenation is the documented text character for character.
DOCUMENTED_EXAMPLE_BODY: Final = (
    '{"code":"00000","msg":"success","requestTime":1695865274510,"data":[{"userId":"**********",'
    '"symbol":"BTCUSDT","orderId":"12345678910","tradeId":"12345678910","orderType":"market",'
    '"side":"buy","priceAvg":"13000","size":"0.0007","amount":"9.1","feeDetail":{"deduction":"no",'
    '"feeCoin":"BTC","totalDeductionFee":"","totalFee":"-0.0000007"},"tradeScope":"taker",'
    '"cTime":"1695865232579","uTime":"1695865233027"}]}'
)

#: The fill object inside it, duplicated rather than sliced out, so the round-trip assertion
#: compares against text the code under test never touched.
DOCUMENTED_EXAMPLE_FILL: Final = (
    '{"userId":"**********","symbol":"BTCUSDT","orderId":"12345678910","tradeId":"12345678910",'
    '"orderType":"market","side":"buy","priceAvg":"13000","size":"0.0007","amount":"9.1",'
    '"feeDetail":{"deduction":"no","feeCoin":"BTC","totalDeductionFee":"","totalFee":"-0.0000007"},'
    '"tradeScope":"taker","cTime":"1695865232579","uTime":"1695865233027"}'
)


async def test_the_documented_example_normalizes_exactly() -> None:
    """Every value by hand from the example: `13000 x 0.0007 = 9.1`, fee 0.1% of the size.

    `cTime` 1695865232579 is 2023-09-28T01:40:32.579Z (`date -u -d @1695865232.579`).
    """
    assert DOCUMENTED_EXAMPLE_FILL in DOCUMENTED_EXAMPLE_BODY
    fake = FakeBitget(fill_replies=[Reply(body=DOCUMENTED_EXAMPLE_BODY)])

    page = await fetch_page(fake)

    (fill,) = page.fills
    assert fill.external_trade_id == "12345678910"
    assert fill.external_order_id == "12345678910"
    assert fill.symbol == "BTCUSDT"
    assert fill.base_asset == "BTC"
    assert fill.quote_asset == "USDT"
    assert fill.side is FillSide.BUY
    assert exact(fill.quantity, "0.0007")
    assert exact(fill.price, "13000")
    assert exact(fill.quote_quantity, "9.1")
    assert fill.quote_quantity_derived is False
    assert exact(fill.fee_amount, "0.0000007")
    assert fill.fee_asset == "BTC"
    assert fill.executed_at == datetime(2023, 9, 28, 1, 40, 32, 579000, tzinfo=UTC)
    raw = decode_json(fill.raw_payload)
    assert raw == decode_json(DOCUMENTED_EXAMPLE_FILL)
    assert isinstance(raw, dict)
    for envelope_field in ("code", "msg", "requestTime", "data"):
        assert envelope_field not in raw
    assert page.next_cursor is None


@pytest.mark.parametrize("fragment", [None, "null", '""'], ids=["absent", "null", "empty string"])
async def test_a_missing_amount_is_derived_and_flagged(fragment: str | None) -> None:
    """`0.0007 x 13000` is exactly 9.1, written at the fill scale of eighteen places."""
    page = await fetch_page(one_fill_fake(amount=fragment))

    (fill,) = page.fills
    assert fill.quote_quantity_derived is True
    assert exact(fill.quote_quantity, "9.100000000000000000")


async def test_a_positive_total_fee_is_refused() -> None:
    """The REST example reports a fee paid as negative. A positive one is not guessed at.

    The companion: a zero fee with no coin is accepted, with no asset.
    """
    error = await refused(one_fill_fake(feeDetail__totalFee='"0.0000007"'))

    assert type(error) is ExchangeSchemaError
    assert "feeDetail.totalFee" in str(error)
    assert "0.0000007" not in f"{error}{error!r}"

    page = await fetch_page(one_fill_fake(feeDetail__totalFee='"0"', feeDetail__feeCoin='""'))
    (fill,) = page.fills
    assert fill.fee_amount == 0
    assert fill.fee_asset is None


@pytest.mark.parametrize(
    ("total_fee", "coin", "expected_fee", "expected_asset"),
    [
        pytest.param('"0"', None, "0", None, id="zero, no coin"),
        pytest.param('"0"', "null", "0", None, id="zero, null coin"),
        pytest.param('"0"', '""', "0", None, id="zero, empty coin"),
        pytest.param('"-0"', '""', "0", None, id="negative zero"),
        pytest.param('"-0.00"', '""', "0.00", None, id="negative zero with places"),
        pytest.param('"0"', '"BTC"', "0", "BTC", id="zero, coin kept"),
    ],
)
async def test_a_zero_fee_needs_no_coin_and_is_never_a_negative_zero(
    total_fee: str, coin: str | None, expected_fee: str, expected_asset: str | None
) -> None:
    """A negative zero would reach `NumericText` as `-0.000...`: equal, and not the same."""
    page = await fetch_page(one_fill_fake(feeDetail__totalFee=total_fee, feeDetail__feeCoin=coin))

    (fill,) = page.fills
    assert exact(fill.fee_amount, expected_fee)
    assert not fill.fee_amount.is_signed()
    assert fill.fee_asset == expected_asset


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('"yes"', id="yes"),
        pytest.param('"No"', id="No, capitalised"),
        pytest.param('"YES"', id="YES"),
        pytest.param('""', id="empty"),
        pytest.param("null", id="null"),
        pytest.param("false", id="a boolean"),
        pytest.param(None, id="absent"),
    ],
)
async def test_bgb_deduction_is_refused(fragment: str | None) -> None:
    """Only the exact string `no` is understood; what BGB deduction puts in the fee is not."""
    error = await refused(one_fill_fake(feeDetail__deduction=fragment))

    assert type(error) is ExchangeSchemaError
    assert "feeDetail.deduction" in str(error)


NINETEEN_PLACES: Final = "0.0000000000000000001"
EIGHTEEN_PLACES: Final = "0.000000000000000001"


@pytest.mark.parametrize("field", ["size", "priceAvg", "amount", "feeDetail.totalFee"])
async def test_an_amount_finer_than_the_fill_scale_is_refused(field: str) -> None:
    """Nineteen places would be rounded by the column: refused. Eighteen: kept exactly."""
    sign = "-" if field == "feeDetail.totalFee" else ""
    key = field.replace(".", "__")

    error = await refused(one_fill_fake(**{key: f'"{sign}{NINETEEN_PLACES}"'}))
    page = await fetch_page(one_fill_fake(**{key: f'"{sign}{EIGHTEEN_PLACES}"'}))

    assert type(error) is ExchangeSchemaError
    (fill,) = page.fills
    read = {
        "size": fill.quantity,
        "priceAvg": fill.price,
        "amount": fill.quote_quantity,
        "feeDetail.totalFee": fill.fee_amount,
    }[field]
    assert exact(read, EIGHTEEN_PLACES)


async def test_a_seconds_timestamp_fails_the_page() -> None:
    """`cTime` is described as seconds and shown as milliseconds. Seconds read as 1970."""
    error = await refused(one_fill_fake(cTime='"1695865232"'))
    page = await fetch_page(one_fill_fake(cTime='"1695865232579"'))

    assert type(error) is ExchangeSchemaError
    (fill,) = page.fills
    assert fill.executed_at == datetime(2023, 9, 28, 1, 40, 32, 579000, tzinfo=UTC)


# -- the interpreter's own limits (spec 012, "What the plan got wrong") -----------------


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def body_nested(depth: int) -> str:
    """A one-fill page whose fill carries an extra field nested `depth` arrays deep."""
    fill = VenueFill(
        trade_id=1_000_003,
        executed_ms=WINDOW_SINCE_MS + 60_000,
        overrides={"nested": nested(depth)},
    )
    return fills_body([fill])


def _decodes(body: str) -> bool:
    try:
        decode_json(body)
    except ProviderResponseError:
        return False
    return True


#: The deepest nesting worth building before concluding the interpreter has no limit.
DEEPEST_NESTING_PROBED: Final = 200_000


@cache
def scanner_limit() -> int:
    """Roughly the deepest nesting inside a whole page that `decode_json` accepts on *this* host.

    Probed by bisection, never written down: the scanner's limit is a CPython build
    constant, measured at 2998 on a Windows build and past 5000 on the Pi's Linux. A fixed
    1500 would decode everywhere today, and the day a build's limit falls below it the
    fixture would silently start testing the decoder instead of the encoder.

    **Roughly**, because the limit counts C recursion from wherever the caller already is:
    measured here, the edge moved by one level between the bisection and a call one frame
    shallower, and the provider decodes several frames deeper still, under the event loop.
    So the cases use half and twice this depth, never the edge itself, and the premise test
    checks both against the decoder directly.
    """
    shallow, deep = 1, DEEPEST_NESTING_PROBED
    assert _decodes(body_nested(shallow))
    if _decodes(body_nested(deep)):
        message = f"nothing up to {deep} deep made this interpreter's JSON scanner give up"
        raise RuntimeError(message)
    while deep - shallow > 1:
        middle = (shallow + deep) // 2
        if _decodes(body_nested(middle)):
            shallow = middle
        else:
            deep = middle
    return shallow


def past_the_integer_digit_limit() -> str:
    """A JSON integer with one digit more than this process will convert, read not assumed."""
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        message = "This interpreter has no integer string conversion limit to exceed."
        raise RuntimeError(message)
    return "1" * (limit + 1)


def test_the_interpreter_probes_find_what_they_claim() -> None:
    """The premises: half the limit decodes and is far past the encoder's bound of 32; twice
    the limit does not decode; the long integer is refused by the decoder."""
    limit = scanner_limit()

    assert limit // 2 > 10 * MAX_RAW_PAYLOAD_DEPTH
    assert _decodes(body_nested(limit // 2))
    assert not _decodes(body_nested(limit * 2))
    assert not _decodes(f'{{"n": {past_the_integer_digit_limit()}}}')


EXPONENT_PAST_THE_LIMIT: Final = "1e1000000000000000000"


def _one_fill(**overrides: str | None) -> str:
    fill = VenueFill(
        trade_id=1_000_003,
        executed_ms=WINDOW_SINCE_MS + 60_000,
        overrides={name.replace("__", "."): value for name, value in overrides.items()},
    )
    return fills_body([fill])


INTERPRETER_LIMIT_BODIES: Final[dict[str, Callable[[], str]]] = {
    "size, a 5000-digit string": lambda: _one_fill(size='"' + "1" * 5000 + '"'),
    "size, a JSON integer past the digit limit": lambda: _one_fill(
        size=past_the_integer_digit_limit()
    ),
    "cTime, a 5000-digit string": lambda: _one_fill(cTime='"' + "1" * 5000 + '"'),
    "totalFee, a 5000-digit string": lambda: _one_fill(
        feeDetail__totalFee='"-0.' + "1" * 5000 + '"'
    ),
    "a fill nested half as deep as the decoder follows": lambda: body_nested(scanner_limit() // 2),
    "a fill nested twice as deep as the decoder follows": lambda: body_nested(scanner_limit() * 2),
    "size, an exponent Decimal cannot hold, as a number": lambda: _one_fill(
        size=EXPONENT_PAST_THE_LIMIT
    ),
    "priceAvg, an exponent Decimal cannot hold, as a string": lambda: _one_fill(
        priceAvg=f'"{EXPONENT_PAST_THE_LIMIT}"'
    ),
    "symbol, a lone surrogate": lambda: _one_fill(symbol='"\\ud800"'),
    "tradeId, a lone surrogate": lambda: _one_fill(tradeId='"\\ud800"'),
    "orderId, a lone surrogate": lambda: _one_fill(orderId='"\\ud800"'),
    "feeCoin, a lone surrogate": lambda: _one_fill(feeDetail__feeCoin='"\\ud800"'),
    "side, a lone surrogate": lambda: _one_fill(side='"\\ud800"'),
}


@pytest.mark.parametrize("case", sorted(INTERPRETER_LIMIT_BODIES))
async def test_the_interpreter_limits_are_schema_errors(case: str) -> None:
    """Integer digits, recursion depth, `Decimal`'s exponent and UTF-8: each a schema error.

    The four limits spec 012 found escaping the taxonomy, one review at a time. Each is a
    value the venue chooses, so each must arrive as the one class that says "the venue sent
    something we cannot read".
    """
    fake = FakeBitget(fill_replies=[Reply(body=INTERPRETER_LIMIT_BODIES[case]())])

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError


REQUIRED_FIELDS: Final = (
    "tradeId",
    "symbol",
    "side",
    "priceAvg",
    "size",
    "cTime",
    "feeDetail",
    "feeDetail.totalFee",
    "feeDetail.feeCoin",
    "feeDetail.deduction",
)

#: A value of the wrong JSON type that carries a sentinel, so a message quoting the value
#: would be caught. An object is the wrong type for every field but `feeDetail`, which gets
#: a string.
MISTYPED_SENTINEL: Final = "MISTYPED-VALUE-SENTINEL-5821"


@pytest.mark.parametrize("shape", ["missing", "mistyped"])
@pytest.mark.parametrize("name", REQUIRED_FIELDS)
async def test_a_missing_or_mistyped_field_is_a_schema_error_naming_the_field(
    name: str, shape: str
) -> None:
    """Never a `KeyError`, `TypeError` or `AttributeError`; the field named, the value not."""
    if shape == "missing":
        fragment = None
    elif name == "feeDetail":
        fragment = f'"{MISTYPED_SENTINEL}"'
    else:
        fragment = f'{{"sentinel":"{MISTYPED_SENTINEL}"}}'

    error = await refused(one_fill_fake(**{name.replace(".", "__"): fragment}))

    assert type(error) is ExchangeSchemaError
    assert name in str(error)
    assert MISTYPED_SENTINEL not in f"{error}{error!r}{error.args}"


@pytest.mark.parametrize("name", ["orderId", "amount"])
async def test_an_optional_field_of_the_wrong_type_is_a_schema_error_naming_it(name: str) -> None:
    error = await refused(one_fill_fake(**{name: f'{{"sentinel":"{MISTYPED_SENTINEL}"}}'}))

    assert type(error) is ExchangeSchemaError
    assert name in str(error)
    assert MISTYPED_SENTINEL not in f"{error}{error!r}{error.args}"


async def test_an_absent_order_id_is_none() -> None:
    """The companion: `orderId` is optional, and its absence is not a refusal."""
    page = await fetch_page(one_fill_fake(orderId=None))

    (fill,) = page.fills
    assert fill.external_order_id is None


@pytest.mark.parametrize("fragment", ['"BUY"', '"hold"', '""', '"sell "'])
async def test_a_side_other_than_buy_or_sell_is_refused(fragment: str) -> None:
    error = await refused(one_fill_fake(side=fragment))

    assert type(error) is ExchangeSchemaError
    assert "side" in str(error)


async def test_a_sell_is_a_sell() -> None:
    page = await fetch_page(one_fill_fake(side='"sell"'))

    (fill,) = page.fills
    assert fill.side is FillSide.SELL


# -- the symbol cache ---------------------------------------------------------------------


async def test_the_symbol_is_split_by_the_venue_and_asked_once() -> None:
    """`AB12CD` splits where no list of quote coins would: the fake says `AB1` and `2CD`."""
    fake = FakeBitget(spread_fills(150, symbols=("AB12CD",)))

    pages = await walk(fake)

    assert len(fake.fill_requests) == 2
    assert len(fake.symbol_requests) == 1
    (request,) = fake.symbol_requests
    assert request.url.raw_path.decode("ascii") == "/api/v2/spot/public/symbols?symbol=AB12CD"
    fills = [fill for page in pages for fill in page.fills]
    assert len(fills) == 150
    assert {(fill.symbol, fill.base_asset, fill.quote_asset) for fill in fills} == {
        ("AB12CD", "AB1", "2CD")
    }


async def test_each_distinct_symbol_is_asked_about_once() -> None:
    fake = FakeBitget(spread_fills(250, symbols=("BTCUSDT", "KASUSDT")))

    pages = await walk(fake)

    asked = sorted(request.url.params["symbol"] for request in fake.symbol_requests)
    assert asked == ["BTCUSDT", "KASUSDT"]
    pairs = {
        (fill.symbol, fill.base_asset, fill.quote_asset) for page in pages for fill in page.fills
    }
    assert pairs == {("BTCUSDT", "BTC", "USDT"), ("KASUSDT", "KAS", "USDT")}


def _entry_without(field: str) -> str:
    return symbol_entry("BTCUSDT", "BTC", "USDT").replace(f'"{field}":', '"ignored":', 1)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(symbols_body(symbol_entry("ETHUSDT", "ETH", "USDT")), id="another symbol"),
        pytest.param(symbols_body(), id="empty data"),
        pytest.param(
            symbols_body(
                symbol_entry("BTCUSDT", "BTC", "USDT"), symbol_entry("BTCUSDT", "BTC", "USDT")
            ),
            id="two entries",
        ),
        pytest.param(
            symbols_body(
                symbol_entry("BTCUSDT", "BTC", "USDT"), symbol_entry("ETHUSDT", "ETH", "USDT")
            ),
            id="the right entry and another",
        ),
        pytest.param(symbols_body(symbol_entry("BTCUSDT", "", "USDT")), id="blank baseCoin"),
        pytest.param(symbols_body(symbol_entry("BTCUSDT", "BTC", "   ")), id="blank quoteCoin"),
        pytest.param(symbols_body(_entry_without("baseCoin")), id="no baseCoin"),
        pytest.param(symbols_body(_entry_without("quoteCoin")), id="no quoteCoin"),
        pytest.param(symbols_body(_entry_without("symbol")), id="no symbol"),
        pytest.param(symbols_body(symbol_entry("btcusdt", "BTC", "USDT")), id="lower case"),
        pytest.param(
            '{"code":"00000","msg":"success","data":'
            + symbol_entry("BTCUSDT", "BTC", "USDT")
            + "}",
            id="data an object",
        ),
        pytest.param('{"code":"00000","msg":"success","data":null}', id="data null"),
        pytest.param("not json", id="not JSON"),
    ],
)
async def test_a_symbol_answer_about_another_symbol_is_refused(body: str) -> None:
    """The `align_balances` rule: an answer about another symbol is refused, not used."""
    fake = FakeBitget(spread_fills(1), symbol_replies=[Reply(body=body)])

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError
    assert len(fake.symbol_requests) == 1


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (Reply(status=500, body=HTML_BODY), ExchangeUnavailableError),
        (Reply(status=429, body=error_body("429")), ExchangeRateLimitedError),
        (Reply(status=400, body=error_body("40102")), ExchangeInvalidRequestError),
        (Reply(status=403, body=HTML_BODY), ExchangeAuthError),
        (Reply(error=httpx.ReadTimeout("the fake venue timed out")), ExchangeUnavailableError),
    ],
    ids=["500", "429", "unknown symbol", "403", "transport"],
)
async def test_a_refused_symbol_lookup_is_classified_like_the_fills_call(
    reply: Reply, expected: type[ExchangeError]
) -> None:
    fake = FakeBitget(spread_fills(1), symbol_replies=[reply])

    error = await refused(fake)

    assert type(error) is expected


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('"BTC/USDT"', id="a slash"),
        pytest.param('"btcusdt"', id="lower case"),
        pytest.param('"\\ud800"', id="a lone surrogate"),
        pytest.param('""', id="empty"),
        pytest.param('"BTC USDT"', id="a space"),
        pytest.param('"BTCUSDT&symbol=ETHUSDT"', id="a query"),
        pytest.param('"BTC%2FUSDT"', id="percent-encoded"),
        pytest.param('"' + "A" * 41 + '"', id="41 characters"),
        pytest.param("12345", id="a number"),
        pytest.param("null", id="null"),
    ],
)
async def test_an_unsafe_symbol_is_refused_before_a_url_is_built(fragment: str) -> None:
    """Checked for every fill of the page before any symbol request is made."""
    fills = [
        VenueFill(trade_id=1_000_006, executed_ms=WINDOW_SINCE_MS + 60_000),
        VenueFill(
            trade_id=1_000_003,
            executed_ms=WINDOW_SINCE_MS + 120_000,
            overrides={"symbol": fragment},
        ),
    ]
    fake = FakeBitget(fill_replies=[Reply(body=fills_body(fills))])

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError
    assert fake.symbol_requests == []
    assert len(fake.fill_requests) == 1, "the positive companion: the page was fetched"


async def test_a_forty_character_symbol_is_asked_about() -> None:
    """The companion: forty characters is inside the pattern, so the venue is asked."""
    symbol = "A" * 40
    fake = FakeBitget(spread_fills(1, symbols=(symbol,)), symbols={symbol: ("A" * 20, "A" * 20)})

    page = await fetch_page(fake)

    assert [request.url.params["symbol"] for request in fake.symbol_requests] == [symbol]
    assert [fill.symbol for fill in page.fills] == [symbol]


# --------------------------------------------------------------------------------------
# The window: widened by a millisecond, then filtered
# --------------------------------------------------------------------------------------

EDGE_FILLS: Final = (
    VenueFill(trade_id=1_000_001, executed_ms=WINDOW_SINCE_MS - 1),
    VenueFill(trade_id=1_000_002, executed_ms=WINDOW_SINCE_MS),
    VenueFill(trade_id=1_000_003, executed_ms=WINDOW_UNTIL_MS - 1),
    VenueFill(trade_id=1_000_004, executed_ms=WINDOW_UNTIL_MS),
)


@pytest.mark.parametrize("bounds", [Bounds.INCLUSIVE, Bounds.EXCLUSIVE], ids=str)
async def test_the_window_is_widened_by_a_millisecond_and_filtered(bounds: Bounds) -> None:
    """`[since, until)` is covered under either reading; the two edge fills are dropped.

    Inclusive: the venue serves all four and the fills at `since - 1` and at `until` belong
    to the neighbouring windows. Exclusive: the venue serves only the two inside, and
    neither is lost.
    """
    fake = FakeBitget(EDGE_FILLS, bounds=bounds)

    page = await fetch_page(fake)

    (query,) = fake.fill_queries()
    assert query["startTime"] == "1695772799999"
    assert query["endTime"] == "1695945600000"
    served = set(fake.served_trade_ids[0])
    if bounds is Bounds.INCLUSIVE:
        assert served == {1_000_001, 1_000_002, 1_000_003, 1_000_004}
    else:
        assert served == {1_000_002, 1_000_003}
    assert sorted(fill.external_trade_id for fill in page.fills) == ["1000002", "1000003"]


@pytest.mark.parametrize(
    "executed_ms",
    [WINDOW_SINCE_MS - 2, WINDOW_UNTIL_MS + 1],
    ids=["since - 2 ms", "until + 1 ms"],
)
async def test_a_fill_outside_the_widened_window_is_refused(executed_ms: int) -> None:
    """Only the two edge milliseconds are dropped; anything else is an answer nobody asked."""
    fake = FakeBitget(
        [VenueFill(trade_id=1_000_001, executed_ms=executed_ms)], bounds=Bounds.IGNORED
    )

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError


async def test_the_page_size_is_checked_before_the_edge_is_dropped() -> None:
    """101 raw fills, one on the edge: refused, not trimmed to 100 and accepted.

    The companion: 100 raw fills with one on the edge is a full page of 99, and its cursor is
    the smallest id on the *raw* page -- the dropped edge fill's.
    """
    edge = VenueFill(trade_id=1_000_000, executed_ms=WINDOW_SINCE_MS - 1)
    over = FakeBitget(fill_replies=[Reply(body=fills_body([*spread_fills(100), edge]))])
    full = FakeBitget(fill_replies=[Reply(body=fills_body([*spread_fills(99), edge]))])

    error = await refused(over)
    page = await fetch_page(full)

    assert type(error) is ExchangeSchemaError
    assert over.symbol_requests == [], "refused on the raw count, before anything else"
    assert len(page.fills) == 99
    assert page.next_cursor == "1000000"


async def test_a_window_at_the_epoch_sends_zero() -> None:
    """`since` at the epoch cannot be widened below it: `startTime=0`, not `-1`."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    fake = FakeBitget()

    await fetch_page(fake, FillWindow(since=epoch, until=epoch + timedelta(days=1)))

    (query,) = fake.fill_queries()
    assert query["startTime"] == "0"
    assert query["endTime"] == "86400000"


async def test_a_caller_mistake_costs_no_request() -> None:
    """A symbol for a venue that needs none, or a window over 30 days: refused, nothing sent."""
    fake = FakeBitget()
    since = datetime(2023, 9, 1, tzinfo=UTC)

    async with bitget_client(fake) as client:
        provider = bitget_provider(client)
        with pytest.raises(ValueError, match="symbol"):
            await provider.fetch_fill_page(WINDOW, cursor=None, symbol="BTCUSDT")
        with pytest.raises(ValueError, match="window"):
            await provider.fetch_fill_page(
                FillWindow(since=since, until=since + timedelta(days=31)), cursor=None, symbol=None
            )
        assert fake.requests == []

        # The companion: exactly thirty days is sent.
        await provider.fetch_fill_page(
            FillWindow(since=since, until=since + timedelta(days=30)), cursor=None, symbol=None
        )
    assert len(fake.fill_requests) == 1


# --------------------------------------------------------------------------------------
# Criterion 6: the retention clamp surfaces an `effective_since`
# --------------------------------------------------------------------------------------

#: 2023-10-01T00:00:00Z. Ninety days before it is 2023-07-03T00:00:00Z
#: (`date -u -d 2023-07-03T00:00:00Z +%s` -> 1688342400), five minutes later 00:05:00Z
#: (-> 1688342700).
RETENTION_NOW: Final = datetime(2023, 10, 1, tzinfo=UTC)


async def test_a_request_older_than_retention_is_clamped_and_the_clamped_window_is_accepted() -> (
    None
):
    clamp = clamp_to_retention(
        RETENTION_NOW - timedelta(days=200), now=RETENTION_NOW, capabilities=BITGET_CAPABILITIES
    )

    assert clamp.clamped
    assert clamp.effective_since == datetime(2023, 7, 3, 0, 5, tzinfo=UTC)

    fake = FakeBitget()
    window = FillWindow(
        since=clamp.effective_since, until=clamp.effective_since + timedelta(days=1)
    )
    page = await fetch_page(fake, window, clock=FixedClock(RETENTION_NOW))

    assert page.fills == ()
    (query,) = fake.fill_queries()
    assert query["startTime"] == "1688342699999"
    assert int(query["startTime"]) >= 1688342400000, "no earlier than now - 90 days"


@pytest.mark.parametrize("status", [400, 200])
async def test_a_retention_refusal_is_typed(status: int) -> None:
    error = await refused(FakeBitget(fill_replies=[Reply(status=status, body=error_body("40704"))]))

    assert type(error) is ExchangeRetentionWindowError


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
            "-1",
            "1e400",
            "1.5",
            "[]",
            "{}",
            '""',
            '"0"',
            '"-0"',
            '"no"',
            '"buy"',
            '"sell"',
            '"BTCUSDT"',
            '"1695865232579"',
            '"-0.0000007"',
            '"\\ud800"',
            '"9223372036854775808"',
            EXPONENT_PAST_THE_LIMIT,
        ]
    ),
    st.integers().map(str),
    QUOTABLE_TEXT.map(lambda text: f'"{text}"'),
)

FIELD_NAMES: Final = st.sampled_from(
    [
        "tradeId",
        "orderId",
        "symbol",
        "side",
        "priceAvg",
        "size",
        "amount",
        "cTime",
        "feeDetail",
        "feeDetail.totalFee",
        "feeDetail.feeCoin",
        "feeDetail.deduction",
        "extra",
    ]
)


def outcome_of(call: Callable[[], object]) -> object:
    """What `call` returned, or the exchange error it raised. Anything else escapes."""
    try:
        return call()
    except ExchangeError as error:
        return error


def _parse_like_the_provider(status: int, body: str) -> FillPage | None:
    """Every step of `fetch_fill_page` after the response arrives, synchronously."""
    data = unwrap_envelope(status, body)
    symbols = {
        symbol: SymbolAssets(base_asset="BTC", quote_asset="USDT") for symbol in fill_symbols(data)
    }
    return parse_fills_page(data, window=WINDOW, cursor=None, symbols=symbols)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    edits=st.lists(st.tuples(FIELD_NAMES, st.one_of(st.none(), FRAGMENTS)), max_size=4),
    status=st.sampled_from([200, 200, 200, 400, 401, 403, 429, 500, 503, 302, 418]),
)
def test_whatever_the_venue_sends_only_the_seven_classes_escape(
    edits: list[tuple[str, str | None]], status: int
) -> None:
    """The documented example fill with up to four fields replaced by anything at all.

    Either a page comes back, or one of the seven classes is raised: never a `KeyError`, a
    `TypeError`, a `ValueError` from the interpreter or a `ProviderResponseError` from the
    decoder.
    """
    fill = VenueFill(
        trade_id=12345678910,
        executed_ms=1695865232579,
        order_id=12345678910,
        overrides=dict(edits),
    )
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


def test_the_example_itself_parses_through_the_pure_steps() -> None:
    """The control for the two properties above: with no edit, a page comes back."""
    page = _parse_like_the_provider(200, DOCUMENTED_EXAMPLE_BODY)

    assert page is not None
    assert [fill.external_trade_id for fill in page.fills] == ["12345678910"]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param(
            Reply(body=fills_body(spread_fills(2)), headers={"ratelimit-remaining": "1" * 5000}),
            None,
            id="a success with a 5000-digit ratelimit-remaining",
        ),
        pytest.param(
            Reply(status=429, body=error_body("429"), headers={"Retry-After": "1" * 5000}),
            ExchangeRateLimitedError,
            id="a 429 with a 5000-digit Retry-After",
        ),
        pytest.param(
            Reply(status=503, body=HTML_BODY, headers={"x-ratelimit-reset": "9" * 5000}),
            ExchangeUnavailableError,
            id="a 503 with a 5000-digit x-ratelimit-reset",
        ),
        pytest.param(
            Reply(
                status=429,
                body=error_body("429"),
                headers={"Retry-After": "Sun, 06 Nov 99999999999999999999 08:49:37 GMT"},
            ),
            ExchangeRateLimitedError,
            id="a 429 with a Retry-After date past any year",
        ),
    ],
)
async def test_a_hostile_header_on_a_bitget_answer_ends_in_one_of_the_seven(
    reply: Reply, expected: type[ExchangeError] | None
) -> None:
    """A header the transport reads on every response never escapes as a bare `ValueError`."""
    fake = FakeBitget(fill_replies=[reply])

    if expected is None:
        page = await fetch_page(fake)
        assert len(page.fills) == 2
        return
    error = await refused(fake)
    assert type(error) is expected
    if isinstance(error, ExchangeRateLimitedError):
        assert error.retry_after_ms is None, "an unusable Retry-After says nothing"


# --------------------------------------------------------------------------------------
# Credentials the transport cannot send
# --------------------------------------------------------------------------------------

#: Printable ASCII, no surrounding whitespace, interior spaces allowed: the rule for the key
#: and the passphrase, which travel in headers. One refused value per character class.
UNSENDABLE: Final = {
    "a newline": "synthetic\nkey-value",
    "a carriage return": "synthetic\rkey-value",
    "a NUL": "synthetic\x00key-value",
    "a tab": "synthetic\tkey-value",
    "a DEL": "synthetic\x7fkey-value",
    "a leading space": " synthetic-key-value",
    "a trailing space": "synthetic-key-value ",
    "a non-ASCII letter": "synthetic-k" + E_ACUTE + "y-value",
}


@pytest.mark.parametrize("field", ["api_key", "passphrase"])
@pytest.mark.parametrize("why", sorted(UNSENDABLE))
def test_the_provider_refuses_a_credential_no_header_can_carry(field: str, why: str) -> None:
    """h11 would refuse it with a message quoting the whole value; refused first, here."""
    value = UNSENDABLE[why]
    if field == "api_key":
        credentials = synthetic_credentials(api_key=value)
    else:
        credentials = synthetic_credentials(passphrase=value)

    with pytest.raises(ValueError, match=field) as caught:
        bitget_provider(httpx.AsyncClient(), credentials=credentials)

    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert value not in rendered
    assert "key-value" not in rendered


@pytest.mark.parametrize("field", ["api_key", "passphrase"])
async def test_an_interior_space_is_a_credential_a_header_can_carry(field: str) -> None:
    """The companion: a space inside is legal in a header value, and is signed and sent."""
    value = "synthetic key with interior spaces"
    if field == "api_key":
        fake = FakeBitget(spread_fills(1), access_key=value)
        credentials = synthetic_credentials(api_key=value)
    else:
        fake = FakeBitget(spread_fills(1), passphrase=value)
        credentials = synthetic_credentials(passphrase=value)

    async with bitget_client(fake) as client:
        provider = bitget_provider(client, credentials=credentials)
        page = await provider.fetch_fill_page(WINDOW, cursor=None, symbol=None)

    assert len(page.fills) == 1
    assert fake.signature_failures == []


async def test_the_secret_is_not_held_to_the_header_rule() -> None:
    """The secret is HMAC input, never a header, so non-ASCII and spaces are its business."""
    signing_key = " synthetic s" + E_ACUTE + "cret with spaces "
    fake = FakeBitget(spread_fills(1), signing_key=signing_key)

    async with bitget_client(fake) as client:
        provider = bitget_provider(
            client, credentials=synthetic_credentials(api_secret=signing_key)
        )
        page = await provider.fetch_fill_page(WINDOW, cursor=None, symbol=None)

    assert len(page.fills) == 1
    assert fake.signature_failures == []
