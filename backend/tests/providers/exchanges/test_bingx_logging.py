"""Criterion 3 of #14: no signature, key or secret reaches a log, or a rendered exception.

**The signature travels in the query string** -- the issue's warning. BingX signs the query
and carries the result as its last parameter, so any log line holding a request's URL holds
a signature that authorises that request for as long as the venue's receive window lasts.
The shared transport logs a label, never a path or a query; this module proves it for this
venue, rather than assuming spec 013's proof for Bitget carries over.

Read off stdout through the **production** pipeline, installed by the exchanges conftest's
`production_logging` -- never `structlog.testing.capture_logs`, which swaps the processor
chain out and never runs `format_exc_info`, so a secret carried inside an exception would be
invisible to the assertion.

**Searched in short windows, not as whole values** (the #13 lesson): pydantic once elided
the middle of a value and kept its head and tail, and a whole-value search cannot see a
tail. The key and the secret are searched for in every five-character window; a signature,
being hex, in every twelve-character window, because five hex characters occur in a log by
chance and twelve do not. The signatures are read back off the requests the fake recorded,
so each absence has a positive companion: the value searched for was real and was on the
wire.

**The venue echoes the key in its refusals here.** Every scripted refusal puts the key
sentinel in its `msg`, so a provider that carried the body anywhere would be caught.
"""

from __future__ import annotations

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
    error_body,
    fetch_page,
    fills_body,
    spread_fills,
    synthetic_credentials,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from tests.providers.exchanges.conftest import LoggingInstaller

#: Built from cycles no ordinary output contains, and with no English in them, so a window
#: found in a log can only have come from the credential. Letters only, so the key is
#: header-safe printable ASCII.
LOG_KEY: Final = "HvRtM" * 10
LOG_SECRET: Final = "PnDwZ" * 10

CREDENTIAL_WINDOW: Final = 5
SIGNATURE_WINDOW: Final = 12

#: The venue's refusal message, echoing the key as a careless venue would.
ECHOING_MSG: Final = f"apiKey {LOG_KEY} is not valid"

#: Fragments of a request that must not reach a log either: the path, the query, the header.
REQUEST_FRAGMENTS: Final = (
    "myTrades",
    "/openApi/",
    "startTime",
    "endTime",
    "signature=",
    "X-BX-APIKEY",
)


def venue(replies: Sequence[Reply] = (), *, fills: int = 0) -> FakeBingX:
    """A fake that signs and verifies with the log sentinels."""
    return FakeBingX(
        spread_fills(fills), replies=replies, signing_key=LOG_SECRET, access_key=LOG_KEY
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
    """Every signature the fake received, asserting there is one and each was the real thing.

    The positive companion for every absence in this module: a list holding only the
    sentinels would make "no signature in the log" true of any log at all.
    """
    signatures = [query.rpartition("&signature=")[2] for query in fake.queries()]
    assert signatures, "no signed request was made, so an absent signature proves nothing"
    for request, signature in zip(fake.requests, signatures, strict=True):
        assert len(signature) == 64
        assert request.headers["X-BX-APIKEY"] == LOG_KEY
    assert fake.signature_failures == [], "the requests were signed with the log sentinels"
    return signatures


def assert_nothing_of(written: str, fake: FakeBingX, *, where: str) -> None:
    assert_no_window(written, [LOG_KEY, LOG_SECRET], size=CREDENTIAL_WINDOW, where=where)
    assert_no_window(written, signatures_of(fake), size=SIGNATURE_WINDOW, where=where)


def test_the_window_search_catches_a_leaked_tail_and_ignores_ordinary_output() -> None:
    """The control: five characters of the key's tail, or twelve of a signature's, fail."""
    leaked_key = f"request refused for key ...{LOG_KEY[-5:]}"
    signature = "c181531feee1cc42ce6ac986aafca6c9b590a7b746809b820df601860b2f9d6e"
    leaked_signature = f"GET ...&signature=...{signature[20:32]}"

    with pytest.raises(AssertionError, match="a planted line") as caught:
        assert_no_window(leaked_key, [LOG_KEY], size=CREDENTIAL_WINDOW, where="a planted line")
    assert LOG_KEY not in str(caught.value), "the failure must not print the secret"
    with pytest.raises(AssertionError, match="a planted line"):
        assert_no_window(
            leaked_signature, [signature], size=SIGNATURE_WINDOW, where="a planted line"
        )
    assert_no_window(
        "success exchange_fills https://open-api.bingx.com 1684814440729 provider_request",
        [LOG_KEY, LOG_SECRET],
        size=CREDENTIAL_WINDOW,
        where="ordinary text",
    )


async def attempt(fake: FakeBingX) -> ExchangeError | None:
    """One fetch, returning the exchange error it raised, if any. Anything else escapes."""
    credentials = synthetic_credentials(api_key=LOG_KEY, api_secret=LOG_SECRET)
    try:
        await fetch_page(fake, credentials=credentials)
    except ExchangeError as error:
        return error
    return None


#: The four paths criterion 3 names -- a success, a refusal, a replayed request and a
#: transport failure -- plus the refusal on a 401, each with the marker its log line carries.
SCENARIOS: Final[dict[str, tuple[Callable[[], FakeBingX], str]]] = {
    "a success": (lambda: venue(fills=3), "provider_request"),
    "a refusal on a 200": (
        lambda: venue([Reply(body=error_body(100001, ECHOING_MSG))]),
        "provider_request",
    ),
    "a refusal on a 401": (
        lambda: venue([Reply(status=401, body=error_body(100413, ECHOING_MSG))]),
        "provider_request_failed",
    ),
    "a replayed request, a 429 then a 200": (
        lambda: venue(
            [
                Reply(
                    status=429, body=error_body(100410, ECHOING_MSG), headers={"Retry-After": "1"}
                ),
                Reply(body=fills_body(spread_fills(3))),
            ]
        ),
        "provider_request_retry",
    ),
    "a transport failure": (
        lambda: venue([Reply(error=httpx.ConnectError("connection refused"))]),
        "provider_request_failed",
    ),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_no_credential_reaches_the_log(
    scenario: str,
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """At debug, the level an operator turns on when something is wrong.

    The transport logs a target built from the scheme, the host and the endpoint label --
    `https://open-api.bingx.com/exchange_fills` -- and the provider logs nothing of its own.
    Asserted rather than reasoned about.
    """
    production_logging("DEBUG")
    build, marker = SCENARIOS[scenario]
    fake = build()

    await attempt(fake)

    written = capsys.readouterr().out
    assert written.strip(), "nothing was written, so an absence proves nothing"
    assert "https://open-api.bingx.com/exchange_fills" in written
    assert marker in written
    assert_nothing_of(written, fake, where="the log")
    for fragment in REQUEST_FRAGMENTS:
        assert fragment not in written, f"{fragment!r} of a request reached the log"
    assert "apiKey" not in written, "the venue's refusal message reached the log"


async def test_the_failure_marker_is_there_at_the_production_level(
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The companion at `INFO`: the failure line is still written, and still carries nothing."""
    production_logging()
    fake = venue([Reply(status=401, body=error_body(100413, ECHOING_MSG))])

    await attempt(fake)

    written = capsys.readouterr().out
    assert "provider_request_failed" in written
    assert "exchange_fills" in written
    assert_nothing_of(written, fake, where="the log")


#: One venue per class the provider raises, each refusal echoing the key in its `msg`.
#: `ExchangeRetentionWindowError` is absent because nothing BingX sends maps to it.
RAISED: Final[dict[type[ExchangeError], Callable[[], FakeBingX]]] = {
    ExchangeAuthError: lambda: venue([Reply(body=error_body(100001, ECHOING_MSG))]),
    ExchangeInsufficientScopeError: lambda: venue([Reply(body=error_body(100004, ECHOING_MSG))]),
    ExchangeRateLimitedError: lambda: venue([Reply(body=error_body(109429, ECHOING_MSG))]),
    ExchangeUnavailableError: lambda: venue([Reply(status=503, body=HTML_BODY)]),
    ExchangeInvalidRequestError: lambda: venue([Reply(body=error_body(100400, ECHOING_MSG))]),
    ExchangeSchemaError: lambda: venue([Reply(body=error_body(100999, ECHOING_MSG))]),
}

#: More routes into the same classes that carry something of their own: a stale replay, a
#: transport error, and h11's refusal of a header, which quotes the header's whole value.
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
async def test_no_credential_reaches_a_rendered_exception(
    route: str,
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`str`, `repr`, `args` and `logger.exception` of every class the provider raises.

    Every link of the chain is searched, the suppressed ones too -- a debugger or an error
    tracker walks `__context__` whatever `from None` said.
    """
    expected, build = ROUTES[route]
    fake = build()

    error = await attempt(fake)

    assert type(error) is expected
    for link in every_link(error):
        assert_nothing_of(f"{link}|{link!r}|{link.args!r}", fake, where="an exception")
        assert "apiKey" not in f"{link}|{link!r}|{link.args!r}"

    production_logging()
    logger = structlog.get_logger("tests.bingx")
    try:
        raise error
    except ExchangeError:
        logger.exception("bingx_fetch_refused")
    written = capsys.readouterr().out

    assert "bingx_fetch_refused" in written, "the positive companion: the line was written"
    assert expected.__name__ in written, "and it carries the traceback"
    assert_nothing_of(written, fake, where="the log")
    assert "apiKey" not in written
