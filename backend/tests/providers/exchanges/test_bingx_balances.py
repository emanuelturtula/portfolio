"""Criteria 1 and 2 of #104 (spec 025), for BingX: the spot balance read, against a fake venue.

`BingXProvider.fetch_balances` reads `GET /openApi/spot/v1/account/balance`, signed as a
fills request is, and `bingx.parse_balances` turns `data.balances` into what the seam
promises: one `AssetBalance` per asset, the total `free + locked`, zeros left out, sorted.
`tests/providers/exchanges/test_balances_seam.py` tests `AssetBalance` and
`assemble_balances` on their own; this module shows that BingX's answer gets there, and what
is BingX's own on the way.

**Every expected value comes from outside the code under test.** The path, the query and the
label are literals. The signatures were computed once with `openssl`, the command beside
each, and are recomputed here with `hmac` and `hashlib` directly. Every total, and every
decoded amount, was worked out by hand and is written as a literal; the two property tests
at the end use integer arithmetic as their oracle. The fake venue (`bingx_harness.FakeBingX`)
verifies every balance request with the standard library, exactly as it verifies a fills
request, so a signing mistake fails every test that makes a request.

**The parsing rules are each asserted twice**, through the `read` fixture: on
`parse_balances` alone, handed the array as the shared decoder produces it, and through
`fetch_balances` over a signed request to the fake. A rule that held in the pure function
and was lost on the way to the provider's method would fail the second.

**Ruling R1 (spec 025).** BingX formats `free` and `locked` from doubles, so each is decoded
with `from_binary_float` -- fifteen significant digits, half to even -- before the sign is
checked and the two are added. Nothing is rounded to the column: a part still finer than
eighteen places after the decode is refused.

**No message carries a value.** Every refusal here is raised over an answer that carries the
marker asset `ZZMARK` or the marker amount `4242.4242`, and `refusal` and
`assert_carries_no_value` search the exception's text, repr and args, and those of every
exception chained to it, for either.

The bodies are hand-written text, never `json.dumps` of a Python number. Escapes that would
put a non-ASCII or unencodable character in this file are built with `json_escape`, so the
source stays ASCII and the body does too.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import inspect
import re
import sys
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import TYPE_CHECKING, Final

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from portfolio.providers.base import decode_json
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.exchanges import bingx
from portfolio.providers.exchanges.base import AssetBalance
from portfolio.providers.exchanges.bingx import (
    BALANCES_MEMBER,
    BALANCES_PATH,
    BINGX_API_URL,
    FILLS_MEMBER,
    MAX_BASE_LENGTH,
    BingXProvider,
    build_balances_query,
    parse_balances,
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
from portfolio.providers.http import ENDPOINT_LABELS, EXCHANGE_BALANCES, request_target
from tests.providers.exchanges.bingx_harness import (
    ACCESS_KEY_SENTINEL,
    DOCUMENTED_BALANCES_PATH,
    FUND_ACCOUNT_BALANCES_PATH,
    GOLDEN_TIMESTAMP_MS,
    HTML_BODY,
    SIGNING_SENTINEL,
    FakeBingX,
    FixedClock,
    Reply,
    TickingClock,
    VenueBalance,
    VenueFill,
    balances_body,
    balances_fragment,
    bingx_client,
    bingx_provider,
    envelope,
    error_body,
    fetch_balances,
    fills_body,
    synthetic_credentials,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence

#: The seven classes the provider may raise, and nothing else. `ExchangeError` itself is
#: the marker base and is never raised.
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

#: Distinctive and synthetic, so their absence from a message means something.
MARK_ASSET: Final = "ZZMARK"
MARK_AMOUNT: Final = "4242.4242"
MARK_DIGITS: Final = "4242"

#: What a careless venue would echo in a refusal's `msg`: holdings the provider must drop.
ECHO: Final = f"{MARK_ASSET} holds {MARK_AMOUNT}"

type Entry = VenueBalance | str
type Reader = Callable[[Sequence[Entry]], Awaitable[tuple[AssetBalance, ...]]]


def json_escape(code_unit: int) -> str:
    """The six-character JSON escape of one UTF-16 code unit: a backslash, `u`, four hex digits.

    Built here rather than typed, so that this file holds no escape an editor could turn
    into the character itself, and the body that carries it stays ASCII.
    """
    return "\\" + f"u{code_unit:04x}"


def held(asset: str = "BTC", free: str = "0", locked: str = "0", **extra: str) -> VenueBalance:
    """One documented entry: `asset`, `free` and `locked` as JSON strings, as documented.

    `extra` appends fields the venue does not document, each a raw JSON fragment.
    """
    return VenueBalance(asset=asset, free=free, locked=locked, overrides=extra)


def raw(**fragments: str | None) -> VenueBalance:
    """An entry whose fields are raw JSON fragments: `raw(free="1.5")` is the bare number,
    `raw(free='"1.5"')` the string, `raw(free=None)` an entry without the field.

    What is not given is `KAS` holding nothing, so the entry is valid but for the fragments.
    """
    return VenueBalance(asset="KAS", free="0", locked="0", overrides=fragments)


def marked(**fragments: str | None) -> VenueBalance:
    """The marker entry -- `ZZMARK`, `4242.4242` free and locked -- with raw fragments put
    in place of the fields named.

    On its own it is a valid entry (`test_the_marker_entry_is_itself_a_valid_balance`), so a
    refusal of `marked(free="true")` is a refusal of that one field.
    """
    return VenueBalance(asset=MARK_ASSET, free=MARK_AMOUNT, locked=MARK_AMOUNT, overrides=fragments)


def links(error: BaseException) -> Iterator[BaseException]:
    """Every exception reachable from `error` by `__cause__` or `__context__`, `error` first.

    The suppressed ones too: `raise ... from None` hides a context from a traceback and
    leaves it where a debugger or an error tracker walks.
    """
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


def rendered(error: BaseException) -> str:
    """Everything of an exception that a log line, a traceback or a tracker could print."""
    return "|".join(f"{link}|{link!r}|{link.args!r}" for link in links(error))


def assert_carries_no_value(error: BaseException) -> None:
    """Neither the marker asset nor the marker amount, anywhere in the exception's chain."""
    text = rendered(error)
    assert MARK_ASSET not in text, text
    assert MARK_DIGITS not in text, text


def names_field(error: BaseException, name: str) -> bool:
    """Whether `error`'s message names `name` as a word: `free` must not match `freedom`."""
    return re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", str(error)) is not None


def pairs(balances: Sequence[AssetBalance]) -> list[tuple[str, Decimal]]:
    return [(entry.asset, entry.quantity) for entry in balances]


def checked(result: object) -> tuple[AssetBalance, ...]:
    """What every successful read is, whatever it holds: a tuple of balances in `Decimal`."""
    assert type(result) is tuple
    for entry in result:
        assert type(entry) is AssetBalance
        assert type(entry.asset) is str
        assert type(entry.quantity) is Decimal, "an amount that is not a Decimal"
    return result


def assert_one_signed_balance_request(fake: FakeBingX) -> None:
    """The positive companion of every read: the answer was asked for once, correctly signed."""
    assert len(fake.all_requests) == 1
    assert fake.balance_verified == fake.all_requests
    assert fake.signature_failures == []


async def read_parsed(entries: Sequence[Entry]) -> tuple[AssetBalance, ...]:
    """`parse_balances` alone, over the array as the shared decoder hands it over. No HTTP."""
    return checked(parse_balances(decode_json(balances_fragment(entries))))


async def read_fetched(entries: Sequence[Entry]) -> tuple[AssetBalance, ...]:
    """`fetch_balances` end to end: one signed request to a venue answering `entries`."""
    fake = FakeBingX(balances=entries)
    try:
        result = await fetch_balances(fake)
    except ExchangeError:
        assert_one_signed_balance_request(fake)
        raise
    assert_one_signed_balance_request(fake)
    return checked(result)


@pytest.fixture(params=["parse_balances", "fetch_balances"])
def read(request: pytest.FixtureRequest) -> Reader:
    """Each parsing rule twice: on the pure parser, and through the provider's method."""
    return read_parsed if request.param == "parse_balances" else read_fetched


async def refusal(read: Reader, *entries: Entry) -> ExchangeSchemaError:
    """The refusal reading `entries` raises: exactly a schema error, carrying no value.

    Anything that is not an `ExchangeError` escapes, and fails the test.

    Beyond the markers, **no run of four digits** may appear anywhere in the refusal: a
    total is the sum of two parts and need not look like either, and a message that quoted
    it would pass a search for the parts alone. A parser's refusal names a field and a rule,
    and the longest number in any rule is three digits ("more than 100 digits").
    """
    body = balances_fragment(entries)
    assert MARK_ASSET in body or MARK_DIGITS in body, "no marker, so an absence proves nothing"
    with pytest.raises(ExchangeError) as caught:
        await read(entries)
    error = caught.value
    assert type(error) is ExchangeSchemaError
    assert_carries_no_value(error)
    assert re.search(r"[0-9]{4}", rendered(error)) is None, rendered(error)
    return error


def scripted(*replies: Reply) -> FakeBingX:
    """A venue whose balance endpoint answers `replies` in order, the last one repeating."""
    return FakeBingX(balance_replies=replies)


async def refused(fake: FakeBingX) -> ExchangeError:
    """The exchange error one balance read raises. Anything else escapes."""
    with pytest.raises(ExchangeError) as caught:
        await fetch_balances(fake)
    return caught.value


# --------------------------------------------------------------------------------------
# The documented constants
# --------------------------------------------------------------------------------------


def test_the_balance_constants_are_the_documented_ones() -> None:
    """Pinned as literals, once: the spot endpoint, the member, the label, the name bound.

    V3, "Query Assets" (spec 025, read 2026-10-01). Not the fund-account endpoint, whose
    path differs in one segment and which is deliberately not read.
    """
    assert BINGX_API_URL == "https://open-api.bingx.com"
    assert BALANCES_PATH == "/openApi/spot/v1/account/balance"
    assert BALANCES_MEMBER == "balances"
    assert FILLS_MEMBER == "fills"
    assert EXCHANGE_BALANCES == "exchange_balances"
    assert "exchange_balances" in ENDPOINT_LABELS
    assert MAX_BASE_LENGTH == 40
    for name in (
        "BALANCES_MEMBER",
        "BALANCES_PATH",
        "FILLS_MEMBER",
        "build_balances_query",
        "parse_balances",
    ):
        assert name in bingx.__all__


def test_the_fake_venue_routes_the_documented_paths_from_its_own_literals() -> None:
    """The fake never reads a path off the provider, or it would route whatever was chosen."""
    assert DOCUMENTED_BALANCES_PATH == "/openApi/spot/v1/account/balance"
    assert FUND_ACCOUNT_BALANCES_PATH == "/openApi/fund/v1/account/balance"


def test_fetch_balances_is_a_coroutine_taking_no_argument() -> None:
    """No window, no cursor, no symbol and no asset: a balance is a reading of the present."""
    assert inspect.iscoroutinefunction(BingXProvider.fetch_balances)
    assert list(inspect.signature(BingXProvider.fetch_balances).parameters) == ["self"]


# --------------------------------------------------------------------------------------
# Criterion 1: the request
# --------------------------------------------------------------------------------------

#: The query the provider builds at `GOLDEN_NOW`, 1684814440729 ms, without its signature.
BALANCES_VECTOR_QUERY: Final = "timestamp=1684814440729"
BALANCES_VECTOR_SIGNATURE: Final = (
    "89bb3f2fd36439a8fb61c453e299a931215c7e7b952d187380cd1742e1345c2d"
)
r"""Computed outside this code, with OpenSSL 3.5.4, which printed exactly the literal above:

    printf '%s' 'timestamp=1684814440729' | openssl dgst -sha256 -hmac 'SECRET_KEY' -hex
"""

#: 2023-09-27T00:00:00.500999Z. `date -u -d @1695772800` is 2023-09-27T00:00:00Z, so the
#: instant is 1695772800500 ms and 999 microseconds, floored to the millisecond.
SECOND_VECTOR_NOW: Final = datetime(2023, 9, 27, 0, 0, 0, 500999, tzinfo=UTC)
SECOND_VECTOR_QUERY: Final = "timestamp=1695772800500"
SECOND_VECTOR_SIGNATURE: Final = "5fd799386164361794b41ff7a2b3887f87e8a703256df77d4a6334a4249dc5a6"
r"""Computed outside this code, with OpenSSL 3.5.4, which printed exactly the literal above:

    printf '%s' 'timestamp=1695772800500' | openssl dgst -sha256 -hmac 'SECRET_KEY' -hex
"""

#: Credentials no other value in a request could be mistaken for. Letters only, so the key
#: is header-safe; neither is assigned to a name carrying the venue's.
DISTINCT_KEY: Final = "QxJfV" * 8
DISTINCT_SECRET: Final = "LmTgW" * 8


def stdlib_signature(secret: str, signed: str) -> str:
    """HMAC-SHA256, lower-case hex, with the standard library and nothing of the provider's."""
    return hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).hexdigest()


def test_the_golden_signature_of_the_balance_query() -> None:
    """A fixed synthetic secret and timestamp, and the signature `openssl` printed for them.

    Three independent routes to one literal: the standard library here, the fake's own
    verifier (which every other test trusts to say whether a request verifies), and the
    shared signer over the query the provider's builder writes.
    """
    assert SIGNING_SENTINEL == "SECRET_KEY"
    assert GOLDEN_TIMESTAMP_MS == 1684814440729
    assert stdlib_signature("SECRET_KEY", "timestamp=1684814440729") == BALANCES_VECTOR_SIGNATURE
    assert FakeBingX().expected_signature(BALANCES_VECTOR_QUERY) == BALANCES_VECTOR_SIGNATURE
    assert build_balances_query(GOLDEN_TIMESTAMP_MS) == BALANCES_VECTOR_QUERY
    assert (
        hmac_sha256_hex(SecretStr(SIGNING_SENTINEL), build_balances_query(GOLDEN_TIMESTAMP_MS))
        == BALANCES_VECTOR_SIGNATURE
    )
    assert stdlib_signature("SECRET_KEY", SECOND_VECTOR_QUERY) == SECOND_VECTOR_SIGNATURE


async def test_the_balance_read_is_one_signed_get_to_the_spot_endpoint() -> None:
    """Exactly one request, to the documented spot path, with `timestamp` and `signature` only.

    The URL is pinned whole, as a literal: no `recvWindow`, no asset, no page, no cursor.
    No fills request is made, and the fund-account endpoint is not asked.
    """
    fake = FakeBingX(balances=[held("KAS", "1500", "0.25")])

    result = await fetch_balances(fake)

    assert pairs(result) == [("KAS", Decimal("1500.25"))]
    (request,) = fake.all_requests
    assert request.method == "GET"
    assert str(request.url) == (
        "https://open-api.bingx.com/openApi/spot/v1/account/balance"
        "?timestamp=1684814440729"
        "&signature=89bb3f2fd36439a8fb61c453e299a931215c7e7b952d187380cd1742e1345c2d"
    )
    assert request.url.path == "/openApi/spot/v1/account/balance"
    assert [name for name, _value in request.url.params.multi_items()] == [
        "timestamp",
        "signature",
    ]
    assert request.content == b""
    assert fake.balance_requests == [request]
    assert fake.balance_verified == [request]
    assert fake.signature_failures == []
    assert fake.requests == [], "a fills request was made"
    assert fake.served == []


async def test_the_query_sent_is_the_query_signed_with_the_signature_appended_last() -> None:
    """HMAC-SHA256 of exactly `timestamp=<ms>`, as 64 lower-case hex, placed last.

    The signed string is taken from the bytes the request carried, and the signature is
    recomputed with `hmac` and `hashlib` alone.
    """
    fake = FakeBingX()

    await fetch_balances(fake)

    (query,) = fake.balance_queries()
    signed, marker, signature = query.rpartition("&signature=")
    assert marker, "the signature is not the last parameter"
    assert signed == "timestamp=1684814440729"
    assert re.fullmatch(r"[0-9a-f]{64}", signature) is not None
    assert signature == stdlib_signature(SIGNING_SENTINEL, signed)
    assert signature == BALANCES_VECTOR_SIGNATURE
    assert query == f"{signed}&signature={signature}"
    assert query.count("signature") == 1
    assert signed == build_balances_query(GOLDEN_TIMESTAMP_MS)


async def test_the_key_travels_in_its_header_and_the_secret_nowhere() -> None:
    """`X-BX-APIKEY` carries the key; the URL carries neither credential; no header the rest."""
    fake = FakeBingX(signing_key=DISTINCT_SECRET, access_key=DISTINCT_KEY)
    credentials = synthetic_credentials(api_key=DISTINCT_KEY, api_secret=DISTINCT_SECRET)

    await fetch_balances(fake, credentials=credentials)

    (request,) = fake.all_requests
    assert fake.balance_verified == [request], "the premise: signed with the distinct secret"
    assert request.headers["X-BX-APIKEY"] == DISTINCT_KEY
    assert DISTINCT_KEY not in str(request.url)
    assert DISTINCT_SECRET not in str(request.url)
    signature = fake.balance_queries()[0].rpartition("&signature=")[2]
    assert signature == stdlib_signature(DISTINCT_SECRET, "timestamp=1684814440729")
    credential_like = [
        name
        for name in request.headers
        if any(word in name.lower() for word in ("key", "sign", "secret", "pass", "token"))
    ]
    assert credential_like == ["x-bx-apikey"]
    for name, value in request.headers.items():
        assert DISTINCT_SECRET not in value, f"the secret travels in {name}"
        assert signature not in value, f"the signature travels in {name}"
    assert request.content == b""


async def test_the_timestamp_is_the_clocks_instant_in_whole_milliseconds() -> None:
    """Read from the injected clock, once, and floored: a second instant, a second vector."""
    fake = FakeBingX()
    clock = FixedClock(SECOND_VECTOR_NOW)

    await fetch_balances(fake, clock=clock)

    assert fake.balance_queries() == [
        "timestamp=1695772800500"
        "&signature=5fd799386164361794b41ff7a2b3887f87e8a703256df77d4a6334a4249dc5a6"
    ]
    assert fake.signature_failures == []
    assert clock.reads == 1


@pytest.mark.parametrize(
    "moment",
    [
        pytest.param(datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC), id="before the epoch"),
        pytest.param(datetime(2023, 9, 27), id="naive"),  # noqa: DTZ001 - the mistake itself
    ],
)
async def test_a_clock_that_cannot_stamp_a_request_costs_none(moment: datetime) -> None:
    """A provider built with a broken clock: a `ValueError`, not a venue's failure, and no
    signed request is sent with a timestamp that is not one."""
    fake = FakeBingX()

    with pytest.raises(ValueError) as caught:  # noqa: PT011 - the class is the contract here
        await fetch_balances(fake, clock=FixedClock(moment))

    assert not isinstance(caught.value, ExchangeError)
    assert fake.all_requests == []


async def test_the_balance_request_is_labelled_for_the_log() -> None:
    """The label is the only thing about the request's target the transport logs.

    That matters most here: the signature travels in the query string.
    """
    fake = FakeBingX()

    await fetch_balances(fake)

    (request,) = fake.all_requests
    assert request.extensions.get("endpoint") == "exchange_balances"
    assert request_target(request) == "https://open-api.bingx.com/exchange_balances"


@pytest.mark.parametrize("count", [499, 500, 501, 1200])
async def test_one_request_answers_the_whole_account_whatever_its_size(count: int) -> None:
    """The endpoint takes no page and documents none: no cursor, no limit, no second request.

    500 is where a fills page is full and a second request follows; nothing follows here,
    and more entries than a fills page may hold are all read.
    """
    fake = FakeBingX(balances=[held(f"A{index:04d}", "1.5") for index in range(count)])

    result = await fetch_balances(fake)

    assert len(result) == count
    assert [entry.asset for entry in result] == [f"A{index:04d}" for index in range(count)]
    assert {entry.quantity for entry in result} == {Decimal("1.5")}
    assert fake.balance_queries() == [
        f"timestamp=1684814440729&signature={BALANCES_VECTOR_SIGNATURE}"
    ]
    assert len(fake.all_requests) == 1


async def test_two_reads_are_two_signed_requests_and_nothing_is_remembered() -> None:
    """No cache and no state between calls: the second read is asked, signed anew, and is
    what the venue answers then."""
    fake = FakeBingX(balances=[held("KAS", "1500")])

    async with bingx_client(fake) as client:
        provider = bingx_provider(client, clock=TickingClock())
        first = await provider.fetch_balances()
        fake.balances = (held("KAS", "1499.5"), held("BTC", "0.25"))
        second = await provider.fetch_balances()

    assert pairs(first) == [("KAS", Decimal(1500))]
    assert pairs(second) == [("BTC", Decimal("0.25")), ("KAS", Decimal("1499.5"))]
    # `GOLDEN_NOW` plus one second, then plus two: 1684814440729 + 1000 and + 2000.
    assert [query.partition("&")[0] for query in fake.balance_queries()] == [
        "timestamp=1684814441729",
        "timestamp=1684814442729",
    ]
    assert fake.balance_verified == fake.balance_requests == fake.all_requests
    assert fake.requests == []


async def test_the_fund_account_endpoint_is_never_asked() -> None:
    """Only the spot account is read: reading both could count the same units twice."""
    fake = FakeBingX(balances=[held("KAS", "1500")])

    await fetch_balances(fake)

    assert [request.url.path for request in fake.all_requests] == [
        "/openApi/spot/v1/account/balance"
    ]
    assert all("fund" not in str(request.url) for request in fake.all_requests)


async def test_the_fake_venue_does_not_answer_the_fund_account_endpoint() -> None:
    """The control on the test above: a request there fails the test that caused it.

    Correctly signed and keyed, so only the path is wrong. The refusal is an
    `AssertionError` out of the handler, and it comes back through the production client
    untouched; the same request to the spot path is answered.
    """
    fake = FakeBingX(balances=[held("KAS", "1500")])
    query = f"timestamp=1684814440729&signature={BALANCES_VECTOR_SIGNATURE}"
    fund = f"https://open-api.bingx.com/openApi/fund/v1/account/balance?{query}"
    spot = f"https://open-api.bingx.com/openApi/spot/v1/account/balance?{query}"
    headers = {"X-BX-APIKEY": ACCESS_KEY_SENTINEL}

    with pytest.raises(AssertionError, match="fund-account"):
        fake.handler(httpx.Request("GET", fund, headers=headers))
    async with bingx_client(fake) as client:
        with pytest.raises(AssertionError, match="fund-account"):
            await client.get(fund, headers=headers)
        answered = await client.get(spot, headers=headers)

    assert answered.status_code == 200
    assert len(fake.all_requests) == 3
    assert len(fake.balance_requests) == 1
    assert fake.signature_failures == []


async def test_the_fake_venue_refuses_a_balance_request_signed_with_another_secret() -> None:
    """The control on the verifier: it can say no to a balance request, so its yes means
    something. Refused as the probe saw BingX refuse one: `100001` on a 200."""
    fake = FakeBingX(balances=[marked()], signing_key="another-secret-entirely-not-real")

    error = await refused(fake)

    assert type(error) is ExchangeAuthError
    assert error.venue_code == "100001"
    assert error.status == 200
    assert len(fake.signature_failures) == len(fake.balance_requests) == 1
    assert "does not verify" in fake.signature_failures[0]
    assert fake.balance_verified == []


def test_the_fake_venue_refuses_a_balance_signature_that_is_not_last_or_not_keyed() -> None:
    """The control on the "last" rule and the key header, on the balance path."""
    fake = FakeBingX()
    base = "https://open-api.bingx.com/openApi/spot/v1/account/balance"
    headers = {"X-BX-APIKEY": ACCESS_KEY_SENTINEL}
    signed = f"{BALANCES_VECTOR_QUERY}&signature={BALANCES_VECTOR_SIGNATURE}"
    first = httpx.Request(
        "GET",
        f"{base}?signature={BALANCES_VECTOR_SIGNATURE}&{BALANCES_VECTOR_QUERY}",
        headers=headers,
    )
    unsigned = httpx.Request("GET", f"{base}?{BALANCES_VECTOR_QUERY}", headers=headers)
    unkeyed = httpx.Request("GET", f"{base}?{signed}")
    wrong_key = httpx.Request("GET", f"{base}?{signed}", headers={"X-BX-APIKEY": "another"})
    right = httpx.Request("GET", f"{base}?{signed}", headers=headers)

    assert fake.verification_failure(first) is not None
    assert fake.verification_failure(unsigned) is not None
    assert fake.verification_failure(unkeyed) is not None
    assert fake.verification_failure(wrong_key) is not None
    assert fake.verification_failure(right) is None


@pytest.mark.parametrize(
    ("timestamp_ms", "expected"),
    [
        (0, "timestamp=0"),
        (1, "timestamp=1"),
        (1684814440729, "timestamp=1684814440729"),
        (253402300799999, "timestamp=253402300799999"),
    ],
)
def test_build_balances_query_is_the_timestamp_and_nothing_else(
    timestamp_ms: int, expected: str
) -> None:
    """`timestamp=<ms>` exactly: one key, so trivially in ASCII order, and all digits."""
    assert build_balances_query(timestamp_ms) == expected


@pytest.mark.parametrize(
    "timestamp_ms",
    [True, False, -1, "1684814440729", Decimal(1684814440729), 1684814440729.0, None],
    ids=["True", "False", "negative", "a string", "a Decimal", "a float", "None"],
)
def test_build_balances_query_refuses_a_timestamp_that_is_not_milliseconds(
    timestamp_ms: object,
) -> None:
    """A direct caller's mistake, refused rather than written into a signed query."""
    with pytest.raises(ValueError, match="timestamp_ms") as caught:
        build_balances_query(timestamp_ms)  # type: ignore[arg-type]

    assert not isinstance(caught.value, ExchangeError)


# --------------------------------------------------------------------------------------
# Criterion 2: a documented answer, parsed
# --------------------------------------------------------------------------------------

#: An answer in the documented shape, written by hand: the envelope, `data.balances`, and
#: entries of `{asset, free, locked}` whose amounts are strings. Two things in it are the
#: documentation's own (spec 025): an amount of seventeen significant digits,
#: `"244.18616265388994"`, and an asset listed with `free` and `locked` both `"0"`. The
#: rest is synthetic and is nobody's holdings.
SAMPLE_SHAPED_BODY: Final = (
    '{"code":0,"msg":"","debugMsg":"","data":{"balances":['
    '{"asset":"USDT","free":"16.73971130673954","locked":"244.18616265388994"},'
    '{"asset":"KAS","free":"1500.25","locked":"0"},'
    '{"asset":"VST","free":"0","locked":"0"}]}}'
)


async def test_a_documented_answer_is_read_as_one_total_per_asset() -> None:
    """By hand, with R1's decode of each part first.

    `16.73971130673954` is sixteen significant digits and its sixteenth is 4, so it reads
    `16.7397113067395`. `244.18616265388994` is seventeen, and `...88994` rounds up to
    `244.186162653890`. Their sum is `260.9258739606295`. `KAS` is `1500.25 + 0`. `VST`
    holds nothing and is left out, and the two that remain are sorted by asset.
    """
    fake = scripted(Reply(body=SAMPLE_SHAPED_BODY))

    result = checked(await fetch_balances(fake))

    assert result == (
        AssetBalance(asset="KAS", quantity=Decimal("1500.25")),
        AssetBalance(asset="USDT", quantity=Decimal("260.9258739606295")),
    )
    assert_one_signed_balance_request(fake)
    pure = parse_balances(unwrap_envelope(200, SAMPLE_SHAPED_BODY, member="balances"))
    assert checked(pure) == result


async def test_the_marker_entry_is_itself_a_valid_balance(read: Reader) -> None:
    """The control for every refusal below: unedited, the marker entry is read."""
    result = await read([marked()])

    assert pairs(result) == [("ZZMARK", Decimal("8484.8484"))]


@pytest.mark.parametrize(
    ("free", "locked", "total"),
    [
        pytest.param("0.1", "0.2", "0.3", id="binary floats would say 0.30000000000000004"),
        pytest.param("0.7", "0.1", "0.8", id="binary floats would say 0.7999999999999999"),
        pytest.param("1500", "0.25", "1500.25", id="an integer and a fraction"),
        pytest.param("0", "5", "5", id="all of it locked"),
        pytest.param("5", "0", "5", id="none of it locked"),
        pytest.param("5", "5", "10", id="half of it locked"),
        pytest.param(
            "0.000000000000000001",
            "0.000000000000000001",
            "0.000000000000000002",
            id="two units at eighteen places",
        ),
        pytest.param(
            "99999999999999900000",
            "0.000000000000000001",
            "99999999999999900000.000000000000000001",
            id="thirty-eight significant digits, past any default precision",
        ),
        pytest.param(
            "99999999999999900000",
            "99999",
            "99999999999999999999",
            id="the largest integer the column holds",
        ),
    ],
)
async def test_the_total_is_free_plus_locked_exactly(
    read: Reader, free: str, locked: str, total: str
) -> None:
    """`free + locked`, in `Decimal`, with no digit lost and no float anywhere."""
    result = await read([held("KAS", free, locked)])

    assert pairs(result) == [("KAS", Decimal(total))]


def test_the_total_ignores_the_callers_decimal_context() -> None:
    """A thread whose context is three digits rounding down still gets the exact sum, and
    the stated decode: neither the addition nor the decode consults the ambient context."""
    entries = decode_json(
        balances_fragment(
            [
                held("KAS", "99999999999999900000", "0.000000000000000001"),
                held("USDT", "16.73971130673954", "244.18616265388994"),
            ]
        )
    )

    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        result = parse_balances(entries)

    assert pairs(result) == [
        ("KAS", Decimal("99999999999999900000.000000000000000001")),
        ("USDT", Decimal("260.9258739606295")),
    ]


async def test_an_unexpected_extra_field_is_tolerated_and_never_added(read: Reader) -> None:
    """A field nobody documented is ignored, whatever it looks like: the total is still
    `free + locked`, not `free + locked + frozen`."""
    entry = held(
        "KAS",
        "1",
        "2",
        frozen='"4242"',
        total='"9999"',
        available="7",
        uTime="1695865274510",
        nested='{"a":[1,"b",null]}',
    )

    result = await read([entry])

    assert pairs(result) == [("KAS", Decimal(3))]


async def test_extra_fields_around_the_list_are_tolerated() -> None:
    """At every level of the envelope, and a `fills` member beside `balances` included."""
    body = (
        '{"code":0,"msg":"","debugMsg":"","retryable":false,"timestamp":1695865274510,'
        '"data":{"total":1,"fills":[],"balances":[{"asset":"KAS","free":"1","locked":"2"}]}}'
    )

    result = await fetch_balances(scripted(Reply(body=body)))

    assert pairs(result) == [("KAS", Decimal(3))]


# --------------------------------------------------------------------------------------
# Zeros are dropped; one entry per asset; sorted
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "zero",
    [
        pytest.param(held("VST", "0", "0"), id="the documented spelling"),
        pytest.param(held("VST", "0.0", "0.00000000"), id="with decimal places"),
        pytest.param(held("VST", "-0", "0"), id="a negative zero free"),
        pytest.param(held("VST", "0", "-0.0"), id="a negative zero locked"),
        pytest.param(held("VST", "0e-30", "0E+5"), id="with exponents"),
        pytest.param(raw(asset='"VST"', free="0", locked="0.0"), id="bare JSON numbers"),
    ],
)
async def test_a_zero_balance_is_dropped(read: Reader, zero: VenueBalance) -> None:
    """The documented sample lists an asset the account does not hold. It is left out, and
    the assets around it are kept."""
    result = await read([held("KAS", "1500"), zero, held("BTC", "0.25")])

    assert pairs(result) == [("BTC", Decimal("0.25")), ("KAS", Decimal(1500))]


async def test_an_account_of_nothing_but_zeros_holds_nothing(read: Reader) -> None:
    assert await read([held("VST"), held("USDT", "0.0", "0")]) == ()


async def test_an_empty_list_is_an_account_that_holds_nothing(read: Reader) -> None:
    """`balances: []` cannot be told from an empty account, and reads as one."""
    assert await read([]) == ()


async def test_the_smallest_storable_amount_is_not_a_zero(read: Reader) -> None:
    result = await read([held("KAS", "0", "0.000000000000000001")])

    assert pairs(result) == [("KAS", Decimal("0.000000000000000001"))]


async def test_the_balances_are_sorted_by_asset_in_code_point_order(read: Reader) -> None:
    """Digits, then capitals, then lower case, whatever order the venue lists them in."""
    names = ["kas", "ZEC", "1INCH", "BTC", "Btc", "AAVE"]

    result = await read([held(name, "1") for name in names])

    assert [entry.asset for entry in result] == ["1INCH", "AAVE", "BTC", "Btc", "ZEC", "kas"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("4242.4242", "1", id="two holdings"),
        pytest.param("4242.4242", "4242.4242", id="the same holding twice"),
        pytest.param("4242.4242", "0", id="a holding and a zero"),
        pytest.param("0", "4242.4242", id="a zero and a holding"),
    ],
)
async def test_an_asset_named_twice_is_refused(read: Reader, first: str, second: str) -> None:
    """Refused, not summed and not last-one-wins, and before the zeros are dropped."""
    error = await refusal(
        read,
        held("KAS", "5"),
        held(MARK_ASSET, first),
        held("BTC", "0.25"),
        held(MARK_ASSET, second),
    )

    assert "more than once" in error.detail
    assert "KAS" not in rendered(error)
    assert "BTC" not in rendered(error)


async def test_an_asset_named_twice_at_zero_both_times_is_refused(read: Reader) -> None:
    """It is the answer's shape that is not recognised, not its arithmetic."""
    error = await refusal(read, held(MARK_ASSET), held("KAS", "5"), held(MARK_ASSET))

    assert "more than once" in error.detail


# --------------------------------------------------------------------------------------
# The asset: as reported, and held to the base-asset rule
# --------------------------------------------------------------------------------------

#: `M`, a capital O with a stroke, `TH`: a name in BingX's live list. Built with `chr`.
O_STROKE: Final = chr(0xD8)

ACCEPTED_ASSETS: Final = [
    pytest.param("KAS", "KAS", id="upper case"),
    pytest.param("kas", "kas", id="lower case, kept"),
    pytest.param("Kas", "Kas", id="mixed case, kept"),
    pytest.param("1INCH", "1INCH", id="starting with a digit"),
    pytest.param("$U", "$U", id="a dollar sign"),
    pytest.param("D.O.G.E.", "D.O.G.E.", id="dots"),
    pytest.param("ATOM(ARC20)", "ATOM(ARC20)", id="parentheses"),
    pytest.param("STRK-OLD", "STRK-OLD", id="a hyphen"),
    pytest.param("H_OLD", "H_OLD", id="an underscore"),
    pytest.param(f"M{json_escape(0xD8)}TH", f"M{O_STROKE}TH", id="a non-ASCII letter"),
    pytest.param("A", "A", id="one character"),
    pytest.param("A" * 40, "A" * 40, id="forty characters, the longest"),
]


@pytest.mark.parametrize(("sent", "expected"), ACCEPTED_ASSETS)
async def test_the_asset_is_kept_exactly_as_reported(
    read: Reader, sent: str, expected: str
) -> None:
    """The venue's spelling, unchanged: the reconciliation joins a balance to the fills by
    name, and a fill's base asset is the venue's spelling too. The rule is as wide as
    BingX's own names are."""
    result = await read([held(sent, "1.5")])

    assert pairs(result) == [(expected, Decimal("1.5"))]


async def test_two_spellings_of_one_name_are_two_assets_and_not_a_duplicate(
    read: Reader,
) -> None:
    """Nothing is upper-cased: `kas` and `KAS` are two entries, each with its own total.

    Bitget's provider upper-cases; this one must not. A fold here would refuse this answer
    as a duplicate, or merge two names the venue keeps apart.
    """
    result = await read([held("kas", "1"), held("KAS", "2"), held("Kas", "4")])

    assert pairs(result) == [("KAS", Decimal(2)), ("Kas", Decimal(4)), ("kas", Decimal(1))]


async def test_the_asset_name_bound_is_forty_characters(read: Reader) -> None:
    """The boundary, pinned on both sides: exactly forty is read, forty-one is refused."""
    forty = MARK_ASSET + "A" * 34
    forty_one = MARK_ASSET + "A" * 35
    assert (len(forty), len(forty_one)) == (40, 41)

    result = await read([held(forty, "1")])
    error = await refusal(read, held(forty_one, "1"))

    assert pairs(result) == [(forty, Decimal(1))]
    assert names_field(error, "asset")


NOT_A_BASE_ASSET: Final = {
    "empty": '""',
    "a space inside": '"ZZMARK X"',
    "a leading space, not trimmed": '" ZZMARK"',
    "a trailing space, not trimmed": '"ZZMARK "',
    "a tab": '"ZZMARK\\tX"',
    "a trailing newline": '"ZZMARK\\n"',
    "a no-break space": f'"ZZMARK{json_escape(0xA0)}X"',
    "a NUL": f'"ZZMARK{json_escape(0x00)}"',
    "a control character": f'"ZZMARK{json_escape(0x01)}"',
    "a zero-width space, format": f'"ZZMARK{json_escape(0x200B)}"',
    "a private-use character": f'"{json_escape(0xE000)}ZZMARK"',
    "an unassigned code point": f'"{json_escape(0x0378)}ZZMARK"',
    "a lone surrogate in a name": f'"ZZMARK{json_escape(0xD800)}"',
    "a lone surrogate alone": f'"{json_escape(0xD800)}"',
    "a lone low surrogate": f'"{json_escape(0xDC00)}ZZMARK"',
    "a number": "7",
    "an amount": "4242.4242",
    "null": "null",
    "true": "true",
    "an array": '["ZZMARK"]',
    "an object": '{"name":"ZZMARK"}',
}


@pytest.mark.parametrize("case", list(NOT_A_BASE_ASSET))
async def test_an_asset_that_could_not_be_a_base_asset_is_refused(read: Reader, case: str) -> None:
    """Not a string, empty, or holding whitespace or a control, format, private-use,
    unassigned or surrogate character. Refused, never trimmed or repaired.

    Nothing is chained to the refusal: a `UnicodeEncodeError` keeps the whole string in its
    `args`, and here the string is an asset the owner holds.
    """
    error = await refusal(read, held("KAS", "5"), marked(asset=NOT_A_BASE_ASSET[case]))

    assert names_field(error, "asset")
    assert error.__cause__ is None
    assert error.__context__ is None


# --------------------------------------------------------------------------------------
# R1: each part is decoded from a binary float, on its own, before the sign and the sum
# --------------------------------------------------------------------------------------

#: Each rounding worked out by hand: count fifteen significant digits, look at the
#: sixteenth and what follows, and round half to even.
DECODED: Final = [
    pytest.param("244.18616265388994", "244.186162653890", id="the documented sample"),
    pytest.param("0.000012340000000000001", "0.00001234", id="dust, twenty-one places"),
    pytest.param("17.997667582000002", "17.997667582", id="noise after a clean value"),
    pytest.param("16.73971130673954", "16.7397113067395", id="sixteenth digit 4: down"),
    pytest.param("1.234567890123447", "1.23456789012345", id="sixteenth digit 7: up"),
    pytest.param("1.234567890123445", "1.23456789012344", id="a tie after an even digit"),
    pytest.param("1.234567890123455", "1.23456789012346", id="a tie after an odd digit"),
    pytest.param("6696.471396937", "6696.471396937", id="a clean value, unchanged"),
    pytest.param("0.000012345678901234", "0.000012345678901234", id="eighteen places, unchanged"),
    pytest.param(
        "123456789.123456789", "123456789.123457", id="eighteen real digits: the stated cost"
    ),
    pytest.param("1234567890123456789", "1234567890123460000", id="nineteen integer digits"),
]


@pytest.mark.parametrize("field", ["free", "locked"])
@pytest.mark.parametrize(("written", "expected"), DECODED)
async def test_each_part_is_decoded_to_fifteen_significant_digits(
    read: Reader, written: str, expected: str, field: str
) -> None:
    """R1: BingX formats these strings from doubles, and anything past the fifteenth
    significant digit is the double's noise, not a holding.

    Without the decode `0.000012340000000000001` is twenty-one places, finer than the
    column, and one such entry would fail the whole read on every run. The last two rows
    are the ruling's cost, pinned where a reader can see it: an amount that really had more
    than fifteen digits is moved by at most half a unit in the fifteenth.
    """
    other = "locked" if field == "free" else "free"

    result = await read([held("KAS", **{field: written, other: "0"})])

    assert pairs(result) == [("KAS", Decimal(expected))]


async def test_a_seventeen_digit_double_reads_as_fifteen_digits(read: Reader) -> None:
    """The documentation's own sample amount: `244.18616265388994` reads `244.186162653890`."""
    result = await read([held("USDT", "0", "244.18616265388994")])

    assert pairs(result) == [("USDT", Decimal("244.186162653890"))]
    assert result[0].quantity != Decimal("244.18616265388994")


async def test_dust_written_with_more_than_eighteen_places_is_read(read: Reader) -> None:
    """`0.000012340000000000001` is `0.00001234` and a double's tail, and is accepted."""
    result = await read([held("KAS", "0.000012340000000000001", "0")])

    assert pairs(result) == [("KAS", Decimal("0.00001234"))]


@pytest.mark.parametrize("field", ["free", "locked"])
@pytest.mark.parametrize(
    "written",
    [
        pytest.param("0.0000123456789012345", id="fifteen digits, nineteen places"),
        pytest.param("0.0000424242424242424", id="the marked one"),
        pytest.param("0.0000000000000000001", id="one unit at nineteen places"),
        pytest.param("1e-19", id="written with an exponent"),
        pytest.param("1.2345678901234567e-10", id="rounded, and still twenty-four places"),
    ],
)
async def test_a_part_still_finer_than_the_column_after_the_decode_is_refused(
    read: Reader, written: str, field: str
) -> None:
    """The decode removes float noise; it never makes a value fit the column.

    Fifteen significant digits nineteen places down is not noise: the decode leaves it
    alone, `NumericText(18)` would round it, and so the read is refused rather than stored
    as something the venue did not say.
    """
    other = "locked" if field == "free" else "free"

    error = await refusal(read, held(MARK_ASSET, **{field: written, other: "0"}))

    assert "18 decimal places" in error.detail


@pytest.mark.parametrize(
    ("free", "locked", "each_part", "the_sum_decoded", "undecoded"),
    [
        pytest.param(
            "1.00000000000000049",
            "0.00000000000000049",
            "1.00000000000000049",
            "1",
            "1.00000000000000098",
            id="noise in one part beside a small real part",
        ),
        pytest.param(
            "123456789.123456789",
            "0.000000123456789012",
            "123456789.123457123456789012",
            "123456789.123457",
            "123456789.123456912456789012",
            id="a wide part beside a fine one",
        ),
    ],
)
async def test_each_part_is_decoded_on_its_own_before_the_sum(
    read: Reader, free: str, locked: str, each_part: str, the_sum_decoded: str, undecoded: str
) -> None:
    """The noise is each double's, so each is decoded and then they are added.

    By hand, for the first row: `1.00000000000000049` is eighteen digits and reads `1`;
    `0.00000000000000049` is two digits and is left alone; the total is
    `1.00000000000000049`. Decoding the sum instead, `1.00000000000000098`, would give `1`
    and lose the locked part; not decoding at all would give `1.00000000000000098`.
    """
    assert len({Decimal(each_part), Decimal(the_sum_decoded), Decimal(undecoded)}) == 3

    result = await read([held("KAS", free, locked)])

    assert pairs(result) == [("KAS", Decimal(each_part))]


NEGATIVE_PARTS: Final = [
    pytest.param('"-4242.4242"', id="marked"),
    pytest.param('"-0.000000000000000001"', id="one unit"),
    pytest.param('"-0.000012340000000000001"', id="dust with float noise: the sign survives"),
    pytest.param('"-244.18616265388994"', id="seventeen digits: the sign survives"),
    pytest.param('"-0.0000000000000000001"', id="finer than the column"),
    pytest.param('"-1e-7"', id="written with an exponent"),
    pytest.param("-4242.4242", id="a bare JSON number"),
]


@pytest.mark.parametrize("field", ["free", "locked"])
@pytest.mark.parametrize("negative", NEGATIVE_PARTS)
async def test_a_negative_part_is_refused_even_when_the_total_would_be_positive(
    read: Reader, negative: str, field: str
) -> None:
    """Each part on its own, after the decode and before the sum.

    The other part is `9999.5`, so every total here is positive: a check on the sum alone
    would accept an answer in which the venue reported less than nothing of something.
    """
    other = "locked" if field == "free" else "free"

    entry = raw(asset=f'"{MARK_ASSET}"', **{field: negative, other: '"9999.5"'})

    error = await refusal(read, entry)

    assert names_field(error, field)
    assert "negative" in error.detail


async def test_a_negative_zero_is_a_zero_and_not_a_negative_part(read: Reader) -> None:
    """`-0` is how a double prints a zero it reached from below. It holds nothing."""
    result = await read([held("KAS", "-0", "5"), held("BTC", "0.25", "-0.0")])

    assert pairs(result) == [("BTC", Decimal("0.25")), ("KAS", Decimal(5))]


# --------------------------------------------------------------------------------------
# What is, and is not, an amount
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("free", "locked", "total"),
    [
        pytest.param("0.1", "0.2", "0.3", id="two numbers a float cannot add"),
        pytest.param("7", "0", "7", id="integers"),
        pytest.param("244.18616265388994", "0", "244.18616265389", id="decoded like a string"),
        pytest.param("1e2", "1.2493e-7", "100.00000012493", id="exponents"),
        pytest.param("0.1", '"0.2"', "0.3", id="a number beside a string"),
        pytest.param("1500.250", "0.0", "1500.25", id="trailing zeros"),
    ],
)
async def test_a_bare_json_number_is_read_exactly_and_never_through_a_float(
    read: Reader, free: str, locked: str, total: str
) -> None:
    """**A JSON number is accepted, as a string is.** The documentation types the three
    fields as strings, and `require_fill_amount`'s house rule is that a number is an amount
    too: the shared decoder builds a `Decimal` from the number's own text, or an `int`, so
    the digits the venue wrote arrive as written and no float exists at any point. `0.1`
    and `0.2` as bare numbers therefore total `0.3` exactly, and a number carrying float
    noise is decoded exactly as the same digits in a string are.
    """
    result = await read([raw(free=free, locked=locked)])

    assert pairs(result) == [("KAS", Decimal(total))]


async def test_a_string_amount_may_carry_an_exponent(read: Reader) -> None:
    """A double small enough is printed as `1.2493e-7` by more than one formatter."""
    result = await read([held("KAS", "1.2493e-7", "1E2")])

    assert pairs(result) == [("KAS", Decimal("100.00000012493"))]


@pytest.mark.parametrize("field", ["free", "locked"])
def test_a_python_float_handed_to_the_parser_is_refused(field: str) -> None:
    """Money is never a float. The decoder cannot produce one; a caller that built one some
    other way is refused here, not rounded into a `Decimal`."""
    other = "locked" if field == "free" else "free"
    entry: dict[str, object] = {"asset": MARK_ASSET, field: 4242.4242, other: "0"}

    with pytest.raises(ExchangeError) as caught:
        parse_balances([entry])

    assert type(caught.value) is ExchangeSchemaError
    assert names_field(caught.value, field)
    assert_carries_no_value(caught.value)


NOT_AN_AMOUNT: Final = {
    "true": "true",
    "false": "false",
    "null": "null",
    "an empty object": "{}",
    "an object holding an amount": '{"amount":"4242.4242"}',
    "an empty array": "[]",
    "an array holding an amount": '["4242.4242"]',
    "letters": '"abc"',
    "an amount followed by letters": '"4242.4242abc"',
    "an empty string": '""',
    "a leading space": '" 4242.4242"',
    "a trailing space": '"4242.4242 "',
    "a trailing newline": '"4242.4242\\n"',
    "NaN": '"NaN"',
    "Infinity": '"Infinity"',
    "negative Infinity": '"-Infinity"',
    "a plus sign": '"+4242.4242"',
    "a decimal comma": '"4242,4242"',
    "a thousands separator": '"4,242.4242"',
    "an underscore": '"4_242.4242"',
    "hexadecimal": '"0x4242"',
    "no digit before the point": '".4242"',
    "no digit after the point": '"4242."',
    "an exponent with no digits": '"4242e"',
    "two points": '"4242.42.42"',
    "fullwidth digits": f'"{json_escape(0xFF14)}{json_escape(0xFF12)}"',
    "a string past a hundred digits by its exponent": '"4242e400"',
    "a number past a hundred digits by its exponent": "4242e400",
    "a hundred and one digits": '"' + "4242" * 25 + '1"',
    "five thousand digits": '"' + "4242" * 1250 + '"',
    "a number with five thousand places": "0." + "4242" * 1250,
    "an exponent Decimal cannot hold, as a string": '"4242e1000000000000000000"',
}


@pytest.mark.parametrize("field", ["free", "locked"])
@pytest.mark.parametrize("case", list(NOT_AN_AMOUNT))
async def test_a_value_that_is_not_an_amount_is_refused(
    read: Reader, case: str, field: str
) -> None:
    """A boolean, a null, a container, text that is not a plain decimal number, a NaN, an
    infinity, or a number no venue writes: a schema error naming the field, never a
    `TypeError`, a `ValueError` or a `decimal.InvalidOperation`, and never a zero."""
    error = await refusal(read, held("KAS", "5"), marked(**{field: NOT_AN_AMOUNT[case]}))

    assert names_field(error, field)


@pytest.mark.parametrize("name", ["asset", "free", "locked"])
async def test_a_missing_field_is_refused_and_never_read_as_zero(read: Reader, name: str) -> None:
    """Both parts are required. An entry without `locked` is not "nothing locked": it is an
    answer in a shape nobody documented, and half a balance is a wrong balance."""
    error = await refusal(read, held("KAS", "5"), marked(**{name: None}))

    assert names_field(error, name)
    assert "missing" in error.detail


@pytest.mark.parametrize(
    ("free", "locked"),
    [
        pytest.param("60000000000000000000", "50000000000000000000", id="each fits, the sum not"),
        pytest.param("424242424242424242424", "0", id="twenty-one integer digits"),
        pytest.param("0", "424242424242424242424", id="twenty-one integer digits, locked"),
        pytest.param("99999999999999999999", "0", id="twenty nines, a twenty-first once decoded"),
        pytest.param("4.242e99", "0", id="a hundred digits"),
        pytest.param("4.242e99", "1e-99", id="parts a hundred and ninety-eight places apart"),
        pytest.param("4242" * 25, "0." + "0" * 98 + "1", id="the widest pair the bound admits"),
    ],
)
async def test_a_total_too_large_for_the_column_is_a_schema_error(
    read: Reader, free: str, locked: str
) -> None:
    """More than twenty digits before the point: refused as one of the seven classes, not as
    the bare `decimal.InvalidOperation` a `quantize` would raise, and not stored rounded."""
    error = await refusal(read, held(MARK_ASSET, free, locked))

    assert "20 digits before the decimal point" in error.detail
    assert error.__cause__ is None


# --------------------------------------------------------------------------------------
# The envelope: where the list is, and what is not a list
# --------------------------------------------------------------------------------------

MARKED_ENTRY: Final = '{"asset":"ZZMARK","free":"4242.4242","locked":"4242.4242"}'

NO_BALANCES_ARRAY: Final = {
    "data absent": '{"code":0,"msg":"","debugMsg":""}',
    "data null": '{"code":0,"msg":"","debugMsg":"","data":null}',
    "data an empty array": '{"code":0,"msg":"","debugMsg":"","data":[]}',
    "data an array of entries": f'{{"code":0,"msg":"","data":[{MARKED_ENTRY}]}}',
    "data a string": '{"code":0,"msg":"","debugMsg":"","data":"balances"}',
    "data a number": '{"code":0,"msg":"","debugMsg":"","data":4242.4242}',
    "data true": '{"code":0,"msg":"","debugMsg":"","data":true}',
    "balances absent": '{"code":0,"msg":"","debugMsg":"","data":{}}',
    "balances null": '{"code":0,"msg":"","data":{"balances":null}}',
    "balances an empty object": '{"code":0,"msg":"","data":{"balances":{}}}',
    "balances one entry, not a list": f'{{"code":0,"msg":"","data":{{"balances":{MARKED_ENTRY}}}}}',
    "balances a string": '{"code":0,"msg":"","data":{"balances":"[]"}}',
    "balances a number": '{"code":0,"msg":"","data":{"balances":0}}',
    "balances false": '{"code":0,"msg":"","data":{"balances":false}}',
    "the list under another name": f'{{"code":0,"msg":"","data":{{"assets":[{MARKED_ENTRY}]}}}}',
    "the list under a capitalised name": (
        f'{{"code":0,"msg":"","data":{{"Balances":[{MARKED_ENTRY}]}}}}'
    ),
    "the list beside data": f'{{"code":0,"msg":"","data":{{}},"balances":[{MARKED_ENTRY}]}}',
    "an empty fills answer": '{"code":0,"msg":"","debugMsg":"","data":{"fills":[]}}',
    "code false": '{"code":false,"msg":"","data":{"balances":[]}}',
    "code the string 0": '{"code":"0","msg":"","data":{"balances":[]}}',
    "code 0.0": '{"code":0.0,"msg":"","data":{"balances":[]}}',
    "code null": '{"code":null,"msg":"","data":{"balances":[]}}',
    "code absent": '{"msg":"","data":{"balances":[]}}',
}


@pytest.mark.parametrize("case", list(NO_BALANCES_ARRAY))
async def test_code_zero_without_a_balances_array_is_a_schema_error(case: str) -> None:
    """Success is HTTP 200, the integer `0`, and a `data` object holding a `balances` array.

    **A missing list is never read as "the account holds nothing".** That reading would
    replace the stored balances with none, and every asset the owner holds at this venue
    would show as history with nothing held against it.
    """
    fake = scripted(Reply(body=NO_BALANCES_ARRAY[case]))

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError
    assert error.__cause__ is None
    assert error.__context__ is None
    assert_carries_no_value(error)
    assert_one_signed_balance_request(fake)


async def test_an_empty_balances_array_is_the_account_holding_nothing() -> None:
    """The companion: code 0 with `data.balances: []` is an answer, and it is `()`."""
    fake = scripted(Reply(body=envelope('{"balances":[]}')))

    assert await fetch_balances(fake) == ()
    assert_one_signed_balance_request(fake)


async def test_a_fills_answer_to_a_balance_request_is_refused() -> None:
    """A successful body that carries `fills` and no `balances` is the wrong answer, and is
    not an empty account."""
    fill = VenueFill(trade_id=36_767_057, executed_ms=1695776400000)
    fake = scripted(Reply(body=fills_body([fill])))

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError
    assert "data.balances" in str(error)
    assert_one_signed_balance_request(fake)


NOT_AN_ENTRY: Final = {
    "a number": "7",
    "an amount": "4242.4242",
    "a string": '"ZZMARK"',
    "null": "null",
    "true": "true",
    "an empty array": "[]",
    "an entry inside an array": f"[{MARKED_ENTRY}]",
}


@pytest.mark.parametrize("case", list(NOT_AN_ENTRY))
async def test_an_element_that_is_not_an_object_is_refused(read: Reader, case: str) -> None:
    """Refused, not skipped: an element nobody can read may be a holding nobody counted."""
    error = await refusal(read, marked(), NOT_AN_ENTRY[case], held("KAS", "5"))

    assert "JSON object" in error.detail


@pytest.mark.parametrize(
    "balances",
    [
        None,
        {},
        {"asset": MARK_ASSET, "free": MARK_AMOUNT, "locked": "0"},
        "[]",
        0,
        True,
        ({"asset": MARK_ASSET, "free": MARK_AMOUNT, "locked": "0"},),
    ],
    ids=["None", "an empty dict", "one entry", "a string", "zero", "True", "a tuple"],
)
def test_parse_balances_refuses_what_is_not_an_array(balances: object) -> None:
    """The rule `unwrap_envelope` applies to the envelope, held again by the parser itself:
    a direct caller's `None` is not "no balances"."""
    with pytest.raises(ExchangeError) as caught:
        parse_balances(balances)

    assert type(caught.value) is ExchangeSchemaError
    assert "array" in caught.value.detail
    assert_carries_no_value(caught.value)
    # The companion: an empty array is an empty account.
    assert parse_balances([]) == ()


def test_unwrap_envelope_takes_fills_by_default_and_balances_when_told() -> None:
    """One function for both endpoints, and `member` says which list. The default is still
    `fills`: a fills caller that names no member must not start reading balances."""
    balances = balances_body([marked()])
    fills = fills_body([])

    assert inspect.signature(unwrap_envelope).parameters["member"].default == "fills"
    assert unwrap_envelope(200, fills) == []
    assert unwrap_envelope(200, balances, member="balances") == [
        {"asset": "ZZMARK", "free": "4242.4242", "locked": "4242.4242"}
    ]
    assert unwrap_envelope(200, balances_body([]), member=BALANCES_MEMBER) == []

    with pytest.raises(ExchangeError) as by_default:
        unwrap_envelope(200, balances)
    with pytest.raises(ExchangeError) as told_balances:
        unwrap_envelope(200, fills, member="balances")

    assert type(by_default.value) is ExchangeSchemaError
    assert "data.fills" in str(by_default.value)
    assert_carries_no_value(by_default.value)
    assert type(told_balances.value) is ExchangeSchemaError
    assert "data.balances" in str(told_balances.value)


# --------------------------------------------------------------------------------------
# Criterion 1: every failure is one of the seven classes, through the same envelope
# --------------------------------------------------------------------------------------

#: The BingX error table, written by hand from spec 017, as `test_bingx.py` pins it for
#: fills: the balance read is classified by the same map, so each code means what it meant.
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
    109500: ExchangeUnavailableError,
    # A request this code built.
    100400: ExchangeInvalidRequestError,
    100204: ExchangeInvalidRequestError,
    100404: ExchangeInvalidRequestError,
    100490: ExchangeInvalidRequestError,
}

#: A code BingX does not use and the map does not hold.
UNMAPPED_CODE: Final = 100999


def test_the_table_names_six_of_the_seven_classes() -> None:
    """The premise of "each error class through the envelope": auth, scope, rate limit,
    unavailable and invalid request are each reached by a code, schema by an unmapped one.
    Nothing BingX sends maps to the retention class."""
    assert set(DOCUMENTED_CODES.values()) == {
        ExchangeAuthError,
        ExchangeInsufficientScopeError,
        ExchangeRateLimitedError,
        ExchangeUnavailableError,
        ExchangeInvalidRequestError,
    }
    assert UNMAPPED_CODE not in DOCUMENTED_CODES


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize(("code", "expected"), sorted(DOCUMENTED_CODES.items()))
async def test_each_in_band_code_maps_to_its_class_on_a_balance_read(
    code: int, expected: type[ExchangeError], status: int
) -> None:
    """On a 200, as the probe saw every refusal arrive, and on a 400: the code decides.

    The venue echoes holdings in its `msg` here, and nothing of the message is carried.
    """
    fake = scripted(Reply(status=status, body=error_body(code, ECHO)))

    error = await refused(fake)

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == str(code)
    assert_carries_no_value(error)
    assert_one_signed_balance_request(fake)


STATUS_BODIES: Final = {"html": HTML_BODY, "json": error_body(UNMAPPED_CODE, ECHO)}


@pytest.mark.parametrize("body", sorted(STATUS_BODIES))
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ExchangeAuthError),
        (403, ExchangeAuthError),
        (418, ExchangeRateLimitedError),
        (429, ExchangeRateLimitedError),
        (500, ExchangeUnavailableError),
        (502, ExchangeUnavailableError),
        (503, ExchangeUnavailableError),
        (504, ExchangeUnavailableError),
    ],
)
async def test_each_status_maps_to_its_class_on_a_balance_read(
    status: int, expected: type[ExchangeError], body: str
) -> None:
    """The exact class, never a superclass, never chained from an HTTP status error, and a
    502 carrying HTML is unavailable, not a schema error."""
    fake = scripted(Reply(status=status, body=STATUS_BODIES[body], headers={"Retry-After": "7"}))

    error = await refused(fake)

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == (str(UNMAPPED_CODE) if body == "json" else None)
    if isinstance(error, ExchangeRateLimitedError):
        assert error.retry_after_ms == 7000
    assert not any(isinstance(link, httpx.HTTPStatusError) for link in links(error))
    assert_carries_no_value(error)
    assert fake.balance_verified == fake.all_requests
    assert fake.requests == []


async def test_a_rate_limit_without_retry_after_carries_none() -> None:
    """`None` is "the venue said nothing", which is not `0`, "immediately"."""
    error = await refused(scripted(Reply(status=429, body=error_body(100410))))

    assert type(error) is ExchangeRateLimitedError
    assert error.retry_after_ms is None


async def test_a_retry_after_date_is_measured_from_the_providers_clock() -> None:
    """The injected clock reads 2023-05-23T04:00:40.729Z, and the venue asks for 04:00:50:
    9271 milliseconds, by hand, whatever the wall clock says."""
    fake = scripted(
        Reply(
            status=429,
            body=error_body(109429),
            headers={"Retry-After": "Tue, 23 May 2023 04:00:50 GMT"},
        )
    )

    error = await refused(fake)

    assert type(error) is ExchangeRateLimitedError
    assert error.retry_after_ms == 9271


@pytest.mark.parametrize("status", [200, 400, 401])
async def test_a_timestamp_error_on_a_balance_read_is_never_auth(status: int) -> None:
    """`100421` under any status: a replayed request can arrive stale, and a skewed clock is
    not a bad key. Spec 025 skips later reads after an `auth` balance failure, so a working
    key must not be recorded as one."""
    error = await refused(scripted(Reply(status=status, body=error_body(100421))))

    assert type(error) is ExchangeUnavailableError
    assert not isinstance(error, ExchangeAuthError)
    assert error.venue_code == "100421"


async def test_an_unmapped_code_on_a_balance_read_is_a_schema_error_on_a_200() -> None:
    """A code nobody mapped is an answer not understood: loud, and never an empty account.

    The companions: the same code on a 400 is left to the status, and `100403`, which means
    two things in two BingX pages, is left unmapped: schema on a 200, auth on a 403.
    """
    on_200 = await refused(scripted(Reply(body=error_body(UNMAPPED_CODE, ECHO))))
    on_400 = await refused(scripted(Reply(status=400, body=error_body(UNMAPPED_CODE, ECHO))))
    ambiguous_on_200 = await refused(scripted(Reply(body=error_body(100403))))
    ambiguous_on_403 = await refused(scripted(Reply(status=403, body=error_body(100403))))

    assert type(on_200) is ExchangeSchemaError
    assert on_200.venue_code == str(UNMAPPED_CODE)
    assert_carries_no_value(on_200)
    assert type(on_400) is ExchangeInvalidRequestError
    assert type(ambiguous_on_200) is ExchangeSchemaError
    assert type(ambiguous_on_403) is ExchangeAuthError


async def test_an_error_code_beside_a_balances_list_is_still_an_error() -> None:
    """The code decides before the list is looked at: a refusal that also carries a
    plausible `data.balances` is a refusal, and nothing in it is read."""
    body = f'{{"code":100004,"msg":"{ECHO}","data":{{"balances":[{MARKED_ENTRY}]}}}}'

    error = await refused(scripted(Reply(body=body)))

    assert type(error) is ExchangeInsufficientScopeError
    assert_carries_no_value(error)


NOT_THE_ENVELOPE: Final = {
    "not JSON": "ZZMARK 4242.4242 is not json at all",
    "HTML": HTML_BODY,
    "an array": f"[{MARKED_ENTRY}]",
    "a number": "4242.4242",
    "a string": '"ZZMARK"',
    "empty": "",
    "truncated": '{"code":0,"data":{"balances":[{"asset":"ZZMARK","free":"4242.4242"',
    "truncated inside the envelope": '{"code":0,"data":{"balances":[',
    "NaN, which is not JSON": '{"code":0,"data":{"balances":[{"asset":"ZZMARK","free":NaN}]}}',
}


@pytest.mark.parametrize("case", list(NOT_THE_ENVELOPE))
async def test_a_success_status_with_a_body_that_is_not_the_envelope_is_a_schema_error(
    case: str,
) -> None:
    """HTTP 200 and a body that is not a JSON object: schema, with nothing chained.

    The decoder's own error would carry the body's text one link down the chain, and the
    body is the owner's holdings.
    """
    error = await refused(scripted(Reply(body=NOT_THE_ENVELOPE[case])))

    assert type(error) is ExchangeSchemaError
    assert error.__cause__ is None
    assert error.__context__ is None
    assert_carries_no_value(error)


async def test_a_transport_failure_is_unavailable_and_carries_no_url() -> None:
    """No answer at all: unavailable, chained from the transport's error, after the
    transport's retries -- and nothing of the signed URL anywhere in the chain."""
    cause = httpx.ConnectError("the fake venue refused the connection")
    fake = scripted(Reply(error=cause))

    error = await refused(fake)

    assert type(error) is ExchangeUnavailableError
    assert error.status is None
    assert error.__cause__ is cause
    assert not any(isinstance(link, httpx.HTTPStatusError) for link in links(error))
    signatures = [query.rpartition("&signature=")[2] for query in fake.balance_queries()]
    assert all(signatures), "the positive companion: the requests were signed and sent"
    assert len(fake.balance_requests) == 3, "and retried before failing"
    text = rendered(error)
    for fragment in (
        "signature",
        "timestamp=",
        "1684814440729",
        "open-api.bingx.com",
        "openApi",
        "account/balance",
        *signatures,
    ):
        assert fragment not in text


async def test_a_local_protocol_error_is_an_invalid_request_and_carries_nothing() -> None:
    """h11 quotes the whole illegal header value, which here would be the key.

    Reachable only through a bug, since the constructor refuses such a key; a mock transport
    raising it is the only way here, and the sentinel stands for the key it quotes.
    """
    sentinel = "LOCAL-PROTOCOL-SENTINEL-7319"
    fake = scripted(Reply(error=httpx.LocalProtocolError(f"Illegal header value b'{sentinel}'")))

    error = await refused(fake)

    assert type(error) is ExchangeInvalidRequestError
    assert error.__cause__ is None
    assert error.__context__ is None
    assert sentinel not in rendered(error)
    assert fake.balance_requests, "the positive companion: the request was attempted"


UNDECODABLE: Final = b"this is not a compressed body"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("status", [200, 500])
async def test_a_balance_body_that_does_not_decompress_is_unavailable(
    status: int, encoding: str
) -> None:
    """`httpx.DecodingError` is raised above the transport and is not a `TransportError`."""
    fake = scripted(Reply(status=status, headers={"Content-Encoding": encoding}, wire=UNDECODABLE))

    error = await refused(fake)

    assert type(error) is ExchangeUnavailableError
    assert error.__cause__ is None
    assert error.__context__ is None
    assert fake.balance_requests, "the positive companion: the call was made"


async def test_a_balance_body_that_does_decompress_is_read() -> None:
    body = gzip.compress(balances_body([held("KAS", "1500", "0.25")]).encode("ascii"))
    fake = scripted(Reply(headers={"Content-Encoding": "gzip"}, wire=body))

    result = await fetch_balances(fake)

    assert pairs(result) == [("KAS", Decimal("1500.25"))]


async def test_the_transport_replays_the_same_signed_balance_request() -> None:
    """A 429 then a 200: the retry resends the request as signed, and the answer is read.

    The clock moves a second on every read, so a provider that re-signed per attempt would
    send a different timestamp and signature, and this would say so.
    """
    fake = scripted(
        Reply(status=429, body=error_body(100410), headers={"Retry-After": "1"}),
        Reply(body=balances_body([held("KAS", "1500")])),
    )

    result = await fetch_balances(fake, clock=TickingClock())

    assert pairs(result) == [("KAS", Decimal(1500))]
    first, second = fake.balance_queries()
    assert first == second
    assert fake.signature_failures == []
    assert len(fake.balance_verified) == 2


async def test_a_replayed_balance_request_refused_as_stale_is_unavailable() -> None:
    """Past the venue's five-second window a replay is refused with `100421`: the next run
    signs a fresh one, so it is unavailable, never auth."""
    fake = scripted(Reply(status=503, body=HTML_BODY), Reply(body=error_body(100421)))

    error = await refused(fake)

    assert type(error) is ExchangeUnavailableError
    assert error.venue_code == "100421"
    assert len(fake.balance_requests) == 2


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param(
            Reply(
                body=balances_body([held("KAS", "1500")]),
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
async def test_a_hostile_header_on_a_balance_answer_ends_in_one_of_the_seven(
    reply: Reply, expected: type[ExchangeError] | None
) -> None:
    """A header the transport reads on every response never escapes as a bare `ValueError`."""
    fake = scripted(reply)

    if expected is None:
        assert pairs(await fetch_balances(fake)) == [("KAS", Decimal(1500))]
        return
    error = await refused(fake)
    assert type(error) is expected


#: Every hostile answer the tables above name, as a whole body: the envelopes without a
#: list, the bodies that are no envelope, and a list holding one entry or element of each
#: refused kind. All carry a marker.
HOSTILE_BODIES: Final[dict[str, str]] = {
    **{f"envelope: {case}": body for case, body in NO_BALANCES_ARRAY.items()},
    **{f"body: {case}": body for case, body in NOT_THE_ENVELOPE.items()},
    **{
        f"free: {case}": balances_body([marked(free=fragment)])
        for case, fragment in NOT_AN_AMOUNT.items()
    },
    **{
        f"asset: {case}": balances_body([marked(asset=fragment)])
        for case, fragment in NOT_A_BASE_ASSET.items()
    },
    **{
        f"element: {case}": balances_body([marked(), fragment])
        for case, fragment in NOT_AN_ENTRY.items()
    },
    "locked: missing": balances_body([marked(locked=None)]),
    "locked: negative": balances_body([marked(locked='"-4242.4242"')]),
    "locked: finer than the column": balances_body([marked(locked='"0.0000424242424242424"')]),
    "locked: too large": balances_body([marked(locked='"424242424242424242424"')]),
    "an asset named twice": balances_body([marked(), marked()]),
}


@pytest.mark.parametrize("status", [200, 302, 400, 500])
@pytest.mark.parametrize("case", list(HOSTILE_BODIES))
async def test_whatever_a_hostile_answer_is_only_the_seven_classes_escape(
    case: str, status: int
) -> None:
    """Every hostile body above, under a success, a redirect, a refusal and an outage.

    None of them is ever read as balances. Each ends in one of the seven classes -- any
    other exception escapes `refused` and fails here -- and none carries what the body held.
    On a 200 every one of them is a schema error: an answer not understood, never an auth
    failure that would stop later reads, and never an outage that would be retried.
    """
    fake = scripted(Reply(status=status, body=HOSTILE_BODIES[case]))

    error = await refused(fake)

    assert type(error) in SEVEN_CLASSES
    assert isinstance(error, ExchangeError)
    assert error.status in (status, None)
    assert_carries_no_value(error)
    if status == 200:
        assert type(error) is ExchangeSchemaError
    assert fake.requests == []
    assert fake.balance_verified == fake.all_requests


# -- the interpreter's own limits ---------------------------------------------------------

EXPONENT_PAST_THE_LIMIT: Final = "1e1000000000000000000"

#: Far past any scanner's recursion on any platform (`test_bingx.py` gives the measurements).
UNREADABLY_DEEP: Final = 200_000


def past_the_integer_digit_limit() -> str:
    """A JSON integer with one digit more than this process will convert, read not assumed."""
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        message = "This interpreter has no integer string conversion limit to exceed."
        raise RuntimeError(message)
    return "4" * (limit + 1)


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


INTERPRETER_LIMIT_ENTRIES: Final[dict[str, Callable[[], VenueBalance]]] = {
    "free, a 5000-digit string": lambda: marked(free='"' + "4242" * 1250 + '"'),
    "locked, a number with 5000 places": lambda: marked(locked="0." + "4242" * 1250),
    "free, an integer past the digit limit": lambda: marked(free=past_the_integer_digit_limit()),
    "asset, an integer past the digit limit": lambda: marked(asset=past_the_integer_digit_limit()),
    "a field nested past any scanner": lambda: marked(nested=nested(UNREADABLY_DEEP)),
    "free, an exponent Decimal cannot hold, as a number": lambda: marked(
        free=EXPONENT_PAST_THE_LIMIT
    ),
    "locked, an exponent Decimal cannot hold, as a string": lambda: marked(
        locked=f'"{EXPONENT_PAST_THE_LIMIT}"'
    ),
    "free, a tiny exponent": lambda: marked(free="1e-1000000000000000000"),
    "free, a negative tiny exponent": lambda: marked(free="-1e-1000000000000000000"),
    "asset, a lone surrogate": lambda: marked(asset=f'"{json_escape(0xD800)}"'),
}


def test_the_limit_cases_include_bodies_the_decoder_itself_refuses() -> None:
    """The premise: three of the cases never reach the parser, so they test the envelope's
    handling of a body that is JSON to the eye and not to the decoder."""
    for case in (
        "free, an integer past the digit limit",
        "a field nested past any scanner",
        "free, an exponent Decimal cannot hold, as a number",
    ):
        with pytest.raises(ProviderResponseError):
            decode_json(balances_body([INTERPRETER_LIMIT_ENTRIES[case]()]))
    decode_json(balances_body([marked(nested=nested(1500))]))


@pytest.mark.parametrize("case", sorted(INTERPRETER_LIMIT_ENTRIES))
async def test_the_interpreter_limits_are_schema_errors_on_a_balance_read(case: str) -> None:
    """Integer digits, recursion depth, `Decimal`'s exponent and UTF-8: each a schema error,
    never the interpreter's own `ValueError`, `RecursionError` or `InvalidOperation`."""
    fake = FakeBingX(balances=[held("KAS", "5"), INTERPRETER_LIMIT_ENTRIES[case]()])

    error = await refused(fake)

    assert type(error) is ExchangeSchemaError
    assert_carries_no_value(error)
    assert_one_signed_balance_request(fake)


async def test_a_deeply_nested_extra_field_is_ignored() -> None:
    """The companion: a balance is not recorded as a payload, so a field nobody reads may
    nest as deep as the decoder allows and changes nothing."""
    fake = FakeBingX(balances=[held("KAS", "1", "2", nested=nested(1500))])

    assert pairs(await fetch_balances(fake)) == [("KAS", Decimal(3))]


# --------------------------------------------------------------------------------------
# Hypothesis: any account, and anything at all
# --------------------------------------------------------------------------------------

ASSET_NAMES: Final = ["BTC", "ETH", "KAS", "USDT", "USDC", "1INCH", "kas", "$U", "A", "ZZ9"]


def exact_decimal(units: int, places: int) -> Decimal:
    """`units * 10**-places`, built from the digits: no arithmetic, no context, no float."""
    return Decimal((0, tuple(int(digit) for digit in str(units)), -places))


def eight_places(units: int) -> str:
    """`units * 10**-8` written as a venue writes an amount, by integer division alone."""
    return f"{units // 10**8}.{units % 10**8:08d}"


HELD_UNITS: Final = st.one_of(st.just(0), st.integers(min_value=0, max_value=10**15 - 1))


@settings(max_examples=200, deadline=None)
@given(
    account=st.dictionaries(st.sampled_from(ASSET_NAMES), st.tuples(HELD_UNITS, HELD_UNITS)),
    data=st.data(),
)
def test_any_account_reads_as_its_exact_totals_without_its_zeros_sorted(
    account: dict[str, tuple[int, int]], data: st.DataObject
) -> None:
    """Amounts of at most fifteen significant digits, so the decode changes nothing: every
    total is `free + locked` exactly, an asset holding nothing is absent, and the rest are
    in asset order whatever order the venue listed them in.

    The oracle is integer arithmetic on hundred-millionths.
    """
    listed = data.draw(st.permutations(sorted(account)))
    entries = [
        held(asset, eight_places(account[asset][0]), eight_places(account[asset][1]))
        for asset in listed
    ]

    result = checked(
        parse_balances(unwrap_envelope(200, balances_body(entries), member="balances"))
    )

    assert pairs(result) == [
        (asset, exact_decimal(free + locked, 8))
        for asset, (free, locked) in sorted(account.items())
        if free + locked != 0
    ]


def written(coefficient: int, places: int) -> str:
    """`coefficient * 10**-places` in plain notation, by string slicing alone."""
    digits = str(coefficient)
    if places == 0:
        return digits
    if places >= len(digits):
        return "0." + "0" * (places - len(digits)) + digits
    return f"{digits[:-places]}.{digits[-places:]}"


def half_even_to_fifteen(coefficient: int) -> int:
    """A seventeen-digit coefficient rounded to its first fifteen digits, half to even."""
    quotient, remainder = divmod(coefficient, 100)
    if remainder > 50 or (remainder == 50 and quotient % 2 == 1):
        quotient += 1
    return quotient


SEVENTEEN_DIGITS: Final = st.integers(min_value=10**16, max_value=10**17 - 1)
PLACES: Final = st.integers(min_value=0, max_value=20)


def test_the_integer_oracle_agrees_with_the_hand_worked_vectors() -> None:
    """The control on the property below: its oracle reproduces the table worked by hand."""
    assert written(24418616265388994, 14) == "244.18616265388994"
    assert half_even_to_fifteen(24418616265388994) == 244186162653890
    assert written(12340000000000001, 21) == "0.000012340000000000001"
    assert half_even_to_fifteen(12340000000000001) == 123400000000000
    assert half_even_to_fifteen(12345678901234450) == 123456789012344
    assert half_even_to_fifteen(12345678901234550) == 123456789012346
    assert exact_decimal(244186162653890, 12) == Decimal("244.18616265389")


@settings(max_examples=300, deadline=None)
@given(free=SEVENTEEN_DIGITS, free_places=PLACES, locked=SEVENTEEN_DIGITS, locked_places=PLACES)
def test_any_two_seventeen_digit_parts_are_each_decoded_and_then_added(
    free: int, free_places: int, locked: int, locked_places: int
) -> None:
    """R1 for any pair of doubles: each part rounded to fifteen significant digits, half to
    even, on its own, and the two results added exactly.

    The oracle works in units of `10**-18`. A seventeen-digit coefficient at up to twenty
    places has, once its last two digits are rounded away, at most eighteen places, so
    every expected total is storable and none is refused.
    """
    entry = held("KAS", written(free, free_places), written(locked, locked_places))
    expected_units = half_even_to_fifteen(free) * 10 ** (20 - free_places) + half_even_to_fifteen(
        locked
    ) * 10 ** (20 - locked_places)

    result = checked(parse_balances(decode_json(balances_fragment([entry]))))

    assert pairs(result) == [("KAS", exact_decimal(expected_units, 18))]


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
            '"KAS"',
            '"kas"',
            '"244.18616265388994"',
            '"0.000012340000000000001"',
            '"0.0000123456789012345"',
            '"-0.1"',
            '"99999999999999999999"',
            '"NaN"',
            f'"{json_escape(0xD800)}"',
            f'"{json_escape(0x0001)}"',
            EXPONENT_PAST_THE_LIMIT,
            f'"{EXPONENT_PAST_THE_LIMIT}"',
        ]
    ),
    st.integers().map(str),
    st.decimals(allow_nan=False, allow_infinity=False).map(str),
    st.decimals(allow_nan=False, allow_infinity=False).map(lambda value: f'"{value}"'),
    QUOTABLE_TEXT.map(lambda text: f'"{text}"'),
)

FIELD_NAMES: Final = st.sampled_from(["asset", "free", "locked", "extra"])
EDITS: Final = st.lists(st.tuples(FIELD_NAMES, st.one_of(st.none(), FRAGMENTS)), max_size=3)


def outcome_of(call: Callable[[], object]) -> object:
    """What `call` returned, or the exchange error it raised. Anything else escapes."""
    try:
        return call()
    except ExchangeError as error:
        return error


def _read_like_the_provider(status: int, body: str | bytes) -> tuple[AssetBalance, ...]:
    """Every step of `fetch_balances` after the response arrives, synchronously."""
    return parse_balances(unwrap_envelope(status, body, member="balances"))


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    first=EDITS,
    second=EDITS,
    extra=st.one_of(st.none(), FRAGMENTS),
    status=st.sampled_from([200, 200, 200, 400, 401, 403, 418, 429, 500, 503, 302]),
)
def test_whatever_the_venue_sends_as_balances_only_the_seven_classes_escape(
    first: list[tuple[str, str | None]],
    second: list[tuple[str, str | None]],
    extra: str | None,
    status: int,
) -> None:
    """Two documented entries with up to three fields each replaced by anything at all, and
    perhaps a third element that is anything at all.

    Either balances come back, and they are what the contract promises -- `Decimal`s above
    zero, one per asset, sorted -- or one of the seven classes is raised: never a
    `KeyError`, a `TypeError`, an interpreter's `ValueError`, a `decimal` signal or the
    decoder's `ProviderResponseError`.
    """
    entries: list[Entry] = [
        VenueBalance(asset="KAS", free="1500.25", locked="0", overrides=dict(first)),
        VenueBalance(
            asset="USDT",
            free="16.73971130673954",
            locked="244.18616265388994",
            overrides=dict(second),
        ),
    ]
    if extra is not None:
        entries.append(extra)
    body = balances_body(entries)

    outcome = outcome_of(lambda: _read_like_the_provider(status, body))

    if isinstance(outcome, ExchangeError):
        assert type(outcome) in SEVEN_CLASSES
        return
    assert status == 200
    result = checked(outcome)
    assets = [entry.asset for entry in result]
    assert assets == sorted(set(assets))
    assert all(entry.quantity > 0 for entry in result)


@settings(max_examples=200, deadline=None)
@given(status=st.integers(min_value=100, max_value=599), body=st.binary(max_size=64))
def test_any_status_with_any_body_is_one_of_the_seven_or_a_balances_list(
    status: int, body: bytes
) -> None:
    outcome = outcome_of(lambda: unwrap_envelope(status, body, member="balances"))

    if isinstance(outcome, ExchangeError):
        assert type(outcome) in SEVEN_CLASSES
    else:
        assert status == 200, "only a 200 can be a success"
        assert isinstance(outcome, list)


@settings(max_examples=200, deadline=None)
@given(code=st.one_of(st.integers(), FRAGMENTS), status=st.sampled_from([200, 400, 418, 503]))
def test_any_code_in_a_balance_envelope_is_one_of_the_seven_or_the_integer_zero(
    code: int | str, status: int
) -> None:
    """Only `code: 0` on a 200 is a success; every other code is classified, never raised raw."""
    body = f'{{"code":{code},"msg":"","data":{{"balances":[]}}}}'

    outcome = outcome_of(lambda: _read_like_the_provider(status, body))

    if isinstance(outcome, ExchangeError):
        assert type(outcome) in SEVEN_CLASSES
    else:
        assert outcome == ()
        assert status == 200
        document = decode_json(body)
        assert isinstance(document, dict)
        assert type(document["code"]) is int
        assert document["code"] == 0
