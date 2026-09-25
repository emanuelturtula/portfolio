"""Criterion 7 of #13: no credential and no signature reaches a log, or a rendered exception.

Read off stdout through the **production** pipeline, installed by the exchanges conftest's
`production_logging` -- never `structlog.testing.capture_logs`, which swaps the processor
chain out and never runs `format_exc_info`, so a secret carried inside an exception would be
invisible to the assertion. That is how a real leak passed a green gate on #5.

Four things must stay out: the API key, the passphrase, the secret, and every `ACCESS-SIGN`
the provider sent. The first three are the synthetic sentinels of `bitget_harness`; the
signatures are read back off the requests the fake venue recorded, so each absence has a
positive companion proving the value searched for was real and was on the wire.

**The venue echoes the key in its refusals here.** Bitget's error envelope carries a `msg`,
and `msg` is the field that echoes request parameters. Every scripted refusal puts the API
key sentinel in its `msg`, so a provider that carried the body anywhere would be caught.
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
    ExchangeRetentionWindowError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
)
from tests.providers.exchanges.bitget_harness import (
    ACCESS_KEY_SENTINEL,
    HTML_BODY,
    PHRASE_SENTINEL,
    SIGNING_SENTINEL,
    FakeBitget,
    Reply,
    error_body,
    fetch_page,
    fills_body,
    spread_fills,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tests.providers.exchanges.conftest import LoggingInstaller

#: The venue's refusal message, echoing the key as a careless venue would.
ECHOING_MSG: Final = f"apiKey {ACCESS_KEY_SENTINEL} is not valid"

#: Fragments of a request that must not reach a log either: the query, the path, a header.
REQUEST_FRAGMENTS: Final = ("idLessThan", "startTime=", "/api/v2/spot", "ACCESS-SIGN", "ACCESS-KEY")


def secrets_of(*fakes: FakeBitget) -> list[str]:
    """The three sentinels and every signature the fakes received.

    Asserts the signatures exist, which is the positive companion for every absence test in
    this module: a list holding only the sentinels would make "no signature in the log"
    true of any log at all.
    """
    signatures = [
        request.headers["ACCESS-SIGN"] for fake in fakes for request in fake.fill_requests
    ]
    assert signatures, "no signed request was made, so an absent signature proves nothing"
    for fake in fakes:
        for request in fake.fill_requests:
            assert request.headers["ACCESS-KEY"] == ACCESS_KEY_SENTINEL
            assert request.headers["ACCESS-PASSPHRASE"] == PHRASE_SENTINEL
    return [ACCESS_KEY_SENTINEL, PHRASE_SENTINEL, SIGNING_SENTINEL, *signatures]


def assert_absent(written: str, forbidden: list[str]) -> None:
    for value in forbidden:
        if value in written:
            line = next(one for one in written.splitlines() if value in one)
            message = f"a credential or signature reached the log on this line: {line[:400]}"
            raise AssertionError(message)


async def attempt(fake: FakeBitget) -> ExchangeError | None:
    """One fetch, returning the exchange error it raised, if any. Anything else escapes."""
    try:
        await fetch_page(fake)
    except ExchangeError as error:
        return error
    return None


#: The four paths criterion 7 names, each a venue and the marker its log line must carry.
SCENARIOS: Final[dict[str, tuple[Callable[[], FakeBitget], str]]] = {
    "success": (lambda: FakeBitget(spread_fills(3)), "provider_request"),
    "a 401": (
        lambda: FakeBitget(fill_replies=[Reply(status=401, body=error_body("40006", ECHOING_MSG))]),
        "provider_request_failed",
    ),
    "a 429 then a 200": (
        lambda: FakeBitget(
            fill_replies=[
                Reply(
                    status=429, body=error_body("429", ECHOING_MSG), headers={"Retry-After": "1"}
                ),
                Reply(body=fills_body(spread_fills(3))),
            ]
        ),
        "provider_request_retry",
    ),
    "a transport failure": (
        lambda: FakeBitget(fill_replies=[Reply(error=httpx.ConnectError("connection refused"))]),
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

    The transport logs a target built from the scheme, the host and the endpoint label, and
    the provider logs nothing of its own. Asserted rather than reasoned about.
    """
    production_logging("DEBUG")
    build, marker = SCENARIOS[scenario]
    fake = build()

    await attempt(fake)

    written = capsys.readouterr().out
    assert written.strip(), "nothing was written, so an absence proves nothing"
    assert "exchange_fills" in written
    assert marker in written
    assert_absent(written, secrets_of(fake))
    assert_absent(written, list(REQUEST_FRAGMENTS))
    assert ECHOING_MSG not in written


async def test_the_failure_markers_are_there_at_the_production_level(
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The companion at `INFO`: the failure line is still written, and still carries nothing."""
    production_logging()
    fake = FakeBitget(fill_replies=[Reply(status=401, body=error_body("40006", ECHOING_MSG))])

    await attempt(fake)

    written = capsys.readouterr().out
    assert "provider_request_failed" in written
    assert "exchange_fills" in written
    assert_absent(written, secrets_of(fake))


#: One venue per class the provider raises, each refusal echoing the key in its `msg`.
RAISED: Final[dict[type[ExchangeError], Callable[[], FakeBitget]]] = {
    ExchangeAuthError: lambda: FakeBitget(
        fill_replies=[Reply(status=401, body=error_body("40006", ECHOING_MSG))]
    ),
    ExchangeInsufficientScopeError: lambda: FakeBitget(
        fill_replies=[Reply(status=400, body=error_body("40014", ECHOING_MSG))]
    ),
    ExchangeRateLimitedError: lambda: FakeBitget(
        fill_replies=[Reply(status=429, body=error_body("429", ECHOING_MSG))]
    ),
    ExchangeUnavailableError: lambda: FakeBitget(fill_replies=[Reply(status=503, body=HTML_BODY)]),
    ExchangeInvalidRequestError: lambda: FakeBitget(
        fill_replies=[Reply(status=400, body=error_body("40017", ECHOING_MSG))]
    ),
    ExchangeRetentionWindowError: lambda: FakeBitget(
        fill_replies=[Reply(status=400, body=error_body("40704", ECHOING_MSG))]
    ),
    ExchangeSchemaError: lambda: FakeBitget(
        fill_replies=[Reply(status=200, body=error_body("12345", ECHOING_MSG))]
    ),
}

#: Two more routes into the same classes that carry something of their own: a transport
#: error, and h11's refusal of a header, which quotes the header's whole value.
EXTRA_ROUTES: Final[dict[str, tuple[type[ExchangeError], Callable[[], FakeBitget]]]] = {
    "transport failure": (
        ExchangeUnavailableError,
        lambda: FakeBitget(fill_replies=[Reply(error=httpx.ReadTimeout("read timed out"))]),
    ),
    "illegal header": (
        ExchangeInvalidRequestError,
        lambda: FakeBitget(
            fill_replies=[
                Reply(
                    error=httpx.LocalProtocolError(f"Illegal header value b'{ACCESS_KEY_SENTINEL}'")
                )
            ]
        ),
    ),
}

ROUTES: Final[dict[str, tuple[type[ExchangeError], Callable[[], FakeBitget]]]] = {
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

    `redact_sensitive` matches key names, and the field a traceback lands in is called
    `exception`, so nothing downstream can help: the message must never have carried it.
    Every link of the chain is searched, the suppressed ones too -- a debugger or an error
    tracker walks `__context__` whatever `from None` said.
    """
    expected, build = ROUTES[route]
    fake = build()

    error = await attempt(fake)

    assert type(error) is expected
    forbidden = secrets_of(fake)
    for link in every_link(error):
        assert_absent(f"{link}|{link!r}|{link.args!r}", forbidden)

    production_logging()
    logger = structlog.get_logger("tests.bitget")
    try:
        raise error
    except ExchangeError:
        logger.exception("bitget_fetch_refused")
    written = capsys.readouterr().out

    assert "bitget_fetch_refused" in written, "the positive companion: the line was written"
    assert expected.__name__ in written, "and it carries the traceback"
    assert_absent(written, forbidden)
    assert ECHOING_MSG not in written
