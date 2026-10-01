"""Criteria 1 and 2 of #104 (spec 025), for Bitget: the balance read, against a fake venue.

`BitgetProvider.fetch_balances` sends one signed GET, and `bitget.parse_balances` turns its
answer into what the seam promises: one entry per asset, the total held, zeros left out,
sorted by asset. `test_balances_seam.py` tests `AssetBalance` and `assemble_balances` on
their own. This module shows that Bitget gets there, and pins what is Bitget's alone: the
request, which three fields make the total, and the upper-casing of a coin's name.

Where each expected value comes from, none of it the code under test:

* **the request** is written as literals from Bitget's Get Account Assets page, as
  `docs/providers.md` records it (read 2026-10-01): the path, the query, the header names;
* **the signature** is a golden vector computed with `openssl`, the command beside it, and
  recomputed in the test with `hmac`, `hashlib` and `base64`. The fake venue verifies every
  request the same way, over the bytes on the wire, so a signing mistake fails every test
  that makes a request;
* **the totals** are sums done by hand and written as literals, and in the property at the
  end they are integer additions, which no decimal context can round;
* **the bodies** are hand-written text. An amount in them never passed through a Python
  `float` or `Decimal` on its way into the body.

The request rules are tested through `fetch_balances` and the fake, because that is what the
sync calls. The parsing rules are tested on `parse_balances` alone, over `data` decoded by
the decoder the provider uses, because thousands of bodies gain nothing from HTTP.

**No refusal may name what the owner holds.** Every refusal in this module is built around
a marked coin and marked amounts, and the two helpers that collect a refusal search its
text, its repr, its arguments and every exception linked to it for them. So "no value in any
message" is asserted for each refusal below and not only by the test named after it.
"""

from __future__ import annotations

import base64
import decimal
import gzip
import hashlib
import hmac
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx
import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from portfolio.providers.base import decode_json
from portfolio.providers.exchanges import bitget
from portfolio.providers.exchanges.base import AssetBalance
from portfolio.providers.exchanges.bitget import (
    ASSETS_PATH,
    ASSETS_QUERY,
    BITGET_ERROR_MAP,
    build_prehash,
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
from portfolio.providers.exchanges.signing import hmac_sha256_base64
from portfolio.providers.http import request_target
from tests.providers.exchanges.bitget_harness import (
    ACCESS_KEY_SENTINEL,
    GOLDEN_NOW,
    GOLDEN_TIMESTAMP_MS,
    HTML_BODY,
    PHRASE_SENTINEL,
    SIGNING_SENTINEL,
    FakeBitget,
    FixedClock,
    Reply,
    asset_entry,
    assets_body,
    bitget_client,
    bitget_provider,
    envelope,
    error_body,
    fetch_balances,
)
from tests.providers.harness import RecordingSleep

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence
    from datetime import datetime

#: The seven classes the seam promises, and nothing else. `ExchangeError` itself is the
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

# --------------------------------------------------------------------------------------
# Marked holdings, and what a refusal may not say
# --------------------------------------------------------------------------------------

#: A coin no venue lists and three amounts nobody holds, so their absence means something.
#: The coin is lower-case, as the documented sample spells one; the asset is what the
#: provider would call it.
MARK_COIN: Final = "zzmark"
MARK_ASSET: Final = "ZZMARK"
MARK_AVAILABLE: Final = "4242.4242"
MARK_FROZEN: Final = "7373.7373"
MARK_LOCKED: Final = "9191.9191"
#: 4242.4242 + 7373.7373 = 11616.1615, and 11616.1615 + 9191.9191 = 20808.0806. By hand.
MARK_TOTAL: Final = "20808.0806"
#: What is searched for: the digits of each marked amount, and of their total.
MARK_DIGITS: Final = ("4242", "7373", "9191", "20808")

#: A lone surrogate as the JSON text a venue sends -- valid JSON, and no UTF-8 encoding.
#: Assembled from two pieces so that this file holds no escape an editor could expand.
SURROGATE_ESCAPE: Final = "\\" + "ud800"
LONE_SURROGATE: Final = chr(0xD800)
#: Fullwidth digits, built with `chr` so the source stays ASCII. `Decimal` reads them as 42.
FULLWIDTH_DIGITS: Final = chr(0xFF14) + chr(0xFF12)

#: The venue's refusal message, echoing the holdings and the key as a careless venue would.
ECHOING_MSG: Final = (
    f"coin {MARK_COIN} available {MARK_AVAILABLE} frozen {MARK_FROZEN} "
    f"apiKey {ACCESS_KEY_SENTINEL} is not valid"
)


def marked_entry(**overrides: str | None) -> str:
    """The marked coin holding the three marked amounts, fields replaced by raw fragments."""
    return asset_entry(
        MARK_COIN,
        available=MARK_AVAILABLE,
        frozen=MARK_FROZEN,
        locked=MARK_LOCKED,
        overrides=overrides,
    )


def every_link(error: BaseException) -> Iterator[BaseException]:
    """Every exception reachable by `__cause__` or `__context__`, suppressed or not."""
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
    """Everything of an exception that a log line, a traceback or a debugger could print.

    Its text, its repr and its arguments, and the same of every exception linked to it. A
    traceback prints the cause, and the context unless `from None` suppressed it; an error
    tracker walks a suppressed context anyway, so those are searched as well. That is
    stricter than a traceback and it costs nothing: every refusal here passes it.
    """
    return " | ".join(f"{link}{link!r}{link.args!r}" for link in every_link(error))


def assert_names_no_holding(error: BaseException) -> None:
    """Neither the marked coin, in either case, nor a marked amount, nor a credential."""
    text = rendered(error)
    assert MARK_COIN not in text.lower(), text
    for digits in MARK_DIGITS:
        assert digits not in text, text
    for sentinel in (ACCESS_KEY_SENTINEL, PHRASE_SENTINEL, SIGNING_SENTINEL):
        assert sentinel not in text, text


def data_of(*entries: str) -> object:
    """The `data` of an answer carrying `entries`, decoded as the provider decodes a body."""
    return decode_json("[" + ",".join(entries) + "]")


def parsed(*entries: str) -> tuple[AssetBalance, ...]:
    return parse_balances(data_of(*entries))


def refusal(data: object) -> ExchangeSchemaError:
    """`parse_balances(data)` must refuse. The refusal, held to both rules every one is.

    Exactly `ExchangeSchemaError`, so that the sync records `schema` and not an `internal`
    failure with a traceback, and naming nothing the owner holds. Anything else raised --
    a `KeyError`, a `decimal.InvalidOperation`, another of the seven -- escapes and fails
    the test.
    """
    with pytest.raises(ExchangeSchemaError) as caught:
        parse_balances(data)
    assert type(caught.value) is ExchangeSchemaError
    assert_names_no_holding(caught.value)
    return caught.value


async def refused_read(fake: FakeBitget) -> ExchangeError:
    """The exchange error one balance read raises. Anything else escapes and fails the test.

    Held to the rules every failure of the read is: one of the seven classes, naming
    nothing, after a request to the assets endpoint and to no other.
    """
    with pytest.raises(ExchangeError) as caught:
        await fetch_balances(fake)
    error = caught.value
    assert type(error) in SEVEN_CLASSES
    assert_names_no_holding(error)
    assert fake.asset_requests, "the positive companion: the read was attempted"
    assert fake.fill_requests == []
    assert fake.symbol_requests == []
    for request in fake.asset_requests:
        assert request.headers["ACCESS-SIGN"] not in rendered(error)
    return error


def pairs(balances: Sequence[AssetBalance]) -> list[tuple[str, str]]:
    """Each balance as its asset and its quantity as text: digits and exponent, not only value."""
    return [(entry.asset, str(entry.quantity)) for entry in balances]


def the_quantity(balances: Sequence[AssetBalance]) -> Decimal:
    """The quantity of the one balance read, which must be exactly a `Decimal`."""
    (entry,) = balances
    assert type(entry.quantity) is Decimal
    return entry.quantity


def exact(value: Decimal, text: str) -> bool:
    """Equal in value **and** in digits and exponent: `0.6` is not `0.60`."""
    return value == Decimal(text) and value.as_tuple() == Decimal(text).as_tuple()


class TickingClock:
    """A clock one second later on every read."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return GOLDEN_NOW + timedelta(seconds=self.reads)


def test_the_marked_total_is_the_sum_of_the_marked_parts() -> None:
    """The premise of the absence checks, in integer arithmetic: units of one ten-thousandth."""
    assert 42424242 + 73737373 + 91919191 == 208080806
    parts = [int(part.replace(".", "")) for part in (MARK_AVAILABLE, MARK_FROZEN, MARK_LOCKED)]
    assert parts == [42424242, 73737373, 91919191]
    assert int(MARK_TOTAL.replace(".", "")) == 208080806
    assert all(any(digits in amount for digits in MARK_DIGITS) for amount in parts_text())


def parts_text() -> tuple[str, ...]:
    """The three marked amounts and their total, as the text a message could quote."""
    return (MARK_AVAILABLE, MARK_FROZEN, MARK_LOCKED, MARK_TOTAL)


def test_the_surrogate_escape_decodes_to_a_lone_surrogate() -> None:
    """The premise of every surrogate case: the body is ASCII and the decoder accepts it."""
    body = assets_body(asset_entry(SURROGATE_ESCAPE))

    assert body.isascii()
    assert decode_json(f'"{SURROGATE_ESCAPE}"') == LONE_SURROGATE
    with pytest.raises(UnicodeEncodeError):
        LONE_SURROGATE.encode("utf-8")


# --------------------------------------------------------------------------------------
# Criterion 1: the request
# --------------------------------------------------------------------------------------

#: Get Account Assets, as documented, and the one query this application sends with it.
ASSETS_TARGET: Final = "/api/v2/spot/account/assets?assetType=hold_only"
ASSETS_URL: Final = "https://api.bitget.com/api/v2/spot/account/assets?assetType=hold_only"

GOLDEN_PREHASH: Final = "1684814440729GET/api/v2/spot/account/assets?assetType=hold_only"
GOLDEN_SIGNATURE: Final = "12MYV8SuUneFlibwU0KcKzaGBh+XFELuCO/+0UMIXq4="
r"""Computed outside this code, with OpenSSL 3.5.4, which printed exactly the literal above:

    printf '%s' '1684814440729GET/api/v2/spot/account/assets?assetType=hold_only' \
      | openssl dgst -sha256 -hmac 'dummy-secret-not-a-real-key' -binary | openssl base64 -A

The timestamp is `GOLDEN_TIMESTAMP_MS` and the secret is `SIGNING_SENTINEL`, the synthetic
pair the fills vectors in `test_bitget.py` use.
"""


def standard_library_signature(secret: str, prehash: str) -> str:
    """HMAC-SHA256 in Base64, with `hmac`, `hashlib` and `base64` and nothing of ours."""
    digest = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def one_holding() -> FakeBitget:
    return FakeBitget(assets=[asset_entry("kas", available="1500")])


def test_the_assets_endpoint_is_the_documented_one() -> None:
    """Pinned as literals, once. A typo in either is a request the venue refuses."""
    assert ASSETS_PATH == "/api/v2/spot/account/assets"
    assert ASSETS_QUERY == "assetType=hold_only"
    for name in ("ASSETS_PATH", "ASSETS_QUERY", "parse_balances"):
        assert name in bitget.__all__


async def test_a_balance_read_is_exactly_one_get_of_the_documented_url() -> None:
    """One request, to the assets endpoint, with the documented query and no body.

    No fills request and no symbol request: a balance needs neither, and each would be a
    signed call, or a public one, made for nothing.
    """
    fake = one_holding()

    balances = await fetch_balances(fake)

    assert pairs(balances) == [("KAS", "1500")]
    (request,) = fake.requests
    assert fake.asset_requests == [request]
    assert fake.fill_requests == []
    assert fake.symbol_requests == []
    assert request.method == "GET"
    assert str(request.url) == ASSETS_URL
    assert request.url.raw_path == ASSETS_TARGET.encode("ascii")
    assert request.url.query == b"assetType=hold_only"
    assert request.content == b""


async def test_the_query_sent_is_byte_for_byte_the_query_signed() -> None:
    """The signature verifies over `raw_path`: the path and the query as they went out.

    Recomputed here from the bytes the request carried, with the standard library, so a
    provider that signed one query and sent another would fail whatever both looked like.
    """
    fake = one_holding()

    await fetch_balances(fake)

    (request,) = fake.asset_requests
    on_the_wire = request.url.raw_path.decode("ascii")
    timestamp = request.headers["ACCESS-TIMESTAMP"]
    assert on_the_wire == ASSETS_TARGET
    assert request.headers["ACCESS-SIGN"] == standard_library_signature(
        SIGNING_SENTINEL, f"{timestamp}GET{on_the_wire}"
    )
    assert fake.verified_asset_requests == [request]
    assert fake.signature_failures == []


def test_the_golden_vector_is_what_the_standard_library_computes() -> None:
    """The literal, the pre-hash and the shared signer agree, each checked against the other.

    The pre-hash is `timestamp + "GET" + path + "?" + query`, the body empty for a GET, as
    the REST introduction documents it.
    """
    assert f"{GOLDEN_TIMESTAMP_MS}GET{ASSETS_TARGET}" == GOLDEN_PREHASH
    assert standard_library_signature(SIGNING_SENTINEL, GOLDEN_PREHASH) == GOLDEN_SIGNATURE
    assert (
        build_prehash(GOLDEN_TIMESTAMP_MS, "/api/v2/spot/account/assets", "assetType=hold_only")
        == GOLDEN_PREHASH
    )
    assert hmac_sha256_base64(SecretStr(SIGNING_SENTINEL), GOLDEN_PREHASH) == GOLDEN_SIGNATURE


async def test_the_balance_request_on_the_wire_carries_the_golden_signature() -> None:
    """A fixed clock and the synthetic secret: the signature sent is the `openssl` literal."""
    clock = FixedClock()
    fake = one_holding()

    await fetch_balances(fake, clock=clock)

    (request,) = fake.asset_requests
    assert request.headers["ACCESS-TIMESTAMP"] == "1684814440729"
    assert request.headers["ACCESS-SIGN"] == GOLDEN_SIGNATURE
    assert clock.reads == 1, "one signed request reads the clock once"


async def test_the_credentials_travel_in_the_four_access_headers_and_never_in_the_url() -> None:
    """Bitget signs in headers. Nothing credential-like is in the path or the query.

    The header names are read off the raw header list, so the spelling asserted is the one
    on the wire, and the documented `Content-Type` and `locale` ride along as they do on a
    fills request.
    """
    fake = one_holding()

    await fetch_balances(fake)

    (request,) = fake.asset_requests
    assert request.headers["ACCESS-KEY"] == ACCESS_KEY_SENTINEL
    assert request.headers["ACCESS-PASSPHRASE"] == PHRASE_SENTINEL
    assert request.headers["ACCESS-TIMESTAMP"] == str(GOLDEN_TIMESTAMP_MS)
    signature = request.headers["ACCESS-SIGN"]
    assert signature
    sent_names = {name.decode("ascii") for name, _value in request.headers.raw}
    assert {"ACCESS-KEY", "ACCESS-SIGN", "ACCESS-TIMESTAMP", "ACCESS-PASSPHRASE"} <= sent_names
    assert request.headers.get("Content-Type") == "application/json"
    assert request.headers.get("locale") == "en-US"

    url = str(request.url)
    for secret in (ACCESS_KEY_SENTINEL, PHRASE_SENTINEL, SIGNING_SENTINEL, signature):
        assert secret not in url
    assert list(request.url.params.keys()) == ["assetType"]
    for word in ("sign", "key", "passphrase", "timestamp"):
        assert word not in url.lower()
    carried = " ".join(f"{name}: {value}" for name, value in request.headers.items())
    assert SIGNING_SENTINEL not in carried, "the secret signs; it is never sent"


async def test_the_fake_venue_refuses_a_balance_request_signed_with_another_secret() -> None:
    """The control on the verifier: it can say no, so its yes above means something."""
    fake = FakeBitget(
        assets=[asset_entry("kas", available="1500")],
        signing_key="another-secret-entirely-not-real",
    )

    error = await refused_read(fake)

    assert type(error) is ExchangeAuthError
    assert error.venue_code == "40009"
    assert len(fake.signature_failures) == len(fake.asset_requests) == 1
    assert "does not verify" in fake.signature_failures[0]
    assert fake.verified_asset_requests == []


async def test_the_balance_request_is_labelled_exchange_balances() -> None:
    """The label is the only thing about the request's target the transport logs.

    A label off `ENDPOINT_LABELS` renders `<unlabelled>`, so the rendered target is asserted
    too: it shows the label was added to the allowlist and not only passed along.
    """
    fake = one_holding()

    await fetch_balances(fake)

    (request,) = fake.asset_requests
    assert request.extensions.get("endpoint") == "exchange_balances"
    assert request_target(request) == "https://api.bitget.com/exchange_balances"


@pytest.mark.parametrize("count", [0, 1, 99, 100, 101, 250])
async def test_one_request_answers_the_whole_account_whatever_its_size(count: int) -> None:
    """No paging: the endpoint documents none, so nothing is asked twice and nothing is cut.

    A fills page is at most 100 fills and a longer one is refused. A balance answer has no
    such limit, and an account of 101 coins or of 250 is read whole, in one request with no
    `limit` and no cursor on it.
    """
    coins = [f"c{index:03d}" for index in range(count)]
    fake = FakeBitget(assets=[asset_entry(coin, available="1") for coin in reversed(coins)])

    balances = await fetch_balances(fake)

    assert len(fake.requests) == len(fake.asset_requests) == 1
    assert [entry.asset for entry in balances] == [coin.upper() for coin in coins]
    assert all(entry.quantity == 1 for entry in balances)
    (request,) = fake.asset_requests
    assert request.url.raw_path.decode("ascii") == ASSETS_TARGET


async def test_each_read_is_signed_anew_at_the_moment_it_is_made() -> None:
    """Two reads on one provider: two timestamps, two signatures, each verifying.

    A provider that kept the first signature would send a request the venue refuses thirty
    seconds later.
    """
    clock = TickingClock()
    fake = one_holding()

    async with bitget_client(fake) as client:
        provider = bitget_provider(client, clock=clock)
        first_read = await provider.fetch_balances()
        second_read = await provider.fetch_balances()

    assert pairs(first_read) == pairs(second_read) == [("KAS", "1500")]
    first, second = fake.asset_requests
    assert first.headers["ACCESS-TIMESTAMP"] != second.headers["ACCESS-TIMESTAMP"]
    assert first.headers["ACCESS-SIGN"] != second.headers["ACCESS-SIGN"]
    assert fake.verified_asset_requests == [first, second]
    assert fake.signature_failures == []


async def test_the_transport_replays_the_same_signed_balance_request() -> None:
    """A 429 then a 200: the retry resends the request as signed, and nothing really sleeps.

    The decision the fills call pins, held for the balance call too: the clock moves on
    every read, so a provider that re-signed per attempt would show two timestamps.
    """
    sleep = RecordingSleep()
    fake = FakeBitget(
        asset_replies=[
            Reply(status=429, body=error_body("429"), headers={"Retry-After": "1"}),
            Reply(body=assets_body(asset_entry("kas", available="1500"))),
        ]
    )

    balances = await fetch_balances(fake, clock=TickingClock(), sleep=sleep)

    assert pairs(balances) == [("KAS", "1500")]
    first, second = fake.asset_requests
    assert first.headers["ACCESS-TIMESTAMP"] == second.headers["ACCESS-TIMESTAMP"]
    assert first.headers["ACCESS-SIGN"] == second.headers["ACCESS-SIGN"]
    assert fake.signature_failures == []
    assert sleep.slept_ms, "the wait was asked of the injected sleep, not of the wall clock"


# --------------------------------------------------------------------------------------
# Criterion 2: a documented answer, read end to end
# --------------------------------------------------------------------------------------

#: The shape of the sample on the Get Account Assets page, as `docs/providers.md` records it:
#: one entry, its `coin` the lower-case `usdt`, every amount the string `"0"`. Written by
#: hand from that record, with the envelope the other samples show.
DOCUMENTED_SHAPE_BODY: Final = (
    '{"code":"00000","msg":"success","requestTime":1695808949356,"data":[{"coin":"usdt",'
    '"available":"0","frozen":"0","locked":"0","limitAvailable":"0","uTime":"1622697148"}]}'
)


async def test_the_documented_sample_is_an_account_that_holds_nothing() -> None:
    """Every amount in the sample is zero, and a zero is left out: nothing is held."""
    fake = FakeBitget(asset_replies=[Reply(body=DOCUMENTED_SHAPE_BODY)])

    balances = await fetch_balances(fake)

    assert balances == ()


def test_the_harness_renders_the_documented_fields_in_the_documented_order() -> None:
    """The control on the fixture: `asset_entry()` with no argument is the sample's entry.

    Only `requestTime` differs between the two bodies, and nothing reads it.
    """
    assert assets_body(asset_entry()) == DOCUMENTED_SHAPE_BODY.replace(
        '"requestTime":1695808949356', '"requestTime":1695865274510'
    )
    entry = decode_json(asset_entry())
    assert isinstance(entry, dict)
    assert list(entry) == ["coin", "available", "frozen", "locked", "limitAvailable", "uTime"]
    assert all(type(value) is str for value in entry.values()), "every field a string"


async def test_a_held_account_is_read_end_to_end() -> None:
    """Four coins as the venue would send them: totalled, upper-cased, sorted, the empty one out.

    By hand: `12.5 + 2.5 + 0` is `15.0`; `1000 + 0 + 0.000000000000000001` keeps its
    eighteenth place; `0.1 + 0.2 + 0.3` is `0.6`, which binary floating point cannot say;
    the `limitAvailable` of 5 is not part of it.
    """
    fake = FakeBitget(
        assets=[
            asset_entry("usdt", available="12.5", frozen="2.5"),
            asset_entry("Kas", available="1000", locked="0.000000000000000001"),
            asset_entry("btc", available="0", frozen="0.00000000", locked="0"),
            asset_entry("ETH", available="0.1", frozen="0.2", locked="0.3", limit_available="5"),
        ]
    )

    balances = await fetch_balances(fake)

    assert isinstance(balances, tuple)
    assert all(type(entry) is AssetBalance for entry in balances)
    assert all(type(entry.quantity) is Decimal for entry in balances)
    assert pairs(balances) == [
        ("ETH", "0.6"),
        ("KAS", "1000.000000000000000001"),
        ("USDT", "15.0"),
    ]


async def test_fetch_balances_answers_what_parse_balances_makes_of_the_data() -> None:
    """The two halves agree: the provider adds the request and the envelope, nothing else."""
    entries = [
        asset_entry("usdt", available="12.5", frozen="2.5"),
        marked_entry(),
        asset_entry("kas", overrides={"available": "0.1", "extra": '{"deep":[1,2,3]}'}),
    ]

    balances = await fetch_balances(FakeBitget(assets=entries))

    assert balances == parsed(*entries)
    assert pairs(balances) == [("KAS", "0.1"), ("USDT", "15.0"), (MARK_ASSET, MARK_TOTAL)]


# --------------------------------------------------------------------------------------
# Criterion 2: the total
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("available", "frozen", "locked", "total"),
    [
        pytest.param("0.1", "0.2", "0.3", "0.6", id="tenths a float cannot add"),
        pytest.param("7", "0", "0", "7", id="available alone"),
        pytest.param("0", "7", "0", "7", id="frozen alone"),
        pytest.param("0", "0", "7", "7", id="locked alone"),
        pytest.param("1", "20", "300", "321", id="each part in its own digit"),
        pytest.param(MARK_AVAILABLE, MARK_FROZEN, MARK_LOCKED, MARK_TOTAL, id="marked"),
        pytest.param(
            "0.000000000000000001",
            "0.000000000000000001",
            "0.000000000000000001",
            "0.000000000000000003",
            id="three units at the eighteenth place",
        ),
        pytest.param(
            "12345678.123456789012345678",
            "0.876543210987654322",
            "1",
            "12345680.000000000000000000",
            id="eighteen places carrying into the integer part",
        ),
        pytest.param(
            "99999999999999999999",
            "0.000000000000000001",
            "0",
            "99999999999999999999.000000000000000001",
            id="thirty-eight significant digits",
        ),
    ],
)
def test_the_total_is_available_plus_frozen_plus_locked(
    available: str, frozen: str, locked: str, total: str
) -> None:
    """Exact, at any length. Each total above was added by hand.

    A total that dropped a part, or doubled one, differs in every row but one; the three
    "alone" rows show that each part is read from its own field.
    """
    balances = parsed(asset_entry("kas", available=available, frozen=frozen, locked=locked))

    assert exact(the_quantity(balances), total)


@pytest.mark.parametrize("precision", [1, 6, 28])
def test_the_total_is_exact_whatever_the_calling_threads_decimal_context(precision: int) -> None:
    """A plain `+` rounds to the ambient precision; the total may not.

    Thirty-eight significant digits, read under a context that keeps one, six or the
    interpreter's default of twenty-eight: every digit survives.
    """
    entry = asset_entry("kas", available="99999999999999999999", frozen="0.000000000000000001")
    data = data_of(entry)

    with decimal.localcontext() as context:
        context.prec = precision
        balances = parse_balances(data)

    assert exact(the_quantity(balances), "99999999999999999999.000000000000000001")


@pytest.mark.parametrize(
    "limit_available",
    ["0", "1000", "0.000000000000000001", "0.6", "99999999999999999999"],
)
def test_limit_available_is_not_added(limit_available: str) -> None:
    """Whether it is part of `available` or beside it is not documented, so it is left out.

    Adding it could count the same units twice, and under-reading is the safe side of a
    comparison that treats what is held as a lower bound.
    """
    balances = parsed(
        asset_entry(
            "kas", available="0.1", frozen="0.2", locked="0.3", limit_available=limit_available
        )
    )

    assert exact(the_quantity(balances), "0.6")


def test_an_entry_whose_only_non_zero_field_is_limit_available_is_dropped() -> None:
    """With `limitAvailable` out of the total such an entry holds nothing, and is left out."""
    balances = parsed(
        asset_entry(MARK_COIN, limit_available=MARK_AVAILABLE),
        asset_entry("kas", available="1"),
    )

    assert pairs(balances) == [("KAS", "1")]


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param(None, id="absent"),
        pytest.param("null", id="null"),
        pytest.param("true", id="a boolean"),
        pytest.param('"abc"', id="letters"),
        pytest.param('"-5"', id="negative"),
        pytest.param("-5", id="a negative number"),
        pytest.param("{}", id="an object"),
        pytest.param('"0.0000000000000000001"', id="nineteen places"),
        pytest.param('"' + "9" * 5000 + '"', id="5000 digits"),
        pytest.param(f'"{SURROGATE_ESCAPE}"', id="a lone surrogate"),
    ],
)
@pytest.mark.parametrize("field", ["limitAvailable", "uTime"])
def test_limit_available_and_utime_are_not_read(field: str, fragment: str | None) -> None:
    """Neither is part of a balance, so whatever either holds cannot fail the read.

    Stronger than "not added": a `limitAvailable` that was parsed and discarded would still
    refuse the whole account for a value nobody uses.
    """
    balances = parsed(asset_entry("kas", available="0.1", overrides={field: fragment}))

    assert pairs(balances) == [("KAS", "0.1")]


def test_an_unexpected_extra_field_is_tolerated() -> None:
    """A field the page does not list is the venue growing, not a different answer."""
    balances = parsed(
        asset_entry(
            "kas",
            available="0.1",
            overrides={
                "accountType": '"spot"',
                "nested": '{"breakdown":[{"available":"999"}]}',
                "total": '"999"',
                "balance": "999",
            },
        )
    )

    assert pairs(balances) == [("KAS", "0.1")]


# --------------------------------------------------------------------------------------
# Criterion 2: amounts are never a float
# --------------------------------------------------------------------------------------


def test_the_result_is_a_tuple_of_asset_balances_with_decimal_quantities() -> None:
    balances = parsed(
        asset_entry("kas", available="0.1"),
        asset_entry("btc", overrides={"available": "0.1"}),
        asset_entry("eth", overrides={"available": "2"}),
    )

    assert type(balances) is tuple
    assert [type(entry) for entry in balances] == [AssetBalance] * 3
    assert [type(entry.quantity) for entry in balances] == [Decimal] * 3
    assert [type(entry.asset) for entry in balances] == [str] * 3


def test_a_bare_json_number_arrives_as_an_exact_decimal_and_never_as_a_float() -> None:
    """The house rule of `require_fill_amount`, pinned for a balance.

    The page says every field is a string. A venue that sent a JSON *number* instead is
    still read, because `decode_json` builds a `Decimal` from the number's literal text
    (`parse_float=Decimal`) and an `int` from an integer, so the digits on the wire arrive
    intact. `0.1` is exactly `Decimal("0.1")`, which `Decimal(0.1)` -- the float -- is not,
    and three bare tenths add to exactly `0.6`.
    """
    as_number = parsed(asset_entry("kas", overrides={"available": "0.1"}))
    as_integer = parsed(asset_entry("kas", overrides={"available": "2"}))
    with_exponent = parsed(asset_entry("kas", overrides={"available": "1.5e1"}))
    three_tenths = parsed(
        asset_entry("kas", overrides={"available": "0.1", "frozen": "0.2", "locked": "0.3"})
    )

    assert exact(the_quantity(as_number), "0.1")
    assert the_quantity(as_number) != Decimal(0.1)  # noqa: RUF032 - the float is the point
    assert the_quantity(as_integer) == Decimal(2)
    assert the_quantity(with_exponent) == Decimal(15)
    assert exact(the_quantity(three_tenths), "0.6")
    assert the_quantity(three_tenths) != Decimal(0.1 + 0.2 + 0.3)


def test_a_float_built_by_some_other_parser_is_refused() -> None:
    """`decode_json` cannot produce a float. A caller that built one anyway is refused."""
    error = refusal([{"coin": "kas", "available": 1.5, "frozen": "0", "locked": "0"}])

    assert "available" in str(error)


# --------------------------------------------------------------------------------------
# Criterion 2: zeros, order, one entry per asset
# --------------------------------------------------------------------------------------

ZEROS: Final = [
    pytest.param('"0"', id="0"),
    pytest.param('"0.0"', id="0.0"),
    pytest.param('"0.00000000"', id="eight places"),
    pytest.param('"0.000000000000000000"', id="eighteen places"),
    pytest.param('"0.00000000000000000000"', id="twenty places"),
    pytest.param('"0e-18"', id="an exponent"),
    pytest.param('"-0"', id="negative zero"),
    pytest.param('"-0.00"', id="negative zero with places"),
    pytest.param("0", id="a bare 0"),
    pytest.param("0.0", id="a bare 0.0"),
]


@pytest.mark.parametrize("zero", ZEROS)
def test_a_zero_balance_is_dropped_however_it_is_spelled(zero: str) -> None:
    """All three parts zero is "holds none of it", and that has one spelling: no entry."""
    empty = asset_entry(MARK_COIN, overrides={"available": zero, "frozen": zero, "locked": zero})

    assert parsed(empty) == ()
    assert pairs(parsed(empty, asset_entry("kas", available="1"))) == [("KAS", "1")]


@pytest.mark.parametrize("zero", ZEROS)
@pytest.mark.parametrize("field", ["available", "frozen", "locked"])
def test_one_non_zero_part_is_enough_to_be_held(field: str, zero: str) -> None:
    """The companion: the entry is dropped for its total, not for any one zero part."""
    parts = {"available": zero, "frozen": zero, "locked": zero}
    parts[field] = '"0.000000000000000001"'

    balances = parsed(asset_entry("kas", overrides=parts))

    quantity = the_quantity(balances)
    assert quantity == Decimal("0.000000000000000001")
    assert not quantity.is_signed()


def test_an_account_of_nothing_but_zeros_holds_nothing() -> None:
    assert parsed(asset_entry("usdt"), asset_entry("btc"), asset_entry("kas")) == ()


def test_the_balances_are_sorted_by_asset() -> None:
    """Sorted on the name as returned, upper-cased, whatever order the venue sent."""
    coins = ["zec", "kas", "ETH", "1inch", "btc", "aave", "Bgb"]

    balances = parsed(*(asset_entry(coin, available="1") for coin in coins))

    assert [entry.asset for entry in balances] == [
        "1INCH",
        "AAVE",
        "BGB",
        "BTC",
        "ETH",
        "KAS",
        "ZEC",
    ]


def test_the_order_does_not_depend_on_the_order_received() -> None:
    entries = [asset_entry(coin, available="1") for coin in ("kas", "btc", "usdt", "eth")]

    assert parsed(*entries) == parsed(*reversed(entries))
    assert [entry.asset for entry in parsed(*entries)] == ["BTC", "ETH", "KAS", "USDT"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(MARK_AVAILABLE, MARK_FROZEN, id="two holdings"),
        pytest.param(MARK_AVAILABLE, MARK_AVAILABLE, id="the same holding twice"),
        pytest.param(MARK_AVAILABLE, "0", id="a holding and a zero"),
        pytest.param("0", MARK_AVAILABLE, id="a zero and a holding"),
        pytest.param("0", "0", id="two zeros"),
    ],
)
def test_a_coin_named_twice_is_refused(first: str, second: str) -> None:
    """Refused, not summed and not last-one-wins, even when one of the two is empty.

    Whether a venue naming a coin twice means two parts of one holding or one holding listed
    twice cannot be told, and the two readings differ by the whole balance.
    """
    refusal(
        data_of(
            asset_entry("kas", available="5"),
            asset_entry(MARK_COIN, available=first),
            asset_entry("btc", available="0.25"),
            asset_entry(MARK_COIN, available=second),
        )
    )


def test_the_same_coins_named_once_each_are_accepted() -> None:
    """The companion: the refusal above is for the repeat, not for the entries around it."""
    balances = parsed(
        asset_entry("kas", available="5"),
        asset_entry(MARK_COIN, available=MARK_AVAILABLE),
        asset_entry("btc", available="0.25"),
    )

    assert pairs(balances) == [("BTC", "0.25"), ("KAS", "5"), (MARK_ASSET, MARK_AVAILABLE)]
    assert len({entry.asset for entry in balances}) == len(balances)


# --------------------------------------------------------------------------------------
# Criterion 2: the name is upper-cased, and what that makes collide
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("coin", "asset"),
    [
        ("usdt", "USDT"),
        ("Kas", "KAS"),
        ("BTC", "BTC"),
        ("1inch", "1INCH"),
        ("bGb", "BGB"),
        (MARK_COIN, MARK_ASSET),
    ],
)
def test_the_coin_is_upper_cased(coin: str, asset: str) -> None:
    """The sample spells `usdt`; the names on this venue's fills are `[A-Z0-9]`.

    The reconciliation joins the two by name, so one asset spelled two ways would be
    compared as two. Upper-casing is right whichever spelling the venue really uses.
    """
    balances = parsed(asset_entry(coin, available="1"))

    assert [entry.asset for entry in balances] == [asset]


@pytest.mark.parametrize(
    ("first", "second", "second_amount"),
    [
        pytest.param("btc", "BTC", "2", id="lower then upper"),
        pytest.param("BTC", "btc", "2", id="upper then lower"),
        pytest.param("Btc", "bTC", "2", id="two mixed spellings"),
        pytest.param("btc", "BTC", "0", id="the second one empty"),
        pytest.param(MARK_COIN, MARK_ASSET, MARK_AVAILABLE, id="marked"),
    ],
)
def test_two_spellings_of_one_coin_in_one_answer_are_refused_as_a_duplicate(
    first: str, second: str, second_amount: str
) -> None:
    """Equal once upper-cased is the same asset twice, and that is refused, never summed."""
    refusal(
        data_of(
            asset_entry(first, available="1"),
            asset_entry("kas", available="5"),
            asset_entry(second, available=second_amount),
        )
    )


#: R6 (spec 025): the one rule for a name the reconciliation joins on, written out here
#: rather than read off the source. One to forty characters, no whitespace anywhere, no
#: Unicode `C*` character -- and one message for every way of breaking it.
MAX_COIN_LENGTH: Final = 40
COIN_RULE: Final = (
    "coin must be a string of 1 to 40 characters, with no whitespace or control character"
)


def escape(code_point: int) -> str:
    """A JSON escape for `code_point`: a backslash, `u`, four hex digits. Keeps this file ASCII."""
    return f"{chr(92)}u{code_point:04x}"


@pytest.mark.parametrize(
    "coin",
    [
        pytest.param(" usdt ", id="padded on both sides"),
        pytest.param(" usdt", id="a leading space"),
        pytest.param("usdt ", id="a trailing space"),
        pytest.param("us dt", id="a space inside"),
        pytest.param("usdt\\t", id="a tab"),
        pytest.param("usdt\\n", id="a newline"),
        pytest.param("us" + escape(0x00A0) + "dt", id="a no-break space"),
        pytest.param("us" + escape(0x2003) + "dt", id="an em space"),
        pytest.param("us" + escape(0x3000) + "dt", id="an ideographic space"),
        pytest.param("us" + escape(0x0085) + "dt", id="a next-line control"),
        pytest.param("us" + escape(0x0000) + "dt", id="a NUL"),
        pytest.param("us" + escape(0x001F) + "dt", id="a control character"),
        pytest.param("us" + escape(0x007F) + "dt", id="a delete"),
        pytest.param("us" + escape(0x200B) + "dt", id="a zero-width space"),
        pytest.param(escape(0xFEFF) + "usdt", id="a byte order mark"),
        pytest.param("us" + escape(0x00AD) + "dt", id="a soft hyphen"),
        pytest.param("us" + escape(0xE000) + "dt", id="a private-use character"),
        pytest.param("us" + escape(0x0378) + "dt", id="an unassigned code point"),
        pytest.param("a" * (MAX_COIN_LENGTH + 1), id="forty-one characters"),
        pytest.param("a" * 400, id="four hundred characters"),
        pytest.param(MARK_COIN + " ", id="the marked coin, padded"),
    ],
)
def test_a_coin_that_is_not_an_asset_name_is_refused_and_not_repaired(coin: str) -> None:
    """R6: whitespace anywhere, a control or format character, or too long a name.

    **Refused, not stripped.** Kept, `" USDT "` joins nothing: it is neither the `USDT` this
    venue's fills name nor the cash asset of that name, so the whole stablecoin balance would
    surface in the holdings check as held with no history. Stripping it would be a guess
    about what the venue meant. BingX holds a balance's `asset` to the same rule, through
    the same predicate.
    """
    error = refusal(data_of(asset_entry(coin, available="1"), asset_entry("kas", available="5")))

    assert COIN_RULE in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    ("coin", "asset"),
    [
        pytest.param("a" * MAX_COIN_LENGTH, "A" * MAX_COIN_LENGTH, id="exactly forty characters"),
        pytest.param("x", "X", id="one character"),
        pytest.param("$u", "$U", id="a dollar sign"),
        pytest.param("d.o.g.e.", "D.O.G.E.", id="dots"),
        pytest.param("atom(arc20)", "ATOM(ARC20)", id="parentheses"),
        pytest.param("a_b-c", "A_B-C", id="an underscore and a hyphen"),
        pytest.param("1000sats", "1000SATS", id="digits first"),
        pytest.param(
            "m" + escape(0x00F8) + "th", "M" + chr(0x00D8) + "TH", id="a non-ASCII letter"
        ),
    ],
)
def test_a_coin_within_the_rule_is_accepted_and_upper_cased(coin: str, asset: str) -> None:
    """The boundary of R6 from the inside: what the rule lets through is still upper-cased."""
    balances = parsed(asset_entry(coin, available="1"))

    assert [entry.asset for entry in balances] == [asset]


def test_the_rule_is_applied_to_the_name_as_sent_before_it_is_upper_cased() -> None:
    """Forty sharp s are forty characters as sent and eighty once upper-cased: accepted.

    R6 says the check runs before upper-casing. A check after it would count the name the
    application made rather than the one the venue sent, and refuse this one.
    """
    coin = escape(0x00DF) * MAX_COIN_LENGTH

    balances = parsed(asset_entry(coin, available="1"))

    assert [entry.asset for entry in balances] == ["SS" * MAX_COIN_LENGTH]


async def test_a_padded_stablecoin_is_refused_rather_than_surfacing_as_a_holding() -> None:
    """The case R6 was ruled on, end to end through the fake venue: a `schema` failure."""
    fake = FakeBitget(assets=[asset_entry(" usdt ", available=MARK_AVAILABLE)])

    error = await refused_read(fake)

    assert type(error) is ExchangeSchemaError
    assert COIN_RULE in str(error)


# --------------------------------------------------------------------------------------
# Criterion 2: what is refused
# --------------------------------------------------------------------------------------

PARTS: Final = ("available", "frozen", "locked")


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('"-1"', id="minus one"),
        pytest.param('"-0.000000000000000001"', id="minus one unit"),
        pytest.param(f'"-{MARK_AVAILABLE}"', id="marked"),
        pytest.param('"-1E-18"', id="an exponent"),
        pytest.param("-1", id="a bare integer"),
        pytest.param("-0.5", id="a bare number"),
    ],
)
@pytest.mark.parametrize("field", PARTS)
def test_a_negative_part_is_refused_even_when_the_total_is_positive(
    field: str, fragment: str
) -> None:
    """Each part on its own, before the sum: the other two hold five each.

    A negative `frozen` inside a positive total is a number the venue never meant as a
    balance, and summing it would read the account too low without saying so.
    """
    parts = {"available": '"5"', "frozen": '"5"', "locked": '"5"'}
    parts[field] = fragment

    error = refusal(data_of(asset_entry(MARK_COIN, overrides=parts)))

    assert field in str(error)


def test_a_negative_frozen_does_not_hide_inside_an_available_of_five() -> None:
    """The case as the spec words it: available 5, frozen -1, a total of 4 that is refused."""
    error = refusal(data_of(asset_entry("kas", available="5", frozen="-1")))

    assert "frozen" in str(error)
    # The companion: the same entry with nothing frozen is a holding of five.
    assert pairs(parsed(asset_entry("kas", available="5", frozen="0"))) == [("KAS", "5")]


def test_a_negative_zero_is_a_zero_and_not_a_negative_amount() -> None:
    """`-0` is not below zero. Alone it is dropped; beside a holding it changes nothing."""
    assert parsed(asset_entry("kas", available="-0", frozen="-0.00", locked="-0")) == ()

    quantity = the_quantity(parsed(asset_entry("kas", available="-0", frozen="5", locked="-0")))

    assert quantity == 5
    assert not quantity.is_signed()


NOT_AN_AMOUNT: Final = [
    pytest.param("true", id="true"),
    pytest.param("false", id="false"),
    pytest.param("null", id="null"),
    pytest.param("{}", id="an empty object"),
    pytest.param("[]", id="an empty array"),
    pytest.param(f'{{"amount":"{MARK_AVAILABLE}"}}', id="an object holding the amount"),
    pytest.param(f'["{MARK_AVAILABLE}"]', id="an array holding the amount"),
    pytest.param('"abc"', id="letters"),
    pytest.param('""', id="an empty string"),
    pytest.param('" 1"', id="a leading space"),
    pytest.param(f'" {MARK_AVAILABLE}"', id="a leading space, marked"),
    pytest.param(f'"{MARK_AVAILABLE} "', id="a trailing space"),
    pytest.param(f'"{MARK_AVAILABLE}\\n"', id="a trailing newline"),
    pytest.param(f'"+{MARK_AVAILABLE}"', id="a plus sign"),
    pytest.param('"4,242.4242"', id="a thousands separator"),
    pytest.param('"4242_4242"', id="an underscore"),
    pytest.param('"4242.42.42"', id="two points"),
    pytest.param('".4242"', id="no digit before the point"),
    pytest.param('"4242."', id="no digit after the point"),
    pytest.param('"0x4242"', id="hexadecimal"),
    pytest.param(f'"{MARK_AVAILABLE} USDT"', id="a unit"),
    pytest.param(f'"{FULLWIDTH_DIGITS}"', id="fullwidth digits"),
    pytest.param('"NaN"', id="NaN"),
    pytest.param('"sNaN"', id="sNaN"),
    pytest.param('"Infinity"', id="Infinity"),
    pytest.param('"-Infinity"', id="-Infinity"),
    pytest.param('"inf"', id="inf"),
    pytest.param('"1e400000000000"', id="1e400000000000 as a string"),
    pytest.param("1e400000000000", id="1e400000000000 as a number"),
    pytest.param('"1e-400000000000"', id="1e-400000000000 as a string"),
    pytest.param('"1e1000000000000000000"', id="an exponent Decimal cannot hold"),
    pytest.param('"' + "4" * 5000 + '"', id="5000 digits"),
    pytest.param('"0.' + "0" * 100 + '1"', id="101 decimal places"),
    pytest.param(f'"{SURROGATE_ESCAPE}"', id="a lone surrogate"),
]


@pytest.mark.parametrize("fragment", NOT_AN_AMOUNT)
@pytest.mark.parametrize("field", PARTS)
def test_a_part_that_is_not_an_amount_is_refused_naming_the_field(
    field: str, fragment: str
) -> None:
    """Each of the three fields, with the other two the marked amounts.

    `true` would be one unit if a `bool` were let through as the `int` it subclasses, and
    `null` would be a zero if it were read as "nothing": both are refused. A string must be
    a plain decimal number and nothing looser -- no space, no sign but a minus, no
    separator, no `NaN` -- and no longer than a hundred digits written out.
    """
    error = refusal(data_of(marked_entry(**{field: fragment})))

    assert field in str(error)
    assert not isinstance(error.__cause__, decimal.DecimalException)


@pytest.mark.parametrize("field", ["coin", *PARTS])
def test_a_missing_field_is_refused_and_not_read_as_zero(field: str) -> None:
    """A total built from two of three parts is a number the venue never gave."""
    error = refusal(data_of(marked_entry(**{field: None})))

    assert field in str(error)


def test_the_entry_with_every_field_present_is_accepted() -> None:
    """The companion to every refusal built on `marked_entry`: untouched, it parses."""
    assert pairs(parsed(marked_entry())) == [(MARK_ASSET, MARK_TOTAL)]


# -- a total the column cannot hold ------------------------------------------------------

LARGEST: Final = "99999999999999999999.999999999999999999"
"""Twenty digits before the point and eighteen after: the most a `NumericText(18)` holds."""


@pytest.mark.parametrize(
    ("available", "frozen", "locked"),
    [
        pytest.param("0.0000000000000000001", "0", "0", id="a nineteenth place, alone"),
        pytest.param(
            "0.000000000000000001", "0.0000000000000000005", "0", id="a nineteenth place in a part"
        ),
        pytest.param(f"{MARK_AVAILABLE}000000000000001", "0", "0.0000000000000000001", id="marked"),
        pytest.param("99999999999999999999", "1", "0", id="twenty-one integer digits by a carry"),
        pytest.param("0", "0", "100000000000000000000", id="twenty-one integer digits in a part"),
        pytest.param(LARGEST, "0.000000000000000001", "0", id="one unit past the largest"),
        pytest.param(LARGEST, LARGEST, LARGEST, id="three times the largest"),
        pytest.param(
            "99999999999999999999.9999999999999999995", "0", "0", id="both limits at once"
        ),
        pytest.param("1e99", "0", "0", id="a hundred digits"),
        pytest.param("0", "1e-99", "0", id="ninety-nine places"),
    ],
)
def test_a_total_the_column_cannot_hold_is_a_schema_error(
    available: str, frozen: str, locked: str
) -> None:
    """`NumericText(18)` would round a nineteenth place and cannot store a 21st digit.

    Refused as the one class that says "the venue sent something we cannot read", never as
    a bare `ValueError` or `decimal.InvalidOperation` out of the arithmetic, and never
    chained from one in a way a traceback would print.
    """
    error = refusal(
        data_of(asset_entry(MARK_COIN, available=available, frozen=frozen, locked=locked))
    )

    assert error.__cause__ is None
    assert error.__suppress_context__ or error.__context__ is None


@pytest.mark.parametrize(
    ("available", "frozen", "locked"),
    [
        pytest.param(LARGEST, "0", "0", id="in one part"),
        pytest.param("99999999999999999999", "0.999999999999999999", "0", id="in two parts"),
        pytest.param("99999999999999999998", "0.999999999999999999", "1", id="in three parts"),
    ],
)
def test_the_largest_storable_total_is_accepted(available: str, frozen: str, locked: str) -> None:
    """The boundary: every digit the column has room for, kept exactly."""
    balances = parsed(asset_entry("kas", available=available, frozen=frozen, locked=locked))

    assert the_quantity(balances) == Decimal(LARGEST)


def test_two_parts_finer_than_eighteen_places_whose_total_is_not_are_accepted() -> None:
    """**Pinned: the scale is checked on the total, not on each part.**

    Two halves of the smallest unit -- each nineteen places, neither storable on its own --
    add to exactly one unit, and that is what is kept. Nothing is rounded: the sum is exact
    and the column stores it as it is. The companion row above ("a nineteenth place in a
    part") shows a part that fine is refused the moment the total inherits its place.
    """
    balances = parsed(
        asset_entry("kas", available="0.0000000000000000005", frozen="0.0000000000000000005")
    )

    assert the_quantity(balances) == Decimal("0.000000000000000001")


# -- the shape of `data` ------------------------------------------------------------------


def test_an_empty_array_is_an_account_that_holds_nothing() -> None:
    """The one way to say so. `()` -- a result, not a refusal."""
    assert parse_balances([]) == ()
    assert parse_balances(data_of()) == ()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(None, id="null"),
        pytest.param({}, id="an empty object"),
        pytest.param(
            {"coin": MARK_COIN, "available": MARK_AVAILABLE, "frozen": "0", "locked": "0"},
            id="one entry, not in an array",
        ),
        pytest.param({"balances": []}, id="an object wrapping an array"),
        pytest.param(MARK_COIN, id="a string"),
        pytest.param("", id="an empty string"),
        pytest.param(7, id="a number"),
        pytest.param(Decimal("4242.4242"), id="a decimal"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param((), id="a tuple"),
    ],
)
def test_a_data_that_is_not_an_array_is_refused(data: object) -> None:
    """`null` included: it is where a venue that failed to answer is most plausible.

    What this endpoint answers for an account upgraded to UTA is not documented, and a
    `null` read as "holds nothing" would make every holding look unaccounted for, in the
    other direction: the history would exceed the balances and nothing would say why.
    """
    error = refusal(data)

    assert "data" in str(error)


@pytest.mark.parametrize(
    "element",
    [
        pytest.param("null", id="null"),
        pytest.param("7", id="a number"),
        pytest.param("true", id="a boolean"),
        pytest.param(f'"{MARK_COIN}"', id="a string"),
        pytest.param("[]", id="an empty array"),
        pytest.param(f"[{marked_entry()}]", id="an entry wrapped in an array"),
    ],
)
@pytest.mark.parametrize("position", ["first", "last", "alone"])
def test_an_element_that_is_not_an_object_is_refused(position: str, element: str) -> None:
    """Wherever it sits: one unreadable element fails the read, it is not skipped."""
    held = marked_entry()
    entries = {"first": [element, held], "last": [held, element], "alone": [element]}[position]

    refusal(data_of(*entries))


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('""', id="empty"),
        pytest.param('"   "', id="spaces"),
        pytest.param('"\\t\\n"', id="a tab and a newline"),
        pytest.param("null", id="null"),
        pytest.param("7", id="a number"),
        pytest.param("true", id="a boolean"),
        pytest.param("{}", id="an object"),
        pytest.param(f'["{MARK_COIN}"]', id="an array holding the name"),
        pytest.param(f'{{"name":"{MARK_COIN}"}}', id="an object holding the name"),
        pytest.param(f'"{SURROGATE_ESCAPE}"', id="a lone surrogate"),
        pytest.param(f'"{MARK_COIN}{SURROGATE_ESCAPE}"', id="a name ending in a lone surrogate"),
    ],
)
def test_a_coin_that_is_not_non_blank_encodable_text_is_refused(fragment: str) -> None:
    """Blank, not a string, or text no UTF-8 encoder accepts: refused, naming `coin`.

    The lone surrogate is refused here, where the venue's name for the field is known, with
    no cause and no context -- a `UnicodeEncodeError` keeps the whole string in its `args`,
    and since #104 that string is an asset the owner holds.
    """
    error = refusal(data_of(marked_entry(coin=fragment)))

    assert COIN_RULE in str(error), "one message, whichever way the name is not a name (R6)"
    assert error.__cause__ is None
    assert error.__context__ is None


# --------------------------------------------------------------------------------------
# No value in any message
# --------------------------------------------------------------------------------------

#: One refusal of each kind this module raises, every one built around the marked holding.
MARKED_REFUSALS: Final[dict[str, Callable[[], object]]] = {
    "a coin named twice": lambda: data_of(marked_entry(), marked_entry()),
    "two spellings of one coin": lambda: data_of(
        marked_entry(), marked_entry(coin=f'"{MARK_ASSET}"')
    ),
    "a negative part": lambda: data_of(marked_entry(frozen=f'"-{MARK_FROZEN}"')),
    "a part that is not a number": lambda: data_of(marked_entry(locked=f'"{MARK_LOCKED} x"')),
    "a part of the wrong type": lambda: data_of(
        marked_entry(available=f'{{"amount":"{MARK_AVAILABLE}"}}')
    ),
    "a missing part": lambda: data_of(marked_entry(locked=None)),
    "a missing coin": lambda: data_of(marked_entry(coin=None)),
    "a coin of the wrong type": lambda: data_of(marked_entry(coin=f'["{MARK_COIN}"]')),
    "an unencodable coin": lambda: data_of(marked_entry(coin=f'"{MARK_COIN}{SURROGATE_ESCAPE}"')),
    "a nineteenth place": lambda: data_of(
        marked_entry(available=f'"{MARK_AVAILABLE}4242424242424241"')
    ),
    "a twenty-first digit": lambda: data_of(marked_entry(available='"424242424242424242424"')),
    "an absurd exponent": lambda: data_of(marked_entry(available='"4242e4242"')),
    "an element that is not an object": lambda: data_of(marked_entry(), f'"{MARK_COIN}"'),
    "a data that is an object": lambda: {MARK_COIN: MARK_AVAILABLE},
    "a data that is a string": lambda: f"{MARK_COIN} {MARK_AVAILABLE}",
}


@pytest.mark.parametrize("case", sorted(MARKED_REFUSALS))
def test_no_refusal_names_the_coin_or_an_amount(case: str) -> None:
    """Both are the owner's holdings, and an exception's text is a log line once it is raised.

    The text, the repr, the arguments and every linked exception -- shown or suppressed --
    are searched for the marked coin in either case and for the digits of each marked
    amount. `refusal` does the same for every other refusal in this module; this test is
    the list of kinds, in one place.
    """
    error = refusal(MARKED_REFUSALS[case]())

    text = rendered(error)
    assert MARK_COIN not in text.lower()
    assert "4242" not in text
    assert error.detail, "the positive companion: the refusal still says which rule"


def test_the_absence_check_can_fail() -> None:
    """The control: a refusal that did quote a holding is caught by the helper."""
    leaking = ExchangeSchemaError(f"coin {MARK_ASSET} holds {MARK_AVAILABLE}")
    chained = ExchangeSchemaError("quantity is wrong")
    chained.__context__ = ValueError(MARK_FROZEN)
    chained.__suppress_context__ = True

    with pytest.raises(AssertionError):
        assert_names_no_holding(leaking)
    with pytest.raises(AssertionError):
        assert_names_no_holding(chained)
    assert_names_no_holding(ExchangeSchemaError("quantity is wrong"))


# --------------------------------------------------------------------------------------
# Criterion 1: every failure is one of the seven classes
# --------------------------------------------------------------------------------------

#: A code Bitget does not use and the map does not hold, so a JSON body carrying it leaves
#: the classification to the status. `test_the_unmapped_code_is_unmapped` is the premise.
UNMAPPED_CODE: Final = "99999"

STATUS_BODIES: Final = {"html": HTML_BODY, "json": error_body(UNMAPPED_CODE, ECHOING_MSG)}


def refusing(reply: Reply) -> FakeBitget:
    """A venue whose every answer to a balance read is `reply`."""
    return FakeBitget(asset_replies=[reply])


def test_the_unmapped_code_is_unmapped() -> None:
    assert all(code != UNMAPPED_CODE for _status, code in BITGET_ERROR_MAP)


@pytest.mark.parametrize("body", sorted(STATUS_BODIES))
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ExchangeInvalidRequestError),
        (401, ExchangeAuthError),
        (403, ExchangeAuthError),
        (404, ExchangeInvalidRequestError),
        (408, ExchangeUnavailableError),
        (429, ExchangeRateLimitedError),
        (500, ExchangeUnavailableError),
        (502, ExchangeUnavailableError),
        (503, ExchangeUnavailableError),
        (504, ExchangeUnavailableError),
        (302, ExchangeSchemaError),
        (201, ExchangeSchemaError),
        (204, ExchangeSchemaError),
    ],
)
async def test_each_status_of_a_balance_read_maps_to_its_class(
    status: int, expected: type[ExchangeError], body: str
) -> None:
    """The exact class, never a superclass, and never chained from an HTTP status error.

    The same table the fills call is held to, because the two share `unwrap_envelope`: a
    401 or a 403 is the key, a 429 carries the wait the venue asked for, a 5xx is an outage
    and a status nobody classifies is an answer this application cannot read. That
    includes a 201 and a 204: a success is HTTP 200 and nothing else is.
    """
    fake = refusing(Reply(status=status, body=STATUS_BODIES[body], headers={"Retry-After": "7"}))

    error = await refused_read(fake)

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == (UNMAPPED_CODE if body == "json" else None)
    if isinstance(error, ExchangeRateLimitedError):
        assert error.retry_after_ms == 7000
    assert not any(isinstance(link, httpx.HTTPStatusError) for link in every_link(error))


async def test_a_rate_limited_balance_read_without_retry_after_carries_none() -> None:
    """`None` is "the venue said nothing", which is not `0`, "immediately"."""
    error = await refused_read(refusing(Reply(status=429, body=error_body("429", ECHOING_MSG))))

    assert type(error) is ExchangeRateLimitedError
    assert error.retry_after_ms is None


#: Spec 014's table of documented codes, by the class each belongs to. Written by hand.
DOCUMENTED_CODES: Final[dict[type[ExchangeError], tuple[str, ...]]] = {
    # The owner has to fix the key.
    ExchangeAuthError: ("40006", "40037", "40041", "40012", "40036", "40009", "40038", "40018"),
    # The key was accepted and lacks the permission this read needs.
    ExchangeInsufficientScopeError: ("40014", "40025", "40040"),
    # A timestamp the venue refused, and the errors the FAQ says to retry.
    ExchangeUnavailableError: ("40008", "40005", "45001", "40725", "40808", "40015"),
    # The in-band throttle.
    ExchangeRateLimitedError: ("429",),
    # Whatever the map gives for fills: these mean nothing for a balance, and are not
    # reclassified for one.
    ExchangeRetentionWindowError: ("40704",),
    ExchangeInvalidRequestError: (
        "00001",
        "40705",
        "40707",
        "40017",
        "40019",
        "40020",
        "40034",
        "40102",
    ),
}

CODE_CASES: Final = [
    pytest.param(code, expected, id=f"{code} {expected.__name__}")
    for expected, codes in DOCUMENTED_CODES.items()
    for code in codes
]


def test_the_table_of_codes_is_the_whole_map() -> None:
    """The premise of the next test: no mapped code is left out of it, and none is invented."""
    table = {(None, code): cls for cls, codes in DOCUMENTED_CODES.items() for code in codes}

    assert table == dict(BITGET_ERROR_MAP)


@pytest.mark.parametrize("status", [400, 200])
@pytest.mark.parametrize(("code", "expected"), CODE_CASES)
async def test_each_in_band_code_on_a_balance_read_maps_to_its_class(
    code: str, expected: type[ExchangeError], status: int
) -> None:
    """On a 400 and on a 200: the code decides, so a refusal on a 200 is not a schema error.

    A missing permission is `ExchangeInsufficientScopeError` -- the page does not say which
    permission the endpoint needs, so this is how the owner finds out -- and a refused key
    is `ExchangeAuthError`, the two classes the sync stops retrying on.
    """
    error = await refused_read(refusing(Reply(status=status, body=error_body(code, ECHOING_MSG))))

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == code


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, ExchangeSchemaError),
        (400, ExchangeInvalidRequestError),
        (401, ExchangeAuthError),
        (500, ExchangeUnavailableError),
    ],
)
async def test_an_unknown_code_on_a_balance_read_is_what_it_is_for_fills(
    status: int, expected: type[ExchangeError]
) -> None:
    """An unmapped code leaves the status to decide, and on a 200 nothing decides: schema."""
    reply = Reply(status=status, body=error_body(UNMAPPED_CODE, ECHOING_MSG))

    error = await refused_read(refusing(reply))

    assert type(error) is expected
    assert error.venue_code == UNMAPPED_CODE


@pytest.mark.parametrize("status", [400, 401, 200])
@pytest.mark.parametrize("code", ["40008", "40005"])
async def test_a_timestamp_error_on_a_balance_read_is_never_an_auth_error(
    code: str, status: int
) -> None:
    """A replayed request can arrive expired. The sync must not stop reading balances for it."""
    error = await refused_read(refusing(Reply(status=status, body=error_body(code))))

    assert type(error) is ExchangeUnavailableError
    assert not isinstance(error, ExchangeAuthError)


async def test_a_transport_failure_is_unavailable_and_chains_only_the_transport_error() -> None:
    cause = httpx.ConnectError("the fake venue refused the connection")
    fake = refusing(Reply(error=cause))

    error = await refused_read(fake)

    assert type(error) is ExchangeUnavailableError
    assert error.status is None
    assert error.__cause__ is cause
    assert not any(isinstance(link, httpx.HTTPStatusError) for link in every_link(error))
    text = rendered(error)
    for fragment in ("assetType", "hold_only", "api.bitget.com", "/api/v2", "account/assets"):
        assert fragment not in text
    # The positive companion: the request was made, and retried, before it failed.
    assert len(fake.asset_requests) == 3


@pytest.mark.parametrize(
    "cause",
    [
        httpx.ReadTimeout("the fake venue timed out"),
        httpx.ConnectTimeout("the fake venue did not answer"),
        httpx.RemoteProtocolError("the fake venue hung up"),
        httpx.ReadError("the fake venue reset the connection"),
    ],
    ids=lambda cause: type(cause).__name__,
)
async def test_every_kind_of_transport_failure_is_unavailable(cause: httpx.TransportError) -> None:
    error = await refused_read(refusing(Reply(error=cause)))

    assert type(error) is ExchangeUnavailableError
    assert error.__cause__ is cause


async def test_a_local_protocol_error_is_an_invalid_request_that_carries_nothing() -> None:
    """h11 quotes the whole illegal header value, which would be the key. Not linked at all."""
    quoted = f"Illegal header value b'{ACCESS_KEY_SENTINEL}'"
    fake = refusing(Reply(error=httpx.LocalProtocolError(quoted)))

    error = await refused_read(fake)

    assert type(error) is ExchangeInvalidRequestError
    assert error.status is None
    assert error.__cause__ is None
    assert error.__context__ is None


class BreakingStream(httpx.AsyncByteStream):
    """A body that starts arriving and then fails the way a dropped connection does."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield MARKED_BODY.encode("ascii")[:60]
        raise httpx.ReadError("the fake venue dropped the connection mid-body")


class BrokenOffReply(Reply):
    """A 200 whose body breaks off while the client reads it, above the retrying transport."""

    __slots__ = ()

    def respond(self) -> httpx.Response:
        return httpx.Response(200, stream=BreakingStream())


async def test_a_balance_body_that_breaks_off_mid_read_is_unavailable() -> None:
    """Half an answer is no answer: unavailable, so the next sync asks again.

    The failure arrives while the client reads the body -- after the transport returned the
    response, so nothing retries it -- and it must still end as one of the seven, naming
    nothing of the half that did arrive, which holds the marked coin.
    """
    fake = refusing(BrokenOffReply())

    error = await refused_read(fake)

    assert type(error) is ExchangeUnavailableError
    assert isinstance(error.__cause__, httpx.ReadError)
    assert len(fake.asset_requests) == 1


UNDECODABLE: Final = b"this is not a compressed body"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("status", [200, 500])
async def test_a_balance_body_that_does_not_decompress_is_unavailable(
    status: int, encoding: str
) -> None:
    """`httpx.DecodingError` is not a transport error, and still may not escape the seven."""
    reply = Reply(status=status, headers={"Content-Encoding": encoding}, wire=UNDECODABLE)

    error = await refused_read(refusing(reply))

    assert type(error) is ExchangeUnavailableError
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_a_balance_body_that_does_decompress_is_read() -> None:
    """The companion: the same encoding header over a real gzip body parses as usual."""
    body = gzip.compress(assets_body(asset_entry("kas", available="1500")).encode("ascii"))
    fake = refusing(Reply(headers={"Content-Encoding": "gzip"}, wire=body))

    assert pairs(await fetch_balances(fake)) == [("KAS", "1500")]


#: A whole, valid answer about the marked holding, for the bodies that damage one.
MARKED_BODY: Final = assets_body(marked_entry())
DEEPLY_NESTED: Final = "[" * 200_000 + "]" * 200_000

HOSTILE_BODIES: Final[dict[str, str]] = {
    "not JSON": f"{MARK_COIN} {MARK_AVAILABLE}",
    "HTML": HTML_BODY,
    "empty": "",
    "whitespace": "  \n ",
    "truncated in an amount": MARKED_BODY[: MARKED_BODY.index(MARK_FROZEN) + 4],
    "truncated before the closing brace": MARKED_BODY[:-1],
    "truncated before the closing bracket": MARKED_BODY[:-2],
    "trailing text": MARKED_BODY + "x",
    "two documents": MARKED_BODY + MARKED_BODY,
    "a top-level array": f"[{marked_entry()}]",
    "a top-level string": '"00000"',
    "a top-level null": "null",
    "a top-level number": "7",
    "no data": '{"code":"00000","msg":"success","requestTime":1}',
    "data null": envelope("null"),
    "data an empty object": envelope("{}"),
    "data one entry not in an array": envelope(marked_entry()),
    "data an object wrapping the array": envelope(f'{{"balances":[{marked_entry()}]}}'),
    "data a string": envelope(f'"{MARK_COIN}"'),
    "data a number": envelope("7"),
    "data true": envelope("true"),
    "code a number": f'{{"code":0,"msg":"success","data":[{marked_entry()}]}}',
    "code null": f'{{"code":null,"msg":"success","data":[{marked_entry()}]}}',
    "code of another spelling": f'{{"code":"0","msg":"success","data":[{marked_entry()}]}}',
    "no code": f'{{"msg":"success","data":[{marked_entry()}]}}',
    "an element that is null": assets_body(marked_entry(), "null"),
    "an element that is a number": assets_body("7", marked_entry()),
    "an element that is an array": assets_body("[]"),
    "a coin named twice": assets_body(marked_entry(), marked_entry()),
    "a negative part": assets_body(marked_entry(frozen='"-1"')),
    "a missing part": assets_body(marked_entry(locked=None)),
    "a part that is true": assets_body(marked_entry(available="true")),
    "a part that is null": assets_body(marked_entry(available="null")),
    "a part that is a bare NaN": assets_body(marked_entry(available="NaN")),
    "a part that is a bare Infinity": assets_body(marked_entry(frozen="Infinity")),
    "a part that is a bare -Infinity": assets_body(marked_entry(locked="-Infinity")),
    "a part with an exponent Decimal cannot hold": assets_body(
        marked_entry(available="1e1000000000000000000")
    ),
    "a part that is a 5000-digit integer": assets_body(marked_entry(available="4" * 5000)),
    "a part that is a 5000-digit string": assets_body(marked_entry(available=f'"{"4" * 5000}"')),
    "a part past a hundred digits": assets_body(marked_entry(available="1e400000000000")),
    "a nineteenth decimal place": assets_body(marked_entry(available='"0.0000000000000000001"')),
    "a twenty-first integer digit": assets_body(marked_entry(available='"99999999999999999999"')),
    "a coin that is a lone surrogate": assets_body(marked_entry(coin=f'"{SURROGATE_ESCAPE}"')),
    "a coin that is blank": assets_body(marked_entry(coin='" "')),
    "a coin that is null": assets_body(marked_entry(coin="null")),
    "an extra field nested past any depth": assets_body(marked_entry(extra=DEEPLY_NESTED)),
    "data nested past any depth": envelope(DEEPLY_NESTED),
}


@pytest.mark.parametrize("case", sorted(HOSTILE_BODIES))
async def test_a_success_status_with_a_body_that_cannot_be_read_is_a_schema_error(
    case: str,
) -> None:
    """HTTP 200 and anything but the documented answer: schema, and nothing else escapes.

    Truncated JSON, an HTML page, the wrong shape at every level, and the values that have
    escaped a parser in this repository before -- an integer past the interpreter's digit
    limit, an exponent `Decimal` cannot hold, the bare `NaN` Python's JSON accepts, nesting
    past the scanner's depth, a lone surrogate. Each is a body the venue chooses freely, so
    each must arrive as the class that says the venue sent something unreadable: never a
    `KeyError`, a `TypeError`, a `RecursionError`, a `decimal.InvalidOperation` or the
    decoder's `ProviderResponseError`.
    """
    fake = refusing(Reply(body=HOSTILE_BODIES[case]))

    error = await refused_read(fake)

    assert type(error) is ExchangeSchemaError
    assert error.__cause__ is None
    assert len(fake.asset_requests) == 1, "a schema error is not retried"


@pytest.mark.parametrize(
    "wire",
    [
        pytest.param(b"\xff\xfe\xfd", id="bytes that are no encoding"),
        pytest.param(MARKED_BODY.encode("ascii")[:-3] + b"\xc3", id="a truncated UTF-8 sequence"),
        pytest.param(b"\x00" * 64, id="NUL bytes"),
    ],
)
async def test_a_body_that_is_not_text_is_a_schema_error(wire: bytes) -> None:
    error = await refused_read(refusing(Reply(wire=wire)))

    assert type(error) is ExchangeSchemaError


async def test_a_null_data_on_a_balance_read_is_refused_and_an_empty_array_is_not() -> None:
    """The pair, end to end: `null` is not an answer, and `[]` is "holds nothing"."""
    error = await refused_read(refusing(Reply(body=envelope("null"))))
    empty = await fetch_balances(refusing(Reply(body=envelope("[]"))))

    assert type(error) is ExchangeSchemaError
    assert empty == ()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param(
            Reply(body=MARKED_BODY, headers={"ratelimit-remaining": "1" * 5000}),
            None,
            id="a success with a 5000-digit ratelimit-remaining",
        ),
        pytest.param(
            Reply(status=429, body=error_body("429"), headers={"Retry-After": "1" * 5000}),
            ExchangeRateLimitedError,
            id="a 429 with a 5000-digit Retry-After",
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
        pytest.param(
            Reply(status=503, body=HTML_BODY, headers={"x-ratelimit-reset": "9" * 5000}),
            ExchangeUnavailableError,
            id="a 503 with a 5000-digit x-ratelimit-reset",
        ),
    ],
)
async def test_a_hostile_header_on_a_balance_answer_ends_in_one_of_the_seven(
    reply: Reply, expected: type[ExchangeError] | None
) -> None:
    """A header the transport reads on every response never escapes as a bare `ValueError`."""
    fake = refusing(reply)

    if expected is None:
        assert pairs(await fetch_balances(fake)) == [(MARK_ASSET, MARK_TOTAL)]
        return
    error = await refused_read(fake)
    assert type(error) is expected
    if isinstance(error, ExchangeRateLimitedError):
        assert error.retry_after_ms is None, "an unusable Retry-After says nothing"


# --------------------------------------------------------------------------------------
# Hypothesis: whatever the venue sends, and any account it could describe
# --------------------------------------------------------------------------------------

#: Generate and shrink, and stop there. The explain phase re-runs a failing example under a
#: tracer, and with it a failing run of the last property here went past the suite's
#: 30-second ceiling: the failure would be reported as a timeout, without its counter-example.
PHASES: Final = (Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink)

#: Text that can sit between two quotes in a hand-built body without ending the string.
QUOTABLE_TEXT: Final = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters='"\\'), max_size=12
)

#: Raw JSON fragments a venue could put where a field goes: every JSON type, the shapes the
#: parser distinguishes, and the ones that have broken a parser in this repository before.
FRAGMENTS: Final = st.one_of(
    st.sampled_from(
        [
            "null",
            "true",
            "false",
            "0",
            "1",
            "-1",
            "0.1",
            "1e400",
            "1e-400",
            "1e1000000000000000000",
            "NaN",
            "[]",
            "{}",
            '""',
            '" "',
            '"0"',
            '"-0"',
            '"1"',
            '"-0.5"',
            '"0.000000000000000001"',
            '"0.0000000000000000001"',
            '"99999999999999999999"',
            '"NaN"',
            '"btc"',
            '"BTC"',
            '"usdt"',
            f'"{SURROGATE_ESCAPE}"',
        ]
    ),
    st.integers().map(str),
    st.decimals(allow_nan=False, allow_infinity=False, places=20).map(lambda value: f'"{value}"'),
    QUOTABLE_TEXT.map(lambda text: f'"{text}"'),
)

FIELD_NAMES: Final = st.sampled_from(
    ["coin", "available", "frozen", "locked", "limitAvailable", "uTime", "extra"]
)

EDITED_ENTRIES: Final = st.lists(
    st.tuples(
        st.sampled_from(["btc", "BTC", "kas", "usdt", "eth"]),
        st.lists(st.tuples(FIELD_NAMES, st.one_of(st.none(), FRAGMENTS)), max_size=3),
    ),
    max_size=4,
)


def outcome_of(call: Callable[[], object]) -> object:
    """What `call` returned, or the exchange error it raised. Anything else escapes."""
    try:
        return call()
    except ExchangeError as error:
        return error


def read_like_the_provider(status: int, body: str) -> tuple[AssetBalance, ...]:
    """Every step of `fetch_balances` after the response arrives, synchronously."""
    return parse_balances(unwrap_envelope(status, body))


@settings(
    max_examples=300,
    deadline=None,
    phases=PHASES,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    entries=EDITED_ENTRIES,
    status=st.sampled_from([200, 200, 200, 400, 401, 403, 429, 500, 503, 302, 418]),
)
def test_whatever_the_venue_sends_only_the_seven_classes_escape(
    entries: list[tuple[str, list[tuple[str, str | None]]]], status: int
) -> None:
    """Up to four entries, each a held coin with up to three fields replaced by anything.

    Either balances come back, and they keep every promise of the seam, or one of the seven
    classes is raised: never a `KeyError`, a `TypeError`, an `AttributeError`, an error out
    of `decimal`, or the decoder's own.
    """
    body = assets_body(
        *(asset_entry(coin, available="1", overrides=dict(edits)) for coin, edits in entries)
    )

    outcome = outcome_of(lambda: read_like_the_provider(status, body))

    if isinstance(outcome, ExchangeError):
        assert type(outcome) in SEVEN_CLASSES
        return
    assert status == 200, "only a 200 can be a success"
    assert type(outcome) is tuple
    names = [entry.asset for entry in outcome]
    assert names == sorted(set(names)), "sorted, and one entry per asset"
    for entry in outcome:
        assert type(entry) is AssetBalance
        assert type(entry.quantity) is Decimal
        assert entry.quantity > 0, "a zero is left out and a negative is refused"


def test_the_property_above_can_see_a_success() -> None:
    """The control: with no edit and a 200, the generated body is an account that parses."""
    body = assets_body(asset_entry("btc", available="1"), asset_entry("kas", available="1"))

    assert pairs(read_like_the_provider(200, body)) == [("BTC", "1"), ("KAS", "1")]


@settings(max_examples=200, deadline=None, phases=PHASES)
@given(status=st.integers(min_value=100, max_value=599), body=st.binary(max_size=64))
def test_any_status_with_any_body_is_one_of_the_seven_or_a_read(status: int, body: bytes) -> None:
    outcome = outcome_of(lambda: parse_balances(unwrap_envelope(status, body)))

    if isinstance(outcome, ExchangeError):
        assert type(outcome) in SEVEN_CLASSES
    else:
        assert status == 200, "only a 200 can be a success"


#: One unit is the eighteenth decimal place. The column holds 10**38 - 1 of them at most.
UNITS_PER_COIN: Final = 10**18
COLUMN_LIMIT: Final = 10**38

PART_UNITS: Final = st.one_of(
    st.just(0),
    st.just(0),
    st.integers(min_value=0, max_value=10**6),
    st.integers(min_value=0, max_value=COLUMN_LIMIT),
)

COIN_NAMES: Final = st.sampled_from(["btc", "eth", "Kas", "BGB", "usdt", "1inch"])


def written(units: int) -> str:
    """`units` eighteenth-places as plain positional text, by integer arithmetic alone."""
    whole, fraction = divmod(units, UNITS_PER_COIN)
    return f"{whole}.{fraction:018d}"


def test_written_spells_units_as_a_venue_would() -> None:
    """The premise of the property below, on three values worked out by hand."""
    assert written(0) == "0.000000000000000000"
    assert written(1) == "0.000000000000000001"
    assert written(1_500_000_000_000_000_000) == "1.500000000000000000"
    assert written(COLUMN_LIMIT - 1) == LARGEST


@settings(max_examples=300, deadline=None, phases=PHASES)
@given(
    account=st.dictionaries(COIN_NAMES, st.tuples(PART_UNITS, PART_UNITS, PART_UNITS), max_size=6),
    data=st.data(),
)
def test_any_account_reads_as_the_integer_sum_of_its_three_parts(
    account: dict[str, tuple[int, int, int]], data: st.DataObject
) -> None:
    """The model is integer addition, which no decimal context and no float can round.

    Every part is a whole number of eighteenth-places, written out as a venue writes an
    amount. What comes back must be, for each coin, exactly the sum of its three integers:
    dropped when that sum is zero, and the whole read refused when any sum is more than
    the column can hold.
    """
    entries = data.draw(
        st.permutations(
            [
                asset_entry(
                    coin,
                    available=written(available),
                    frozen=written(frozen),
                    locked=written(locked),
                    limit_available=written(data.draw(PART_UNITS)),
                )
                for coin, (available, frozen, locked) in account.items()
            ]
        )
    )
    totals = {coin.upper(): sum(parts) for coin, parts in account.items()}

    if any(total >= COLUMN_LIMIT for total in totals.values()):
        refusal(data_of(*entries))
        return
    balances = parsed(*entries)

    expected = sorted((asset, total) for asset, total in totals.items() if total)
    assert [(entry.asset, entry.quantity) for entry in balances] == [
        (asset, Decimal(f"{total}E-18")) for asset, total in expected
    ]
    assert all(type(entry.quantity) is Decimal for entry in balances)
