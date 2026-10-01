"""Criterion 1 of #104 (spec 025), for BingX: the balance read is logged as `exchange_balances`.

**The signature travels in the query string.** A balance request is
`GET /openApi/spot/v1/account/balance?timestamp=<ms>&signature=<hex>`, so a log line holding
its URL holds a signature that authorises it for as long as the venue's receive window
lasts. And the answer is the owner's holdings: spec 025 allows no log line, column or
response to carry an asset name or an amount, except the reconciliation endpoint itself.

What the log may hold of a balance read is therefore `https://open-api.bingx.com/exchange_balances`
and nothing else: not the path, not the query, not the key, the secret or the signature, and
nothing of the body. This module asserts that off stdout, through the **production**
pipeline installed by the exchanges conftest's `production_logging` -- never
`structlog.testing.capture_logs`, which swaps the processor chain out and never runs
`format_exc_info`, so a value carried inside an exception would be invisible to it.

**Searched in short windows, not as whole values**, as `test_bingx_logging.py` does and for
its reason: the key and the secret in every five-character window, a signature in every
twelve-character window. The signatures are read back off the balance requests the fake
recorded, so each absence has a positive companion: the value searched for was real, and it
was on the wire.

**The venue echoes the key and the holdings in its refusals here**, and every successful or
malformed answer carries the marker asset `ZZMARK` and the marker amount
`424242.424242424`, so a provider or a transport that carried a body anywhere would be
caught. The amount is searched for in two long forms, with its point and as a run of
twelve digits: a log line carries a timestamp, and a short run of digits turns up in one
by chance.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import httpx
import pytest
import structlog

from portfolio.providers.exchanges.errors import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeInsufficientScopeError,
    ExchangeInvalidRequestError,
    ExchangeRateLimitedError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
)
from tests.providers.exchanges.bingx_harness import (
    HTML_BODY,
    FakeBingX,
    Reply,
    VenueBalance,
    balances_body,
    error_body,
    fetch_balances,
    fills_body,
    synthetic_credentials,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from tests.providers.exchanges.conftest import LoggingInstaller

#: Built from cycles no ordinary output contains, and with no English in them, so a window
#: found in a log can only have come from the credential. Letters only, so the key is
#: header-safe printable ASCII.
LOG_KEY: Final = "GxKvB" * 10
LOG_SECRET: Final = "WdNzQ" * 10

CREDENTIAL_WINDOW: Final = 5
SIGNATURE_WINDOW: Final = 12

#: Distinctive and synthetic: an asset and an amount that occur nowhere but in a body.
#: The amount is fifteen significant digits, so the provider's decode leaves it as it is.
MARK_ASSET: Final = "ZZMARK"
MARK_AMOUNT: Final = "424242.424242424"

#: What of the amount is searched for: with its point, and as digits alone, which catches
#: a rendering without the point. Both are too long to occur in a timestamp by chance.
MARK_AMOUNT_FORMS: Final = ("424242.4242", "424242424242")

#: The venue's refusal message, echoing the key and the holdings as a careless venue would.
ECHOING_MSG: Final = f"apiKey {LOG_KEY} is not valid for {MARK_ASSET} {MARK_AMOUNT}"

#: The clock every request here is stamped at: `bingx_harness.GOLDEN_TIMESTAMP_MS`, as text.
TIMESTAMP_TEXT: Final = "1684814440729"

#: Fragments of a balance request that must not reach a log: the path, the query, the header.
REQUEST_FRAGMENTS: Final = (
    "/openApi/spot/v1/account/balance",
    "account/balance",
    "/openApi/",
    "openApi",
    "timestamp=",
    TIMESTAMP_TEXT,
    "signature",
    "recvWindow",
    "X-BX-APIKEY",
)

#: The same, less the bare word `signature`: a traceback prints source lines, and the
#: provider's own source is entitled to that word. `signature=` is what a URL would carry.
TRACEBACK_FRAGMENTS: Final = (
    "/openApi/spot/v1/account/balance",
    "account/balance",
    "timestamp=",
    TIMESTAMP_TEXT,
    "signature=",
)

#: Nothing locked, so the total the provider computes is the marker amount too: a log line
#: carrying the total, and not only one carrying a part, is caught.
MARKED: Final = VenueBalance(asset=MARK_ASSET, free=MARK_AMOUNT, locked="0")
NEGATIVE: Final = VenueBalance(asset=MARK_ASSET, free=f"-{MARK_AMOUNT}", locked=MARK_AMOUNT)


def venue(
    replies: Sequence[Reply] = (), *, balances: Sequence[VenueBalance | str] = ()
) -> FakeBingX:
    """A fake that signs and verifies with the log sentinels."""
    return FakeBingX(
        balances=balances, balance_replies=replies, signing_key=LOG_SECRET, access_key=LOG_KEY
    )


def windows_of(value: str, size: int) -> set[str]:
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def assert_no_window(written: str, secrets: Sequence[str], *, size: int, where: str) -> None:
    """Fail naming the window and the line it reached, never the whole secret."""
    for secret in secrets:
        for window in sorted(windows_of(secret, size)):
            if window in written:
                line = next((one for one in written.splitlines() if window in one), written)
                message = f"{window!r} of a credential reached {where}: {line[:300]}"
                raise AssertionError(message)


def signatures_of(fake: FakeBingX) -> list[str]:
    """Every signature the fake received on a balance request, each the real thing.

    The positive companion for every absence in this module: with no signed request made,
    "no signature in the log" would be true of any log at all.
    """
    signatures = [query.rpartition("&signature=")[2] for query in fake.balance_queries()]
    assert signatures, "no signed balance request was made, so an absence proves nothing"
    for request, signature in zip(fake.balance_requests, signatures, strict=True):
        assert len(signature) == 64
        assert request.headers["X-BX-APIKEY"] == LOG_KEY
        assert request.url.path == "/openApi/spot/v1/account/balance"
    assert fake.signature_failures == [], "the requests were signed with the log sentinels"
    assert fake.requests == [], "the premise: only the balance endpoint was asked"
    return signatures


def assert_nothing_of(written: str, fake: FakeBingX, *, where: str) -> None:
    """No credential, no signature, and nothing of the holdings the venue answered with."""
    assert_no_window(written, [LOG_KEY, LOG_SECRET], size=CREDENTIAL_WINDOW, where=where)
    assert_no_window(written, signatures_of(fake), size=SIGNATURE_WINDOW, where=where)
    assert MARK_ASSET not in written, f"an asset name reached {where}"
    for form in MARK_AMOUNT_FORMS:
        assert form not in written, f"an amount reached {where}"
    assert "apiKey" not in written, f"the venue's refusal message reached {where}"


#: The three events the shared transport writes about a request. The provider writes none:
#: its module has no log call, so every structured line of a balance read is one of these.
TRANSPORT_EVENTS: Final = frozenset(
    {"provider_request", "provider_request_retry", "provider_request_failed"}
)


def structured_records(written: str) -> list[dict[str, object]]:
    """Every line of `written` that is a JSON object, which is how the pipeline renders one.

    A line that is not one is left to the text searches: the standard library's own
    loggers write plain text, and what they say is not this module's to enumerate.
    """
    records: list[dict[str, object]] = []
    for line in written.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def assert_only_the_transport_wrote(written: str) -> None:
    """Every structured line is the transport's, and names the balance label as its target.

    A line with any other event is a log call somebody added on the way to the venue, and a
    log call there has the owner's holdings in reach.
    """
    for record in structured_records(written):
        assert record.get("event") in TRANSPORT_EVENTS, f"an unexpected log line: {record}"
        assert record.get("target") == "https://open-api.bingx.com/exchange_balances"


def test_the_structured_line_check_tells_the_transports_lines_from_any_other() -> None:
    """The control: the transport's own line passes, and a line of anybody else's fails."""
    transport = (
        '{"target": "https://open-api.bingx.com/exchange_balances", "attempt": 1, '
        '"status": 200, "event": "provider_request", "level": "debug"}'
    )
    another = '{"count": 3, "event": "balances_parsed", "level": "info"}'
    mislabelled = (
        '{"target": "https://open-api.bingx.com/exchange_fills", "attempt": 1, '
        '"status": 200, "event": "provider_request", "level": "debug"}'
    )

    assert_only_the_transport_wrote(f"{transport}\nnot a structured line\n")
    assert_only_the_transport_wrote("")
    with pytest.raises(AssertionError, match="unexpected log line"):
        assert_only_the_transport_wrote(f"{transport}\n{another}\n")
    with pytest.raises(AssertionError):
        assert_only_the_transport_wrote(f"{mislabelled}\n")


def test_the_window_search_catches_a_leaked_tail_and_ignores_ordinary_output() -> None:
    """The control: five characters of the key's tail, or twelve of a signature's, fail."""
    leaked_key = f"request refused for key ...{LOG_KEY[-5:]}"
    signature = "89bb3f2fd36439a8fb61c453e299a931215c7e7b952d187380cd1742e1345c2d"
    leaked_signature = f"GET ...&signature=...{signature[20:32]}"

    with pytest.raises(AssertionError, match="a planted line") as caught:
        assert_no_window(leaked_key, [LOG_KEY], size=CREDENTIAL_WINDOW, where="a planted line")
    assert LOG_KEY not in str(caught.value), "the failure must not print the secret"
    with pytest.raises(AssertionError, match="a planted line"):
        assert_no_window(
            leaked_signature, [signature], size=SIGNATURE_WINDOW, where="a planted line"
        )
    assert_no_window(
        "success exchange_balances https://open-api.bingx.com provider_request",
        [LOG_KEY, LOG_SECRET],
        size=CREDENTIAL_WINDOW,
        where="ordinary text",
    )


async def attempt(fake: FakeBingX) -> ExchangeError | None:
    """One balance read, returning the exchange error it raised, if any. Anything else escapes."""
    credentials = synthetic_credentials(api_key=LOG_KEY, api_secret=LOG_SECRET)
    try:
        await fetch_balances(fake, credentials=credentials)
    except ExchangeError as error:
        return error
    return None


#: A success, the refusals, a replayed request, a transport failure, and three answers the
#: parser refuses -- each with the marker its log line carries and whether the read fails.
SCENARIOS: Final[dict[str, tuple[Callable[[], FakeBingX], str, bool]]] = {
    "a success": (lambda: venue(balances=[MARKED]), "provider_request", False),
    "a refusal on a 200": (
        lambda: venue([Reply(body=error_body(100001, ECHOING_MSG))]),
        "provider_request",
        True,
    ),
    "a refusal on a 401": (
        lambda: venue([Reply(status=401, body=error_body(100413, ECHOING_MSG))]),
        "provider_request_failed",
        True,
    ),
    "a replayed request, a 429 then a 200": (
        lambda: venue(
            [
                Reply(
                    status=429, body=error_body(100410, ECHOING_MSG), headers={"Retry-After": "1"}
                ),
                Reply(body=balances_body([MARKED])),
            ]
        ),
        "provider_request_retry",
        False,
    ),
    "a transport failure": (
        lambda: venue([Reply(error=httpx.ConnectError("connection refused"))]),
        "provider_request_failed",
        True,
    ),
    "an entry the parser refuses": (
        lambda: venue(balances=[MARKED, NEGATIVE]),
        "provider_request",
        True,
    ),
    "an asset named twice": (lambda: venue(balances=[MARKED, MARKED]), "provider_request", True),
    "a truncated answer": (
        lambda: venue([Reply(body=balances_body([MARKED])[:-9])]),
        "provider_request",
        True,
    ),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_nothing_of_a_balance_read_reaches_the_log_but_its_label(
    scenario: str,
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """At debug, the level an operator turns on when something is wrong.

    The transport logs a target built from the scheme, the host and the endpoint label --
    `https://open-api.bingx.com/exchange_balances` -- and the provider logs nothing of its
    own. Asserted rather than reasoned about: no path, no query, no credential, no
    signature, no asset and no amount.
    """
    production_logging("DEBUG")
    build, marker, fails = SCENARIOS[scenario]
    fake = build()

    error = await attempt(fake)

    written = capsys.readouterr().out
    assert (error is not None) is fails, "the premise: the scenario ended as it was scripted to"
    assert written.strip(), "nothing was written, so an absence proves nothing"
    assert "https://open-api.bingx.com/exchange_balances" in written
    assert marker in written
    assert "exchange_fills" not in written, "a balance read was logged as a fills read"
    assert "<unlabelled>" not in written
    assert structured_records(written), "the positive companion: the lines were structured"
    assert_only_the_transport_wrote(written)
    assert_nothing_of(written, fake, where="the log")
    for fragment in REQUEST_FRAGMENTS:
        assert fragment not in written, f"{fragment!r} of a request reached the log"


async def test_the_failure_marker_is_there_at_the_production_level(
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The companion at `INFO`: the failure line is still written, labelled, and carries nothing."""
    production_logging()
    fake = venue([Reply(status=401, body=error_body(100413, ECHOING_MSG))])

    await attempt(fake)

    written = capsys.readouterr().out
    assert "provider_request_failed" in written
    assert "https://open-api.bingx.com/exchange_balances" in written
    assert_only_the_transport_wrote(written)
    assert_nothing_of(written, fake, where="the log")
    for fragment in REQUEST_FRAGMENTS:
        assert fragment not in written, f"{fragment!r} of a request reached the log"


async def test_a_successful_balance_read_adds_no_line_of_the_providers_own(
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """At `INFO`, after a success: the provider has no log call of its own to say what the
    account holds or how much of it, so whatever was written is the transport's, and
    carries nothing of the answer."""
    production_logging()
    fake = venue(balances=[MARKED])

    error = await attempt(fake)

    written = capsys.readouterr().out
    assert error is None
    assert_only_the_transport_wrote(written)
    assert_nothing_of(written, fake, where="the log")
    for fragment in REQUEST_FRAGMENTS:
        assert fragment not in written, f"{fragment!r} of a request reached the log"


#: One venue per class the balance read raises, each refusal echoing the key and the
#: holdings in its `msg`. `ExchangeRetentionWindowError` is absent because nothing BingX
#: sends maps to it.
RAISED: Final[dict[type[ExchangeError], Callable[[], FakeBingX]]] = {
    ExchangeAuthError: lambda: venue([Reply(body=error_body(100001, ECHOING_MSG))]),
    ExchangeInsufficientScopeError: lambda: venue([Reply(body=error_body(100004, ECHOING_MSG))]),
    ExchangeRateLimitedError: lambda: venue([Reply(body=error_body(109429, ECHOING_MSG))]),
    ExchangeUnavailableError: lambda: venue([Reply(status=503, body=HTML_BODY)]),
    ExchangeInvalidRequestError: lambda: venue([Reply(body=error_body(100400, ECHOING_MSG))]),
    ExchangeSchemaError: lambda: venue([Reply(body=error_body(100999, ECHOING_MSG))]),
}

#: More routes into the same classes that carry something of their own: a stale replay, a
#: transport error, h11's refusal of a header (which quotes the header's whole value), and
#: every way the parser refuses an answer that is the owner's holdings.
EXTRA_ROUTES: Final[dict[str, tuple[type[ExchangeError], Callable[[], FakeBingX]]]] = {
    "a stale replay": (
        ExchangeUnavailableError,
        lambda: venue(
            [Reply(status=503, body=HTML_BODY), Reply(body=error_body(100421, ECHOING_MSG))]
        ),
    ),
    "a transport failure": (
        ExchangeUnavailableError,
        lambda: venue([Reply(error=httpx.ReadTimeout("read timed out"))]),
    ),
    "an illegal header": (
        ExchangeInvalidRequestError,
        lambda: venue(
            [Reply(error=httpx.LocalProtocolError(f"Illegal header value b'{LOG_KEY}'"))]
        ),
    ),
    "a negative part": (ExchangeSchemaError, lambda: venue(balances=[MARKED, NEGATIVE])),
    "an asset named twice": (ExchangeSchemaError, lambda: venue(balances=[MARKED, MARKED])),
    "an amount that is not one": (
        ExchangeSchemaError,
        lambda: venue(
            balances=[VenueBalance(asset=MARK_ASSET, free=f"{MARK_AMOUNT}abc", locked=MARK_AMOUNT)]
        ),
    ),
    "an amount finer than the column": (
        ExchangeSchemaError,
        lambda: venue(
            balances=[VenueBalance(asset=MARK_ASSET, free="0.0000424242424242424", locked="0")]
        ),
    ),
    "an amount too large for the column": (
        ExchangeSchemaError,
        lambda: venue(
            balances=[VenueBalance(asset=MARK_ASSET, free="4242" * 5 + "4", locked=MARK_AMOUNT)]
        ),
    ),
    "an asset that is not one": (
        ExchangeSchemaError,
        lambda: venue(
            balances=[VenueBalance(asset=f"{MARK_ASSET} X", free=MARK_AMOUNT, locked=MARK_AMOUNT)]
        ),
    ),
    "a missing field": (
        ExchangeSchemaError,
        lambda: venue(
            balances=[VenueBalance(asset=MARK_ASSET, free=MARK_AMOUNT, overrides={"locked": None})]
        ),
    ),
    "a fills answer": (
        ExchangeSchemaError,
        lambda: venue([Reply(body=fills_body([]))]),
    ),
    "a truncated answer": (
        ExchangeSchemaError,
        lambda: venue([Reply(body=balances_body([MARKED])[:-9])]),
    ),
}

ROUTES: Final[dict[str, tuple[type[ExchangeError], Callable[[], FakeBingX]]]] = {
    **{error_class.__name__: (error_class, build) for error_class, build in RAISED.items()},
    **EXTRA_ROUTES,
}


def every_link(error: BaseException) -> Iterator[BaseException]:
    """Every exception reachable by `__cause__` or `__context__`, suppressed or not."""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        link = pending.pop()
        if id(link) not in seen:
            seen.add(id(link))
            yield link
            pending.extend(
                nested for nested in (link.__cause__, link.__context__) if nested is not None
            )


@pytest.mark.parametrize("route", list(ROUTES))
async def test_nothing_of_a_balance_read_reaches_a_rendered_exception(
    route: str,
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`str`, `repr`, `args` and `logger.exception` of every class the balance read raises.

    This is the line spec 025's sync writes when a balance read fails with an `internal`
    kind, and what any caller's `logger.exception` would write for the rest. Every link of
    the chain is searched, the suppressed ones too -- a debugger or an error tracker walks
    `__context__` whatever `from None` said.
    """
    expected, build = ROUTES[route]
    fake = build()

    error = await attempt(fake)

    assert type(error) is expected
    for link in every_link(error):
        assert_nothing_of(f"{link}|{link!r}|{link.args!r}", fake, where="an exception")

    production_logging()
    logger = structlog.get_logger("tests.bingx")
    try:
        raise error
    except ExchangeError:
        logger.exception("bingx_balances_refused")
    written = capsys.readouterr().out

    assert "bingx_balances_refused" in written, "the positive companion: the line was written"
    assert expected.__name__ in written, "and it carries the traceback"
    assert_nothing_of(written, fake, where="the log")
    for fragment in TRACEBACK_FRAGMENTS:
        assert fragment not in written, f"{fragment!r} of a request reached the log"
