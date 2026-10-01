"""Spec 025 for Bitget's balance read: what a log may say about it, and what it may not.

"No log line carries an asset name or an amount" is the spec's rule, and a balance request
is signed exactly as a fills request is, so everything criterion 7 of #13 keeps out of a log
stays out here too. Read off stdout through the **production** pipeline, installed by the
exchanges conftest's `production_logging` -- never `structlog.testing.capture_logs`, which
swaps the processor chain out and never runs `format_exc_info`, so a value carried inside an
exception would be invisible to the assertion.

What a line about a balance read does say: `https://api.bitget.com/exchange_balances`, the
scheme, the host and the label. What must stay out, each with a positive companion showing
the value searched for was real:

* **the request**: the path, the query and the access headers;
* **the credentials**: the API key, the passphrase, the secret, and every `ACCESS-SIGN` the
  provider sent, read back off the requests the fake venue recorded;
* **the holdings**: a marked coin and three marked amounts the fake venue answers with, and
  their total. The venue also echoes them, and the key, in the `msg` of its refusals, which
  is the field a careless venue echoes a request in.
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
    asset_entry,
    assets_body,
    error_body,
    fetch_balances,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from portfolio.providers.exchanges.base import AssetBalance
    from tests.providers.exchanges.conftest import LoggingInstaller

#: A coin no venue lists and amounts nobody holds, so their absence from a log means
#: something. The coin is lower-case as the venue would send it; the provider upper-cases
#: it, so it is searched for without regard to case.
MARK_COIN: Final = "zzmark"
MARK_AVAILABLE: Final = "424242.424242424242"
MARK_FROZEN: Final = "737373.737373737373"
MARK_LOCKED: Final = "919191.919191919191"
#: The three added by hand, and checked in integer arithmetic by the premise test below.
MARK_TOTAL: Final = "2080808.080808080806"
HOLDINGS: Final = (MARK_AVAILABLE, MARK_FROZEN, MARK_LOCKED, MARK_TOTAL)

#: What a **log** is searched for: two long forms of each amount. The head of the amount
#: with its point, and its first twelve digits with the point taken out, which also catches
#: an amount rendered in some other unit. Long on purpose. A log line carries a timestamp
#: with six digits of microseconds, and a traceback carries line numbers, so a short run of
#: digits such as `4242` turns up in a log by chance and fails a test one run in a few.
#: Neither form below can come out of a timestamp or a line number.
LOG_FORMS: Final = (
    "424242.4242",
    "424242424242",
    "737373.7373",
    "737373737373",
    "919191.9191",
    "919191919191",
    "2080808.0808",
    "208080808080",
)

#: What an **exception's own text** is searched for as well. Nothing random is in it, so a
#: short run is safe there, and it catches a message that quoted only part of an amount.
TEXT_FORMS: Final = ("424242", "737373", "919191", "2080808")


def marked_entry(**overrides: str | None) -> str:
    return asset_entry(
        MARK_COIN,
        available=MARK_AVAILABLE,
        frozen=MARK_FROZEN,
        locked=MARK_LOCKED,
        overrides=overrides,
    )


#: A whole answer: the marked holding, and an ordinary one beside it.
HELD_BODY: Final = assets_body(marked_entry(), asset_entry("kas", available="1500"))

#: The venue's refusal message, echoing the holdings and the key as a careless venue would.
ECHOING_MSG: Final = (
    f"coin {MARK_COIN} available {MARK_AVAILABLE} apiKey {ACCESS_KEY_SENTINEL} is not valid"
)

#: Fragments of the request that must not reach a log: the path, the query, a header name.
REQUEST_FRAGMENTS: Final = (
    "/api/v2/spot/account/assets",
    "account/assets",
    "/api/v2",
    "assetType",
    "hold_only",
    "ACCESS-SIGN",
    "ACCESS-KEY",
    "ACCESS-PASSPHRASE",
)

#: Lone surrogate, as the JSON escape a venue sends. In two pieces, so this file stays ASCII.
SURROGATE_ESCAPE: Final = "\\" + "ud800"


def refusing(reply: Reply) -> FakeBitget:
    """A venue whose every answer to a balance read is `reply`."""
    return FakeBitget(asset_replies=[reply])


def secrets_of(fake: FakeBitget) -> list[str]:
    """The three sentinels and every signature the fake received on a balance request.

    Asserts the signatures exist, which is the positive companion for every absence test in
    this module: a list holding only the sentinels would make "no signature in the log" true
    of any log at all.
    """
    signatures = [request.headers["ACCESS-SIGN"] for request in fake.asset_requests]
    assert signatures, "no signed request was made, so an absent signature proves nothing"
    for request in fake.asset_requests:
        assert request.headers["ACCESS-KEY"] == ACCESS_KEY_SENTINEL
        assert request.headers["ACCESS-PASSPHRASE"] == PHRASE_SENTINEL
    return [ACCESS_KEY_SENTINEL, PHRASE_SENTINEL, SIGNING_SENTINEL, *signatures]


def assert_absent(written: str, forbidden: Sequence[str]) -> None:
    for value in forbidden:
        if value in written:
            line = next(one for one in written.splitlines() if value in one)
            message = f"a value that must not be logged is on this line: {line[:400]}"
            raise AssertionError(message)


def assert_no_holding(written: str) -> None:
    """Neither the marked coin, in either case, nor a marked amount, nor the echoed message."""
    assert_absent(written.lower(), [MARK_COIN])
    assert_absent(written, LOG_FORMS)
    assert_absent(written, [ECHOING_MSG])


async def attempt(fake: FakeBitget) -> Sequence[AssetBalance] | ExchangeError:
    """One balance read: its answer, or the exchange error it raised. Anything else escapes."""
    try:
        return await fetch_balances(fake)
    except ExchangeError as error:
        return error


def test_the_marked_body_holds_what_the_absence_checks_search_for() -> None:
    """The premise: the coin and all three amounts are in the body the venue sends."""
    for value in (MARK_COIN, MARK_AVAILABLE, MARK_FROZEN, MARK_LOCKED):
        assert value in HELD_BODY
    assert MARK_COIN in error_body("40006", ECHOING_MSG)
    assert MARK_AVAILABLE in error_body("40006", ECHOING_MSG)


def test_the_forms_searched_for_are_forms_of_the_marked_amounts() -> None:
    """The premise of every absence: each amount would be found, written out or without its point.

    The total is checked in integer arithmetic, in units of the twelfth decimal place.
    """
    assert 424242424242424242 + 737373737373737373 + 919191919191919191 == 2080808080808080806
    for amount in HOLDINGS:
        digits = amount.replace(".", "")
        assert sum(amount.startswith(form) for form in LOG_FORMS) == 1
        assert sum(digits.startswith(form) for form in LOG_FORMS) == 1
        assert sum(amount.startswith(form) for form in TEXT_FORMS) == 1
    with pytest.raises(AssertionError):
        assert_no_holding(f'{{"quantity": "{MARK_TOTAL}"}}')
    with pytest.raises(AssertionError):
        assert_no_holding('{"base_units": 424242424242424242}')
    with pytest.raises(AssertionError):
        assert_no_holding(f'{{"asset": "{MARK_COIN.upper()}"}}')
    assert_no_holding('{"timestamp": "2026-10-01T19:42:42.424242Z", "line": 4242}')


#: Each path a balance read can take through the transport: the venue, the log event its
#: line must carry, and whether the read succeeds.
SCENARIOS: Final[dict[str, tuple[Callable[[], FakeBitget], str, bool]]] = {
    "success": (lambda: refusing(Reply(body=HELD_BODY)), "provider_request", True),
    "a 401": (
        lambda: refusing(Reply(status=401, body=error_body("40006", ECHOING_MSG))),
        "provider_request_failed",
        False,
    ),
    "a 403 with an HTML page": (
        lambda: refusing(Reply(status=403, body=HTML_BODY)),
        "provider_request_failed",
        False,
    ),
    "a 429 then a 200": (
        lambda: FakeBitget(
            asset_replies=[
                Reply(
                    status=429, body=error_body("429", ECHOING_MSG), headers={"Retry-After": "1"}
                ),
                Reply(body=HELD_BODY),
            ]
        ),
        "provider_request_retry",
        True,
    ),
    "a 503 every time": (
        lambda: refusing(Reply(status=503, body=error_body("45001", ECHOING_MSG))),
        "provider_request_failed",
        False,
    ),
    "a transport failure": (
        lambda: refusing(Reply(error=httpx.ConnectError("connection refused"))),
        "provider_request_failed",
        False,
    ),
    "a 200 the parser refuses": (
        lambda: refusing(Reply(body=assets_body(marked_entry(), marked_entry()))),
        "provider_request",
        False,
    ),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_a_balance_read_logs_its_label_and_nothing_of_the_request_or_the_holdings(
    scenario: str,
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """At debug, the level an operator turns on when something is wrong.

    The transport logs a target built from the scheme, the host and the endpoint label, and
    the provider logs nothing of its own. So a line says `exchange_balances` -- which read
    it was -- and neither where it went, nor who asked, nor what came back.
    """
    production_logging("DEBUG")
    build, event, succeeds = SCENARIOS[scenario]
    fake = build()

    outcome = await attempt(fake)

    written = capsys.readouterr().out
    assert written.strip(), "nothing was written, so an absence proves nothing"
    assert event in written
    assert "https://api.bitget.com/exchange_balances" in written
    assert "<unlabelled>" not in written
    assert "exchange_fills" not in written
    assert "exchange_symbol" not in written
    assert_absent(written, secrets_of(fake))
    assert_absent(written, REQUEST_FRAGMENTS)
    assert_no_holding(written)
    if succeeds:
        # The positive companion: the marked holding really was in the answer and was read.
        assert not isinstance(outcome, ExchangeError)
        assert [(entry.asset, str(entry.quantity)) for entry in outcome] == [
            ("KAS", "1500"),
            (MARK_COIN.upper(), MARK_TOTAL),
        ]
    else:
        assert isinstance(outcome, ExchangeError)


async def test_a_failed_balance_read_is_still_logged_at_the_production_level(
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The companion at `INFO`: the failure line is written, labelled, and carries nothing."""
    production_logging()
    fake = refusing(Reply(status=401, body=error_body("40006", ECHOING_MSG)))

    outcome = await attempt(fake)

    written = capsys.readouterr().out
    assert type(outcome) is ExchangeAuthError
    assert "provider_request_failed" in written
    assert "https://api.bitget.com/exchange_balances" in written
    assert_absent(written, secrets_of(fake))
    assert_absent(written, REQUEST_FRAGMENTS)
    assert_no_holding(written)


async def test_a_successful_balance_read_logs_no_holding_at_the_production_level(
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """At `INFO` a read that works may be silent, and whatever it does write names nothing.

    The HTTP client's own logger writes the whole URL of every request at this level unless
    the pipeline silences it, which is the leak #6 found; the request fragments are searched
    for here for that reason.
    """
    production_logging()
    fake = refusing(Reply(body=HELD_BODY))

    outcome = await attempt(fake)

    written = capsys.readouterr().out
    assert not isinstance(outcome, ExchangeError)
    assert len(outcome) == 2, "the positive companion: the answer was read"
    assert_absent(written, secrets_of(fake))
    assert_absent(written, REQUEST_FRAGMENTS)
    assert_no_holding(written)


#: One venue per class the provider raises, each refusal echoing the holdings and the key.
RAISED: Final[dict[type[ExchangeError], Callable[[], FakeBitget]]] = {
    ExchangeAuthError: lambda: refusing(Reply(status=401, body=error_body("40006", ECHOING_MSG))),
    ExchangeInsufficientScopeError: lambda: refusing(
        Reply(status=400, body=error_body("40014", ECHOING_MSG))
    ),
    ExchangeRateLimitedError: lambda: refusing(
        Reply(status=429, body=error_body("429", ECHOING_MSG))
    ),
    ExchangeUnavailableError: lambda: refusing(Reply(status=503, body=HTML_BODY)),
    ExchangeInvalidRequestError: lambda: refusing(
        Reply(status=400, body=error_body("40017", ECHOING_MSG))
    ),
    ExchangeRetentionWindowError: lambda: refusing(
        Reply(status=400, body=error_body("40704", ECHOING_MSG))
    ),
    ExchangeSchemaError: lambda: refusing(Reply(status=200, body=error_body("12345", ECHOING_MSG))),
}

#: More routes into the same classes, each carrying something of its own: a transport error,
#: h11's refusal of a header -- which quotes the header's whole value -- and every way the
#: parser refuses an answer that holds the marked coin and the marked amounts.
EXTRA_ROUTES: Final[dict[str, tuple[type[ExchangeError], Callable[[], FakeBitget]]]] = {
    "transport failure": (
        ExchangeUnavailableError,
        lambda: refusing(Reply(error=httpx.ReadTimeout("read timed out"))),
    ),
    "illegal header": (
        ExchangeInvalidRequestError,
        lambda: refusing(
            Reply(error=httpx.LocalProtocolError(f"Illegal header value b'{ACCESS_KEY_SENTINEL}'"))
        ),
    ),
    "a body that does not decompress": (
        ExchangeUnavailableError,
        lambda: refusing(Reply(headers={"Content-Encoding": "gzip"}, wire=HELD_BODY.encode())),
    ),
    "a truncated body": (
        ExchangeSchemaError,
        lambda: refusing(Reply(body=HELD_BODY[: HELD_BODY.index(MARK_FROZEN) + 6])),
    ),
    "a coin named twice": (
        ExchangeSchemaError,
        lambda: refusing(Reply(body=assets_body(marked_entry(), marked_entry()))),
    ),
    "a negative part": (
        ExchangeSchemaError,
        lambda: refusing(Reply(body=assets_body(marked_entry(frozen=f'"-{MARK_FROZEN}"')))),
    ),
    "a part that is not a number": (
        ExchangeSchemaError,
        lambda: refusing(Reply(body=assets_body(marked_entry(locked=f'"{MARK_LOCKED} x"')))),
    ),
    "a missing part": (
        ExchangeSchemaError,
        lambda: refusing(Reply(body=assets_body(marked_entry(available=None)))),
    ),
    "a total too fine for the column": (
        ExchangeSchemaError,
        lambda: refusing(
            Reply(body=assets_body(marked_entry(available=f'"{MARK_AVAILABLE}4242421"')))
        ),
    ),
    "a total too large for the column": (
        ExchangeSchemaError,
        lambda: refusing(Reply(body=assets_body(marked_entry(locked='"424242424242424242424"')))),
    ),
    "an unencodable coin": (
        ExchangeSchemaError,
        lambda: refusing(
            Reply(body=assets_body(marked_entry(coin=f'"{MARK_COIN}{SURROGATE_ESCAPE}"')))
        ),
    ),
    "a null data": (
        ExchangeSchemaError,
        lambda: refusing(Reply(body=error_body("00000", ECHOING_MSG))),
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
async def test_nothing_of_a_balance_read_reaches_a_rendered_exception(
    route: str,
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`str`, `repr`, `args` and `logger.exception` of every class the balance read raises.

    The sync logs `exchange_balances_read_failed` for a failed read, with the traceback when
    it cannot classify the failure, and `redact_sensitive` matches key names: the field a
    traceback lands in is called `exception`, so nothing downstream can help. The message
    must never have carried a coin, an amount or a credential. Every link of the chain is
    searched, the suppressed ones too -- a debugger or an error tracker walks `__context__`
    whatever `from None` said.
    """
    expected, build = ROUTES[route]
    fake = build()

    error = await attempt(fake)

    assert isinstance(error, ExchangeError)
    assert type(error) is expected
    forbidden = secrets_of(fake)
    for link in every_link(error):
        text = f"{link}|{link!r}|{link.args!r}"
        assert_absent(text, forbidden)
        assert_no_holding(text)
        assert_absent(text, TEXT_FORMS)

    production_logging()
    logger = structlog.get_logger("tests.bitget")
    try:
        raise error
    except ExchangeError:
        logger.exception("bitget_balances_refused")
    written = capsys.readouterr().out

    assert "bitget_balances_refused" in written, "the positive companion: the line was written"
    assert expected.__name__ in written, "and it carries the traceback"
    assert_absent(written, forbidden)
    assert_absent(written, REQUEST_FRAGMENTS)
    assert_no_holding(written)
