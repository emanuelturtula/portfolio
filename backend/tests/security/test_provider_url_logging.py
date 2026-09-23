"""Criterion 4b: a wallet address never reaches stdout because of a provider request.

**These tests read stdout. They do not use `structlog.testing.capture_logs`.** That is the
whole design of this module and it is not a stylistic preference: `capture_logs` swaps the
entire processor chain out for a `LogCapture` and then reports on the pipeline it
installed. On #5 it hid a real production leak behind a green gate, because the thing that
leaked was rendered by a processor `capture_logs` had replaced.

The same argument applies with more force here, because the transport is not the only
thing that can write a log line about a request. `httpx` logs one of its own, from
`AsyncClient.send`, *above* the transport -- so a test that only inspected what
`RetryingTransport` emitted would be examining the one log line that was already known to
be safe. Reading the bytes on stdout is the only way to see every line the request caused.

Every assertion of absence is paired with an assertion that stdout carried the log at all.
An absence assertion on its own is satisfied by silence, and silence is exactly what a
misordered logging fixture produces -- which is how #5's leak survived.

Addresses come from `tests/address_vectors.py`. Testnet only; rule 3.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.logging import URL_LOGGING_LIBRARIES, VENDOR_LOG_FLOOR
from portfolio.providers.http import (
    ADDRESS_BALANCE,
    BLOCK_TIP_HEIGHT,
    ENDPOINT_EXTENSION,
    UNLABELLED,
    HostRateLimiter,
    RetryPolicy,
    build_http_client,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BIP350_TESTNET_V1,
    CORE_SIGNET_P2PKH,
    KASPA_TESTNET_V1_KEY,
)
from tests.providers.chains.harness import EsploraFake, Reply, ScriptedInstance, esplora_provider
from tests.security.conftest import assert_absent, assert_carried_something

if TYPE_CHECKING:
    from collections.abc import Callable

#: A fictional host (RFC 2606). Rule 3: no real API hostname in the repository.
ORIGIN: Final = "https://api.example"

#: The label a chain provider will put in `request.extensions`.
ENDPOINT_LABEL: Final = "address_balance"

#: The structlog event names the transport emits, and the level each one needs. A test
#: that asserted an absence without naming one of these would be asserting about silence.
SUCCESS_EVENT: Final = "provider_request"
RETRY_EVENT: Final = "provider_request_retry"
FAILURE_EVENT: Final = "provider_request_failed"

#: Synthetic, and obviously so. In the query string, because one exchange will sign there
#: and a logged URL would otherwise carry both the signature and the key that produced
#: it. Deliberately not plausible-looking: a string that could be mistaken for a real
#: credential is one somebody eventually reports as a leak.
SENTINEL_SIGNATURE: Final = "NOT-A-REAL-SIGNATURE-DO-NOT-LOG-aaaa"
SENTINEL_API_KEY: Final = "NOT-A-REAL-API-KEY-DO-NOT-LOG-bbbb"


def esplora_style_url(address: str) -> str:
    """Esplora's real shape: the address is in the path, not the query.

    This is the URL that makes criterion 4 insufficient on its own. Stripping the query
    from it removes the signature and leaves the address exactly where it was.
    """
    return (
        f"{ORIGIN}/api/address/{address}/utxo"
        f"?apiKey={SENTINEL_API_KEY}&signature={SENTINEL_SIGNATURE}"
    )


def kaspa_style_url(address: str) -> str:
    """Kaspa's real shape, which puts the address in a different path segment."""
    return f"{ORIGIN}/addresses/{address}/balance"


async def drive(
    url: str,
    *outcomes: int | BaseException,
    endpoint: str | None = ENDPOINT_LABEL,
    max_attempts: int = 3,
) -> None:
    """Make one request through the real client, with every duration injected.

    The whole production path -- the limiter, the retry loop, the logging -- over an
    `httpx.MockTransport`. Nothing sleeps: a security test that took two seconds of real
    time per retry would be a security test somebody eventually marks slow and skips.
    """

    async def no_sleep(_milliseconds: int) -> None:
        return

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        outcome = outcomes[min(calls - 1, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        # A body that echoes the request, which is what a public index actually does on an
        # error -- and therefore one more way the address could reach a log.
        return httpx.Response(outcome, json={"requested": str(request.url)})

    client = build_http_client(
        transport=httpx.MockTransport(handler),
        policy=RetryPolicy(max_attempts=max_attempts, base_backoff_ms=0, max_backoff_ms=0),
        limiter=HostRateLimiter(min_interval_ms=0, clock=lambda: 0, sleep=no_sleep),
        jitter=lambda bound: bound,
        sleep=no_sleep,
    )
    extensions = {ENDPOINT_EXTENSION: endpoint} if endpoint is not None else {}
    async with client:
        # The final failure is one of the cases under test, so the exception it raises
        # is expected here rather than a fault in the test.
        with contextlib.suppress(httpx.TransportError):
            await client.get(url, extensions=extensions)


# --------------------------------------------------------------------------------------
# The success path
# --------------------------------------------------------------------------------------


async def test_no_log_line_from_a_request_contains_the_address_in_its_path(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Criterion 4b, against the bytes the process actually writes.

    Driven at `DEBUG` because the transport's success line is a debug line, and a test
    that could not see the success line would be asserting about the failure paths only.
    `DEBUG` is also the setting an operator reaches for when something is wrong, which is
    precisely the moment a leak would happen and nobody would be watching for it.
    """
    production_logging(log_level="DEBUG")

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 200)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=SUCCESS_EVENT)
    assert_absent(written, BIP173_TESTNET_P2WPKH)
    assert SENTINEL_SIGNATURE not in written
    assert SENTINEL_API_KEY not in written


@pytest.mark.parametrize(
    ("address", "url_shape"),
    [
        pytest.param(BIP173_TESTNET_P2WPKH, esplora_style_url, id="bitcoin, esplora path"),
        pytest.param(KASPA_TESTNET_V1_KEY, kaspa_style_url, id="kaspa, rest path"),
        pytest.param(
            BIP173_TESTNET_P2WPKH_UPPERCASE,
            esplora_style_url,
            id="the uppercase spelling a qr wallet shows",
        ),
    ],
)
async def test_both_target_chains_path_shapes_are_covered(
    address: str,
    url_shape: Callable[[str], str],
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Esplora and Kaspa put the address in different path segments.

    A scrubber written against one shape would leave the other intact, and the uppercase
    row covers the case where a log lower-cases what it prints and a case-sensitive
    assertion would miss it.
    """
    production_logging(log_level="DEBUG")

    await drive(url_shape(address), 200)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=SUCCESS_EVENT)
    assert_absent(written, address)


async def test_an_unlabelled_request_logs_no_path_at_all(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Deny by default: a request that says nothing about itself is logged as nothing.

    The failure this rules out is the tempting one -- falling back to the path when there
    is no label, on the reasoning that a log line saying `<unlabelled>` is not very
    useful. It is not very useful, and that is the correct trade: saying more is an
    opt-in, exactly as making an endpoint public is in rule 8.
    """
    production_logging(log_level="DEBUG")

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 200, endpoint=None)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=UNLABELLED)
    assert_absent(written, BIP173_TESTNET_P2WPKH)
    assert "/api/address/" not in written


# --------------------------------------------------------------------------------------
# The retry and failure paths, which are where an error message quotes what it refused
# --------------------------------------------------------------------------------------


async def test_a_retried_and_a_failed_request_log_no_address_either(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """The warning line and the error line, in one request that produces both.

    Three 503s with `max_attempts=3`: two retry warnings and one final error. These are
    the lines most likely to quote the thing that failed, because "the request failed" is
    exactly when a developer reaches for the URL to make the message useful.

    At the production default level, not `DEBUG` -- a warning and an error are what the Pi
    actually emits, so this is the configuration the disclosure would happen in.
    """
    production_logging()

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 503, max_attempts=3)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=RETRY_EVENT)
    assert_carried_something(written, marker=FAILURE_EVENT)
    assert_absent(written, BIP173_TESTNET_P2WPKH)
    assert SENTINEL_SIGNATURE not in written
    assert SENTINEL_API_KEY not in written


async def test_a_transport_error_that_exhausts_its_attempts_logs_no_address(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """The other final-failure arm: no response at all, and an exception that propagates.

    `httpx` annotates its own errors with the request that failed, so the exception
    carries the URL -- and `format_exc_info` renders a traceback into the production JSON
    log. An exception's text is precisely the leak `capture_logs` could not see on #5.
    """
    production_logging()

    await drive(
        kaspa_style_url(KASPA_TESTNET_V1_KEY),
        httpx.ConnectError("connection refused"),
        max_attempts=2,
    )

    written = capsys.readouterr().out

    assert_carried_something(written, marker=FAILURE_EVENT)
    assert_absent(written, KASPA_TESTNET_V1_KEY)


async def test_a_throttled_request_logs_the_delay_but_not_the_target_path(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """A 429 is the line an operator reads most often, so it is the one most often copied."""
    production_logging()

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 429, 200, max_attempts=2)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=RETRY_EVENT)
    assert "delay_ms" in written
    assert_absent(written, BIP173_TESTNET_P2WPKH)


# --------------------------------------------------------------------------------------
# Criterion 4 proper: the query string, and the guard that keeps it off stdout
# --------------------------------------------------------------------------------------
#
# The leak these tests exist for did not come from any code in this repository. `httpx`
# logs every request itself, at INFO, with the whole URL, through the standard library --
# above the transport, where `request_target` cannot reach it, and outside structlog, where
# `redact_sensitive` never sees it. It was live at production defaults.
#
# The fix is a named list of silenced loggers rather than a mechanism, so these tests pin
# the list, pin the floor, and prove the guard can fail.


async def test_no_fake_credential_in_a_query_string_survives_to_stdout(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Criterion 4 as the issue words it, measured on stdout rather than on a helper.

    `strip_query` passing its unit tests says what that function returns. It says nothing
    about whether a URL with a signature in it reached the log by another route -- and one
    did, at the production default level, which is where it mattered.
    """
    production_logging()

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 503, max_attempts=2)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=FAILURE_EVENT)
    assert SENTINEL_API_KEY not in written
    assert SENTINEL_SIGNATURE not in written
    assert "apiKey" not in written
    assert "signature" not in written


def test_the_silenced_logger_list_is_pinned_against_a_literal() -> None:
    """Removing an entry has to fail here rather than quietly reopening the leak.

    The same shape as `EXPECTED_CHAIN_KEYS` in `tests/domain/test_chains.py`, and for the
    same reason: a guard that derives its expectation from the thing it guards shrinks
    along with it and cannot fail. `httpx` is the measured leak; `httpcore` is
    precautionary and the source says so rather than implying a test that does not exist.
    """
    assert URL_LOGGING_LIBRARIES == ("httpx", "httpcore")
    assert VENDOR_LOG_FLOOR == logging.WARNING


def test_the_vendor_floor_is_absolute_and_not_relative_to_the_application_level(
    production_logging: Callable[..., None],
) -> None:
    """Turning the application up to DEBUG must not be the act that reopens the leak.

    This is the arm a naive fix gets wrong. `max(settings_level, WARNING)` reads sensibly
    and puts the URL back on stdout at exactly the moment somebody is tailing the log to
    find out why a provider is failing -- which is also the moment a copy ends up pasted
    into an issue.

    Asserted on `.level` as well as on `getEffectiveLevel()`. The first says the floor was
    set on the logger itself, which is what survives the next
    `logging.basicConfig(force=True)` -- and `configure_logging` calls exactly that. A
    filter installed on the root handler would satisfy the second, be removed by the next
    call, and leave every test green.
    """
    for level in ("DEBUG", "INFO", "WARNING"):
        production_logging(log_level=level)

        for name in URL_LOGGING_LIBRARIES:
            logger = logging.getLogger(name)
            assert logger.level == VENDOR_LOG_FLOOR, f"{name} at app level {level}"
            assert logger.getEffectiveLevel() == VENDOR_LOG_FLOOR, f"{name} at app level {level}"


async def test_the_leak_returns_the_moment_the_guard_is_removed(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """The control, and the reason this module is not a test of nothing.

    Every other assertion here says an address was absent from stdout. An absence proves
    something only if the test would have seen the value had it been there -- and the
    route this one watches is not the transport's own log line but
    `httpx.AsyncClient.send`, which no assertion about `request_target` can reach.

    So the guard is lifted, the same request is driven again, and the address is asserted
    to be **present**. If a future httpx moves its request line to DEBUG or renames its
    logger, this goes red and says the guard is now watching a route nothing uses --
    rather than leaving it to protect against nothing with every test still green.
    """
    production_logging()
    httpx_logger = logging.getLogger("httpx")
    previous = httpx_logger.level
    try:
        httpx_logger.setLevel(logging.INFO)
        await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 200)
        written = capsys.readouterr().out
    finally:
        httpx_logger.setLevel(previous)

    assert "HTTP Request:" in written, (
        "httpx no longer logs a request line at INFO, so the silenced-logger list is "
        "guarding a route that does not exist and the reason for it needs revisiting"
    )
    assert BIP173_TESTNET_P2WPKH in written
    assert SENTINEL_SIGNATURE in written


def test_the_guard_is_restored_after_the_control_lifted_it(
    production_logging: Callable[..., None],
) -> None:
    """The control above mutates a process-global logger, so this says it put it back.

    Test order is not something to rely on, and a leaked `setLevel(INFO)` would make a
    later run of the tests above fail for a reason that has nothing to do with the code
    they are testing.
    """
    production_logging()

    assert logging.getLogger("httpx").level == VENDOR_LOG_FLOOR


# --------------------------------------------------------------------------------------
# Criterion 9 of #7: the label allowlist, measured on stdout
# --------------------------------------------------------------------------------------
#
# #6 closed the accidental disclosure -- a label carrying a slash, a dot or an upper-case
# character -- with `ENDPOINT_LABEL`, and recorded in its own docstring the one input it
# could not close: a **truncated** bech32 address is lowercase, alphanumeric and under 32
# characters, so it matched the pattern exactly. Twenty characters of a bech32 address is
# unique on chain and is enough to search an explorer with.
#
# `tests/providers/test_url_scrubbing.py` asserts that `request_target` now renders it
# `<unlabelled>`. That is a statement about a function. These two tests are the statement
# about the artifact: what the process actually wrote to stdout, through the real
# transport, with the production pipeline installed.


async def test_a_truncated_address_used_as_a_label_does_not_reach_the_log(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Criterion 9, over the bytes the process wrote.

    The label is deliberately the leak #6 could not close, and it is driven at `DEBUG` so
    the success line is visible -- a test that could not see the success line would be
    asserting about the failure paths only, and `DEBUG` is what an operator turns on when
    something is wrong, which is precisely when nobody is watching for a disclosure.

    The absence assertion is paired with two companions, because an absence over an empty
    capture passes for the wrong reason and #5 catalogued that twice. First, that stdout
    carried a provider log line at all. Second, that the line rendered `<unlabelled>` --
    which says the label was *seen and refused*, not merely that the request never
    happened.
    """
    production_logging(log_level="DEBUG")
    truncated = BIP173_TESTNET_P2WPKH[:20]
    assert truncated.isascii()
    assert truncated.islower()
    assert len(truncated) <= 32

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 200, endpoint=truncated)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=SUCCESS_EVENT)
    assert_carried_something(written, marker=UNLABELLED)
    assert_absent(written, BIP173_TESTNET_P2WPKH)
    assert truncated not in written


@pytest.mark.parametrize(
    "label",
    [
        pytest.param(BIP173_TESTNET_P2WPKH[:20], id="twenty characters of a bech32 address"),
        pytest.param(BIP173_TESTNET_P2WPKH[:12], id="twelve characters"),
        pytest.param(KASPA_TESTNET_V1_KEY[10:30], id="twenty characters of a kaspa payload"),
    ],
)
async def test_a_well_shaped_label_that_is_not_on_the_allowlist_reaches_no_log_line(
    label: str,
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Every line a request can produce, not only the successful one.

    Three 503s with `max_attempts=3` produces two retry warnings and one final error, at
    the production default level. Those are the lines most likely to quote what failed --
    "the request failed" is exactly when a developer reaches for the target to make the
    message useful -- and they are the lines an operator copies into an issue.
    """
    production_logging()

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 503, endpoint=label, max_attempts=3)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=RETRY_EVENT)
    assert_carried_something(written, marker=FAILURE_EVENT)
    assert_carried_something(written, marker=UNLABELLED)
    assert label not in written
    assert_absent(written, BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V1_KEY)


async def test_a_balance_read_logs_no_address_on_any_line(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """The real provider, reading a real balance, with the production pipeline installed.

    Every other test in this module drives a bare `client.get` at a URL the test wrote.
    This one drives `EsploraProvider.fetch_balances`, so the URL, the label and the retry
    are the provider's own -- which is the only configuration in which "a balance read
    logs no address" is a claim about the thing that will actually run on the Pi.

    Four addresses, two instances and a throttled primary, so the success line, the retry
    warning and the failure line are all produced by one call. `DEBUG`, so the success
    line is visible.

    The companions are the point again: stdout carried a provider line, and it carried the
    allowlisted label -- so the absence below is an absence in a log that ran, and the
    label that is present is the one the provider chose rather than a fallback.
    """
    production_logging(log_level="DEBUG")
    requested = [
        BIP173_TESTNET_P2WPKH,
        BIP173_TESTNET_P2WPKH_UPPERCASE.lower(),
        CORE_SIGNET_P2PKH,
        BIP350_TESTNET_V1,
    ]
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=429)),
        fallback=ScriptedInstance(Reply(funded=100_000)),
    )
    provider, client = esplora_provider(fake, max_attempts=2)

    async with client:
        balances = await provider.fetch_balances(list(dict.fromkeys(requested)))

    written = capsys.readouterr().out

    assert [balance.confirmed for balance in balances] == [100_000] * 3
    assert_carried_something(written, marker=SUCCESS_EVENT)
    assert_carried_something(written, marker=RETRY_EVENT)
    assert_carried_something(written, marker=ADDRESS_BALANCE)
    assert_absent(written, *requested)
    assert "/address/" not in written


async def test_a_health_check_logs_no_address_and_names_its_own_endpoint(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """The health endpoint names no address, so its log line must name none either.

    It is the endpoint an operations view calls on a schedule, which makes it the most
    frequently logged line in the system -- and a line that appears every minute is the
    one somebody eventually enriches with "which chain, which wallet" to make a dashboard
    work.
    """
    production_logging(log_level="DEBUG")
    fake = EsploraFake(primary=ScriptedInstance(Reply()))
    provider, client = esplora_provider(fake)

    async with client:
        health = await provider.health()

    written = capsys.readouterr().out

    assert health.healthy is True
    assert_carried_something(written, marker=BLOCK_TIP_HEIGHT)
    assert_absent(written, BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V1_KEY)
    assert "/blocks/tip/height" not in written


# --------------------------------------------------------------------------------------
# The controls: this test can fail, and it is reading the right stream
# --------------------------------------------------------------------------------------


async def test_the_stdout_capture_really_captures_a_provider_log_line(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Without this, every assertion above is a claim about an empty string.

    `configure_logging` goes through `logging.basicConfig`, which binds whatever object
    `sys.stdout` names at that moment. Install the pipeline at the wrong time and
    `capsys.readouterr()` returns nothing -- and "the address is not in the output" passes
    against nothing, forever.
    """
    production_logging(log_level="DEBUG")

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 200)

    written = capsys.readouterr().out

    assert written.strip(), "stdout was empty; the logging pipeline is not the one under test"
    line = json.loads(next(one for one in written.splitlines() if SUCCESS_EVENT in one))
    assert line["event"] == SUCCESS_EVENT
    assert line["target"] == f"{ORIGIN}/{ENDPOINT_LABEL}"
    assert line["status"] == 200


def test_the_absence_assertion_would_notice_an_address_on_stdout() -> None:
    """The helper is proven able to fail, so a green run means it looked and found nothing.

    Both spellings and the twenty-character prefix, because a log that printed a truncated
    address would still be a disclosure -- twenty characters of a bech32 address is unique
    on chain and is enough to search an explorer with.
    """
    leaked = f'{{"event": "provider_request", "url": "/api/address/{BIP173_TESTNET_P2WPKH}/utxo"}}'

    with pytest.raises(AssertionError, match=r"an address reached the log"):
        assert_absent(leaked, BIP173_TESTNET_P2WPKH)

    truncated = f'{{"event": "x", "url": "{BIP173_TESTNET_P2WPKH[:20]}..."}}'
    with pytest.raises(AssertionError, match=r"an address reached the log"):
        assert_absent(truncated, BIP173_TESTNET_P2WPKH)

    lowered = f'{{"event": "x", "url": "{BIP173_TESTNET_P2WPKH_UPPERCASE.lower()}"}}'
    with pytest.raises(AssertionError, match=r"an address reached the log"):
        assert_absent(lowered, BIP173_TESTNET_P2WPKH_UPPERCASE)


def test_the_companion_assertion_would_notice_silence() -> None:
    """And the other half: an empty capture must fail rather than pass quietly."""
    with pytest.raises(AssertionError, match=r"nothing was written to stdout"):
        assert_carried_something("", marker=SUCCESS_EVENT)

    with pytest.raises(AssertionError, match=r"the log under test never ran"):
        assert_carried_something('{"event": "something_else"}', marker=SUCCESS_EVENT)


# --------------------------------------------------------------------------------------
# A refusal has to be visible at the level the Pi actually runs at
# --------------------------------------------------------------------------------------
#
# A 400, a 401, a 404, or a 503 on a POST all returned through the debug branch, so at the
# production default of INFO a provider could be refused on every single call and the log
# would say nothing at all. The rule is now: status >= 400 logs `provider_request_failed`
# at error; everything else logs `provider_request` at debug.


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_a_failing_response_is_logged_at_error_and_not_swallowed_by_the_level(
    status: int,
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Visible at INFO, which is what production runs at, and still carrying no address.

    Both halves matter and they pull against each other: making a refusal visible means
    logging more about it, and logging more about a request is exactly how an address
    reaches a log. So the level is asserted *and* the address is asserted absent.
    """
    production_logging()

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), status, max_attempts=1)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=FAILURE_EVENT)
    assert '"level": "error"' in written or '"level":"error"' in written, written[:400]
    assert_absent(written, BIP173_TESTNET_P2WPKH)
    assert SENTINEL_API_KEY not in written
    assert SENTINEL_SIGNATURE not in written


async def test_a_successful_response_stays_at_debug_and_is_quiet_in_production(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """The control, and the reason the rule is a threshold rather than "log everything".

    A line per successful request at INFO would be one line per address per poll, forever,
    on a Raspberry Pi -- which is both noise and a standing invitation to put something
    identifying in it. Success stays at debug; only a refusal earns a production line.
    """
    production_logging()

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 200)

    written = capsys.readouterr().out

    assert SUCCESS_EVENT not in written
    assert FAILURE_EVENT not in written
    assert_absent(written, BIP173_TESTNET_P2WPKH)


async def test_the_success_line_is_still_there_when_someone_turns_debug_on(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[..., None],
) -> None:
    """Quiet in production is not the same as absent. The debug line still has to exist.

    Without this, "success stays at debug" would be satisfied by deleting the success log
    entirely, and an operator turning DEBUG on to investigate would get nothing.
    """
    production_logging(log_level="DEBUG")

    await drive(esplora_style_url(BIP173_TESTNET_P2WPKH), 200)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=SUCCESS_EVENT)
    assert_absent(written, BIP173_TESTNET_P2WPKH)
