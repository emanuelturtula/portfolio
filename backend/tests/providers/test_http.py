"""Criterion 3: timeouts, bounded retry with jitter, and the rate limiter in the path.

Every assertion here is about a count or an exact integer of milliseconds. Nothing waits,
nothing measures elapsed time, and nothing would give a different answer on a loaded CI
runner than on a laptop -- the sleep, the clock and the jitter are all injected, which is
the only reason that is possible.

The retry behaviour is asserted on **how many times the inner transport was entered**,
never on how many responses the caller saw. A caller sees one response either way; the
difference between "retried twice" and "not retried at all" is only visible from below.
"""

from __future__ import annotations

import inspect
import secrets
from typing import Final

import httpx
import pytest

from portfolio.providers import http as provider_http
from portfolio.providers.http import (
    CONNECT_TIMEOUT_MS,
    DEFAULT_MIN_HOST_INTERVAL_MS,
    DEFAULT_RETRY_POLICY,
    POOL_TIMEOUT_MS,
    READ_TIMEOUT_MS,
    RETRYABLE_STATUSES,
    WRITE_TIMEOUT_MS,
    HostRateLimiter,
    RetryingTransport,
    RetryPolicy,
    build_http_client,
    sleep_ms,
)
from tests.providers.harness import (
    MILLISECONDS_PER_SECOND,
    TEST_ORIGIN,
    FakeClock,
    RecordingSleep,
    client_with_the_shipped_defaults,
    fast_policy,
    perform,
    retrying_client,
    scripted_handler,
    scripted_transport,
    top_of_range_jitter,
    zero_jitter,
)

#: The four `httpx.Timeout` slots. All four, because `httpx.Timeout(10.0)` sets all four
#: too -- so "a timeout is set" and "each of the four was decided" are different claims,
#: and only the second is criterion 3.
TIMEOUT_FIELDS: Final[tuple[str, ...]] = ("connect", "read", "write", "pool")


def unset_timeouts(timeout: httpx.Timeout) -> list[str]:
    """Which of the four slots carry no deadline at all."""
    return [field for field in TIMEOUT_FIELDS if getattr(timeout, field) is None]


# --------------------------------------------------------------------------------------
# Timeouts
# --------------------------------------------------------------------------------------


def test_every_timeout_is_set_not_only_the_default() -> None:
    """All four slots carry a deadline, and each is the package's own number.

    httpx's default is five seconds on each slot and no total ceiling, which sounds
    adequate until a public API accepts the connection and then stalls: the read timeout
    resets on every byte, so a server dribbling one byte a second holds the sync open
    indefinitely. Asserting the four against the module's millisecond constants is what
    distinguishes "decided" from "inherited" -- a client that had been configured with no
    timeout at all would still pass a check for `is not None`.
    """
    client = build_http_client()

    assert unset_timeouts(client.timeout) == []
    assert client.timeout.connect == CONNECT_TIMEOUT_MS / MILLISECONDS_PER_SECOND
    assert client.timeout.read == READ_TIMEOUT_MS / MILLISECONDS_PER_SECOND
    assert client.timeout.write == WRITE_TIMEOUT_MS / MILLISECONDS_PER_SECOND
    assert client.timeout.pool == POOL_TIMEOUT_MS / MILLISECONDS_PER_SECOND


def test_the_four_timeouts_are_not_all_the_same_number() -> None:
    """The control for the test above, and a statement about the design.

    If all four were equal, the assertions above would be satisfied by
    `httpx.Timeout(5.0)` -- the library default -- and the "all four explicit" claim would
    be indistinguishable from having set nothing. They are not equal because a chain index
    answering a batch legitimately takes longer than a handshake does, which is exactly
    the reason the four are separate knobs.
    """
    values = {CONNECT_TIMEOUT_MS, READ_TIMEOUT_MS, WRITE_TIMEOUT_MS, POOL_TIMEOUT_MS}

    assert len(values) > 1, "four identical timeouts are the library default wearing a name"
    assert all(value > 0 for value in values)
    # The ordering, which is the actual decision. A mutation that set the read timeout
    # equal to the connect timeout survived every other assertion in this file: `len > 1`
    # still held, because two of the other three differed. The relationship is what the
    # source comment claims and what an operator would be changing, so it is what is
    # pinned -- rather than the numbers themselves, which are a guess that a measurement
    # should be free to move.
    assert READ_TIMEOUT_MS > CONNECT_TIMEOUT_MS, (
        "the read timeout must be the generous one: a chain index answering a batch "
        "legitimately takes longer than a TCP handshake does"
    )
    assert READ_TIMEOUT_MS > WRITE_TIMEOUT_MS


def test_the_unset_timeout_helper_can_actually_fail() -> None:
    """The control for the helper: a client with no deadlines reports all four."""
    assert unset_timeouts(httpx.Timeout(None)) == list(TIMEOUT_FIELDS)
    assert unset_timeouts(httpx.Timeout(connect=1.0, read=None, write=1.0, pool=1.0)) == ["read"]


def test_every_duration_the_module_declares_is_an_integer_of_milliseconds() -> None:
    """Rule 2 bans `float` in `providers/`, so a duration cannot be `0.25` seconds.

    Asserted over whatever `*_MS` constants the module declares rather than against a list
    of names, so a duration added later is covered without this test being edited -- and
    one added as a float fails here as well as in the AST ban, with a message naming it.
    """
    durations = {
        name: value
        for name, value in vars(provider_http).items()
        if name.isupper() and name.endswith("_MS")
    }

    assert durations, "providers/http.py declares no `*_MS` duration constant at all"
    wrong = {
        name: value
        for name, value in durations.items()
        if isinstance(value, bool) or not isinstance(value, int)
    }
    assert wrong == {}, f"these durations are not integer milliseconds: {wrong}"


def test_the_client_is_wired_through_the_retrying_transport() -> None:
    """The retry lives in the transport so no caller can forget to use it.

    A helper function a provider has to remember to call is the failure mode rule 8 was
    written against. Asserting the *type* of the client's transport is how that stays
    true: a factory that returned a plain client with a retry helper beside it would pass
    every behavioural test in this file that used the helper, and none of the requests
    that did not.
    """
    client = build_http_client()

    assert isinstance(client._transport, RetryingTransport)


def test_a_redirect_is_not_followed() -> None:
    """A followed redirect is a request the limiter never paced, to a host we did not pick.

    It is also how a compromised or misconfigured index could point the client at an
    origin of its choosing, carrying whatever the provider was about to send.
    """
    client = build_http_client()

    assert client.follow_redirects is False


# --------------------------------------------------------------------------------------
# What is retried, and what is not
# --------------------------------------------------------------------------------------


def test_the_retryable_statuses_are_the_throttle_and_the_server_errors() -> None:
    """Pinned as membership, so the set is a fact and not a predicate nobody read.

    404 and 400 are in here as non-members on purpose: the boundary of the 5xx range is
    where an off-by-one would put 499 or leave out 599.
    """
    assert 429 in RETRYABLE_STATUSES
    assert {500, 502, 503, 504, 599} <= RETRYABLE_STATUSES
    assert RETRYABLE_STATUSES.isdisjoint({200, 301, 400, 401, 403, 404, 409, 422, 499})


@pytest.mark.parametrize(
    "status",
    [429, 500, 502, 503, 504],
    ids=["throttled", "server error", "bad gateway", "unavailable", "gateway timeout"],
)
async def test_a_server_error_and_a_throttle_are_retried(status: int) -> None:
    """Criterion 3's retry, on the two families that are worth retrying.

    A 429 and a 5xx both mean "ask again later". Everything else in the 4xx family means
    "this request was wrong", and asking again produces the same wrong request.
    """
    inner, requests = scripted_transport(status, 200)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 200
    assert len(requests) == 2
    assert len(sleep.slept_ms) == 1


@pytest.mark.parametrize(
    "status",
    [400, 401, 403, 404, 409, 410, 422],
    ids=["bad", "unauthorised", "forbidden", "missing", "conflict", "gone", "unprocessable"],
)
async def test_a_client_error_is_returned_not_retried(status: int) -> None:
    """A 400 retried three times is three identical wrong requests.

    401 and 403 matter most: a wrong or revoked API key retried on a schedule is how an
    account gets throttled or locked out for a fault one attempt had already established.
    """
    inner, requests = scripted_transport(status)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == status
    assert len(requests) == 1
    assert sleep.slept_ms == []


async def test_a_success_is_not_retried_and_never_sleeps() -> None:
    """The happy path costs one request and no delay. The floor everything else sits on."""
    inner, requests = scripted_transport(200)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 200
    assert len(requests) == 1
    assert sleep.slept_ms == []


async def test_a_transport_error_is_retried() -> None:
    """A connection reset on a home broadband link is the ordinary case, not the exotic one.

    `httpx.AsyncHTTPTransport(retries=...)` covers exactly this and nothing else, which is
    why it was not enough on its own -- but it still has to be covered.
    """
    inner, requests = scripted_transport(httpx.ConnectError("refused"), 200)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 200
    assert len(requests) == 2


async def test_a_post_is_not_retried_unless_the_policy_opts_in() -> None:
    """Retrying a non-idempotent method can apply the same side effect twice.

    Kaspa's batch balance endpoint is a read expressed as a `POST`, so #8 will opt in --
    explicitly, as a visible line in a diff, which is this project's rule for making
    something less safe. The default must not do it for them.
    """
    inner, requests = scripted_transport(500, 200)
    async with retrying_client(inner) as client:
        outcome = await perform(client, "POST")

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 500
    assert len(requests) == 1

    opted_in, opted_in_requests = scripted_transport(500, 200)
    policy = fast_policy(retry_methods=frozenset({"GET", "HEAD", "POST"}))
    async with retrying_client(opted_in, policy=policy) as client:
        opted_outcome = await perform(client, "POST")

    assert isinstance(opted_outcome, httpx.Response), opted_outcome
    assert opted_outcome.status_code == 200
    assert len(opted_in_requests) == 2


async def test_a_transport_error_on_a_non_idempotent_method_is_not_retried_either() -> None:
    """The arm a status-only test misses: a `POST` that never got an answer.

    This is the case the method allowlist exists for. The server may have applied the
    request and failed on the way back, and the transport cannot tell -- so it must not
    guess, and it must not swallow the error either.
    """
    inner, requests = scripted_transport(httpx.ConnectError("refused"))
    async with retrying_client(inner) as client:
        outcome = await perform(client, "POST")

    assert isinstance(outcome, httpx.ConnectError), outcome
    assert len(requests) == 1


def test_the_retry_methods_default_to_the_idempotent_pair() -> None:
    """Pinned as a literal. A default that grew a method would be a silent safety change."""
    assert RetryPolicy().retry_methods == frozenset({"GET", "HEAD"})


@pytest.mark.parametrize("max_attempts", [1, 2, 3, 5])
async def test_the_attempt_count_is_a_ceiling(max_attempts: int) -> None:
    """`max_attempts` is total attempts, not retries on top of one.

    Off by one here is the difference between three requests and four, which nobody
    notices until an API's quota does. `max_attempts=1` is the boundary that says the
    number means attempts: it must produce exactly one request and no sleep at all.
    """
    inner, requests = scripted_transport(503)
    sleep = RecordingSleep()
    policy = fast_policy(max_attempts=max_attempts)
    async with retrying_client(inner, policy=policy, sleep=sleep) as client:
        await perform(client)

    assert len(requests) == max_attempts
    assert len(sleep.slept_ms) == max_attempts - 1


@pytest.mark.parametrize("max_attempts", [0, -1])
def test_a_policy_that_could_never_make_a_request_is_refused(max_attempts: int) -> None:
    """`max_attempts = 0` has no last attempt, so the request loop would never run.

    Caught at construction, where the wrong number is, rather than as a request that
    silently does nothing.
    """
    with pytest.raises(ValueError, match=r"max_attempts"):
        RetryPolicy(max_attempts=max_attempts)


async def test_an_exhausted_retry_returns_the_last_response_rather_than_raising() -> None:
    """The transport hands the final 503 back; deciding what it means is the provider's job.

    Asserted because it is a contract and not an accident: a transport that raised here
    would make every provider wrap its calls in a `try`, and a provider that wanted to
    read the status of the final failure could not.
    """
    inner, requests = scripted_transport(503)
    policy = fast_policy(max_attempts=2)
    async with retrying_client(inner, policy=policy) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 503
    assert len(requests) == 2


async def test_an_exhausted_transport_error_propagates_rather_than_becoming_a_response() -> None:
    """The other half of the same contract: no answer is not an answer.

    A transport that turned a connection failure into a synthetic response would make
    "the chain said nothing" indistinguishable from "the chain said something unhelpful",
    and a zero balance would be reported for a host that was simply unreachable.
    """
    inner, requests = scripted_transport(httpx.ConnectError("refused"))
    policy = fast_policy(max_attempts=3)
    async with retrying_client(inner, policy=policy) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.TransportError), outcome
    assert len(requests) == 3


# --------------------------------------------------------------------------------------
# The backoff: full jitter, bounded, and `Retry-After` on top
# --------------------------------------------------------------------------------------


async def test_the_backoff_is_drawn_from_zero_to_the_exponential_bound() -> None:
    """Full jitter, not "the exponential plus a little random".

    Two assertions, and the first is the one that distinguishes the two forms. Under full
    jitter a draw of zero is a sleep of zero. Under `base + randbelow(base)` a draw of
    zero is a sleep of `base`, and every client that failed at the same moment wakes at
    the same moment -- the thundering herd the jitter was added to break up.

    The second assertion is the schedule: the bound handed to the draw doubles per
    attempt, so the *range* grows exponentially even though an individual draw may not.
    """
    inner, _ = scripted_transport(503)
    sleep = RecordingSleep()
    jitter, bounds = zero_jitter()
    policy = fast_policy(max_attempts=4, base_backoff_ms=100, max_backoff_ms=100_000)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        await perform(client)

    assert sleep.slept_ms == [0, 0, 0], "a zero draw must be a zero sleep under full jitter"
    assert bounds == [100, 200, 400]


async def test_the_top_of_the_jitter_range_is_the_bound_itself() -> None:
    """The other end of the range, so the draw is used and not merely requested.

    A transport that called the jitter, threw the answer away and slept the full bound
    would satisfy the zero-draw assertion only if it ignored the draw -- and fails here.
    """
    inner, _ = scripted_transport(503)
    sleep = RecordingSleep()
    jitter, bounds = top_of_range_jitter()
    policy = fast_policy(max_attempts=3, base_backoff_ms=100, max_backoff_ms=100_000)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        await perform(client)

    assert bounds == [100, 200]
    assert sleep.slept_ms == [99, 199]


async def test_the_exponential_bound_is_capped_at_the_policy_ceiling() -> None:
    """Doubling is unbounded; the ceiling is what stops attempt six being half an hour."""
    inner, _ = scripted_transport(503)
    sleep = RecordingSleep()
    jitter, bounds = top_of_range_jitter()
    policy = fast_policy(max_attempts=5, base_backoff_ms=100, max_backoff_ms=250)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        await perform(client)

    assert bounds == [100, 200, 250, 250]
    assert max(sleep.slept_ms) < 250


async def test_a_zero_backoff_policy_never_asks_the_jitter_for_a_draw() -> None:
    """`secrets.randbelow(0)` raises, so a zero bound has to short-circuit.

    A policy with a zero base is legitimate -- "retry immediately" -- and it is exactly
    the configuration that would turn the real jitter source into a `ValueError` on the
    first failure, in production, on a path no happy-path test reaches.
    """
    inner, _ = scripted_transport(503)
    sleep = RecordingSleep()
    jitter, bounds = top_of_range_jitter()
    policy = fast_policy(max_attempts=3, base_backoff_ms=0, max_backoff_ms=0)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        await perform(client)

    assert bounds == [], "the jitter was asked for a draw below a bound of zero"
    assert sleep.slept_ms == [0, 0]


async def test_a_retry_after_header_is_honoured_instead_of_the_computed_backoff() -> None:
    """The server's own number wins, because it is the only one that knows its window.

    Asserted as the exact millisecond value rather than as "longer than the backoff": a
    transport that added the header to the computed delay, or that read it as milliseconds
    instead of seconds, would satisfy a comparison and fail this.
    """
    inner, requests = scripted_transport((429, {"Retry-After": "2"}), 200)
    sleep = RecordingSleep()
    jitter, _ = zero_jitter()
    policy = fast_policy(max_attempts=2, base_backoff_ms=100, max_backoff_ms=100_000)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 200
    assert len(requests) == 2
    assert sleep.slept_ms == [2 * MILLISECONDS_PER_SECOND]


async def test_an_absurd_retry_after_cannot_hang_the_sync_for_a_day() -> None:
    """`Retry-After: 86400` from a hostile or broken server is clamped to the ceiling.

    Unclamped, one bad header stalls the whole sync for as long as the header says, on a
    Raspberry Pi where nobody is watching. This is the difference between a legible delay
    and a process that looks hung.
    """
    inner, _ = scripted_transport((429, {"Retry-After": "86400"}), 200)
    sleep = RecordingSleep()
    policy = fast_policy(max_attempts=2, base_backoff_ms=100, max_backoff_ms=5_000)
    async with retrying_client(inner, policy=policy, sleep=sleep) as client:
        await perform(client)

    assert sleep.slept_ms == [5_000]


async def test_an_unparseable_retry_after_falls_back_to_the_computed_backoff() -> None:
    """A malformed header is not a reason to fail a request that would otherwise succeed.

    Raising on it would turn one broken server response into a hard failure of the sync;
    ignoring it costs nothing, because the computed backoff was always the fallback.
    """
    inner, requests = scripted_transport((429, {"Retry-After": "soon"}), 200)
    sleep = RecordingSleep()
    jitter, bounds = top_of_range_jitter()
    policy = fast_policy(max_attempts=2, base_backoff_ms=100, max_backoff_ms=100_000)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 200
    assert len(requests) == 2
    assert bounds == [100]
    assert sleep.slept_ms == [99]


async def test_a_server_cannot_make_us_retry_sooner_than_our_own_policy_would_have() -> None:
    """`Retry-After` is a floor on top of our backoff, not a replacement for it.

    This test previously asserted the opposite -- that `Retry-After: 0` produced a zero
    wait -- and that was a real hazard rather than a stricter reading: a server sending
    `0`, or a date that clock skew puts in the past, could drive every attempt back to
    back and turn our own retry budget into a burst. The contract now is
    `max(demanded, computed)`.

    **Asserted as a property, not as a floor value.** Full jitter can legitimately draw
    near zero, and a test demanding a non-zero wait would be a test against the design.
    What is asserted is the comparison: the wait is never below what our own policy would
    have chosen on its own.
    """
    inner, _ = scripted_transport((503, {"Retry-After": "0"}), 200)
    sleep = RecordingSleep()
    jitter, bounds = top_of_range_jitter()
    policy = fast_policy(max_attempts=2, base_backoff_ms=1_000, max_backoff_ms=100_000)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        await perform(client)

    # The policy was consulted, and its answer won over the server's smaller one.
    assert bounds == [1_000]
    assert sleep.slept_ms == [999]


async def test_a_server_asking_for_longer_than_our_backoff_still_gets_what_it_asked_for() -> None:
    """The other side of the floor: it raises a wait, it never lowers one.

    Ignoring a `Retry-After` is how a soft throttle becomes a ban, so `max` has to yield
    the server's number whenever the server asks for more -- which is the ordinary case.
    """
    inner, _ = scripted_transport((429, {"Retry-After": "5"}), 200)
    sleep = RecordingSleep()
    jitter, _ = top_of_range_jitter()
    policy = fast_policy(max_attempts=2, base_backoff_ms=100, max_backoff_ms=100_000)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        await perform(client)

    assert sleep.slept_ms == [5 * MILLISECONDS_PER_SECOND]


async def test_a_retry_after_date_already_in_the_past_cannot_produce_a_burst() -> None:
    """The realistic way a server demands zero: a date, and a clock that disagrees.

    `Retry-After` as an HTTP-date is resolved against our clock. A few seconds of skew, or
    a vendor whose clock is behind, turns a polite "wait until 12:00:05" into "wait no
    time at all" on every attempt. The floor is what stops that being a burst aimed at a
    server that is already struggling.
    """
    inner, _ = scripted_transport((503, {"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"}), 200)
    sleep = RecordingSleep()
    jitter, bounds = top_of_range_jitter()
    policy = fast_policy(max_attempts=2, base_backoff_ms=400, max_backoff_ms=100_000)
    async with retrying_client(inner, policy=policy, sleep=sleep, jitter=jitter) as client:
        await perform(client)

    assert bounds == [400]
    assert sleep.slept_ms == [399]


# --------------------------------------------------------------------------------------
# The limiter is in the path, not beside it
# --------------------------------------------------------------------------------------


async def test_the_rate_limiter_is_consulted_on_every_request_the_client_makes() -> None:
    """A limiter a caller has to remember to use is a limiter that gets forgotten.

    Driven through the client rather than by calling the limiter directly: the question
    here is whether the factory wired it into the path at all, which a direct call cannot
    answer. `tests/providers/test_rate_limiter.py` is where its own behaviour is pinned.
    """
    inner, requests = scripted_transport(200)
    sleep = RecordingSleep()
    clock = FakeClock()
    limiter = HostRateLimiter(min_interval_ms=250, clock=clock, sleep=sleep)
    async with retrying_client(inner, sleep=sleep, limiter=limiter) as client:
        await perform(client)
        await perform(client)

    assert len(requests) == 2
    # The clock never moved, so the second request is a full interval early and the
    # limiter has to have made it wait. No wall-clock time passed to measure.
    assert sleep.slept_ms == [250]


async def test_each_retry_attempt_goes_through_the_limiter_too() -> None:
    """A retry is a request. Retrying past the limiter is how a 429 becomes a ban.

    The failure this rules out is subtle: pace the first attempt, then hammer the host
    three more times in the same instant because the retry loop sits inside the limiter
    rather than outside it.
    """
    inner, requests = scripted_transport(503)
    sleep = RecordingSleep()
    clock = FakeClock()
    limiter = HostRateLimiter(min_interval_ms=40, clock=clock, sleep=sleep)
    jitter, _ = zero_jitter()
    policy = fast_policy(max_attempts=3, base_backoff_ms=0, max_backoff_ms=0)
    async with retrying_client(
        inner, policy=policy, sleep=sleep, jitter=jitter, limiter=limiter
    ) as client:
        await perform(client)

    assert len(requests) == 3
    # Three acquires: the first is free, the next two each wait a further interval,
    # interleaved with the two zero-length retry backoffs.
    assert sleep.slept_ms == [0, 40, 0, 80]


async def test_the_real_sleeper_converts_milliseconds_into_the_seconds_anyio_wants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other test in this package injects a fake sleep, so nothing runs the real one.

    That is the correct trade -- a suite that actually waited would be a suite somebody
    eventually skips -- but it leaves the one conversion out of integer milliseconds
    unexercised, and a factor-of-a-thousand mistake there is a retry that waits four
    minutes instead of a quarter of a second. Asserted as the value handed to `anyio`,
    not as "it was called".
    """
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr("anyio.sleep", record)

    await sleep_ms(250)

    assert recorded == [0.25]


async def test_the_real_sleeper_is_the_default_and_survives_a_zero_delay() -> None:
    """A zero wait has to be a checkpoint rather than an error -- and it is the default.

    `_backoff_ms` returns zero for a zero-backoff policy and the limiter computes zero for
    a caller already outside its window, so this is the argument the real sleeper is
    handed most often. The two signature assertions are what stop this suite proving
    things about an injected fake while production calls something else entirely.
    """
    await sleep_ms(0)

    transport_default = inspect.signature(RetryingTransport.__init__).parameters["sleep"]
    limiter_default = inspect.signature(HostRateLimiter.__init__).parameters["sleep"]

    assert transport_default.default is sleep_ms
    assert limiter_default.default is sleep_ms


# --------------------------------------------------------------------------------------
# The defaults the mechanism ships with
# --------------------------------------------------------------------------------------
#
# Every test above hands the transport a `fast_policy()`, so that a delay can be asserted
# as an exact integer and no test waits on a wall clock. That is the right discipline and
# it left a hole big enough to drive the whole feature through: none of those tests ever
# constructs `RetryPolicy()`, so `DEFAULT_RETRY_POLICY` -- the policy `build_http_client`
# uses when a caller passes none, which is what production will pass -- was unobserved.
#
# A mutation sweep set `max_attempts` to 1 and the entire suite stayed green. The retry
# subsystem could have shipped dead. These tests pin the shipped numbers, and pin them
# behaviourally wherever a behaviour can reach them, rather than only as literals.


def test_the_shipped_retry_policy_is_pinned_field_by_field() -> None:
    """Every field of `DEFAULT_RETRY_POLICY`, against a literal.

    A pin rather than a derivation: `DEFAULT_RETRY_POLICY == RetryPolicy()` is true for
    any defaults at all and says nothing. These are the numbers an operator's measurement
    would move, and moving one should be a visible line in a diff rather than a silent
    change to what every provider does.
    """
    assert RetryPolicy() == DEFAULT_RETRY_POLICY
    assert DEFAULT_RETRY_POLICY.max_attempts == 3
    assert DEFAULT_RETRY_POLICY.base_backoff_ms == 250
    assert DEFAULT_RETRY_POLICY.max_backoff_ms == 30_000
    assert DEFAULT_RETRY_POLICY.retry_methods == frozenset({"GET", "HEAD"})
    assert DEFAULT_RETRY_POLICY.retry_statuses == RETRYABLE_STATUSES
    assert DEFAULT_MIN_HOST_INTERVAL_MS == 250
    # The factory's own defaults, so a test that passes the same values back in is not
    # quietly substituting something else for what production uses.
    factory = inspect.signature(build_http_client).parameters
    assert factory["policy"].default is DEFAULT_RETRY_POLICY
    assert factory["jitter"].default is secrets.randbelow
    assert factory["sleep"].default is sleep_ms


async def test_a_client_built_with_no_policy_at_all_still_retries() -> None:
    """The gap, in one test: does the **default** decide to retry?

    `build_http_client()` with no `policy` is the call production makes. With
    `max_attempts=1` this fails and every other retry test in this file still passes,
    because each of those supplies its own policy. Only this one is looking at the
    shipped number.

    `sleep` is injected, so the default is allowed to decide without being allowed to
    wait.
    """
    inner, requests = scripted_transport(503, 200)
    sleep = RecordingSleep()
    async with client_with_the_shipped_defaults(inner, sleep=sleep) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 200
    assert len(requests) == 2
    assert len(sleep.slept_ms) == 1


async def test_the_shipped_attempt_budget_is_three_and_not_one_or_four() -> None:
    """The exact budget, observed through a client that was given no policy.

    Both neighbours matter. At one the retry subsystem is dead; at four a failing vendor
    is asked a fourth time on every poll, which is how a free public index starts
    refusing us. The number is a decision, so it is asserted rather than inherited.
    """
    inner, requests = scripted_transport(503)
    sleep = RecordingSleep()
    async with client_with_the_shipped_defaults(inner, sleep=sleep) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 503
    assert len(requests) == 3
    assert len(sleep.slept_ms) == 2


async def test_the_shipped_backoff_bounds_are_the_shipped_numbers() -> None:
    """`base_backoff_ms` observed as the schedule it produces, with no policy passed.

    `jitter` is injected here and nowhere else in this section, because the bound handed
    to the draw is the only place `base_backoff_ms` becomes visible from outside. The
    draw itself is irrelevant to what is being pinned.
    """
    inner, _ = scripted_transport(503)
    sleep = RecordingSleep()
    jitter, bounds = zero_jitter()
    async with client_with_the_shipped_defaults(inner, sleep=sleep, jitter=jitter) as client:
        await perform(client)

    assert bounds == [250, 500]


async def test_the_shipped_ceiling_is_what_clamps_an_absurd_retry_after() -> None:
    """`max_backoff_ms` behaviourally, which the attempt budget alone cannot reach.

    Three attempts at a 250 ms base never approach a 30 s ceiling, so the schedule can
    never show it. A server demanding a day can: the clamp is the shipped ceiling, to the
    millisecond. Without this the default ceiling is pinned only as a literal, and a
    literal pin cannot tell you the value is actually the one being applied.
    """
    inner, _ = scripted_transport((429, {"Retry-After": "86400"}), 200)
    sleep = RecordingSleep()
    async with client_with_the_shipped_defaults(inner, sleep=sleep) as client:
        await perform(client)

    assert sleep.slept_ms == [30_000]


async def test_a_client_built_with_no_policy_still_refuses_to_retry_a_post() -> None:
    """`retry_methods` observed through the default rather than through a literal.

    The literal pin above would survive a `build_http_client` that quietly widened the
    method set on its way past. This would not.
    """
    inner, requests = scripted_transport(500, 200)
    sleep = RecordingSleep()
    async with client_with_the_shipped_defaults(inner, sleep=sleep) as client:
        outcome = await perform(client, "POST")

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 500
    assert len(requests) == 1


async def test_a_client_built_with_no_policy_does_not_retry_a_404() -> None:
    """And `retry_statuses` the same way: the default must not retry a plain 4xx."""
    inner, requests = scripted_transport(404)
    sleep = RecordingSleep()
    async with client_with_the_shipped_defaults(inner, sleep=sleep) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 404
    assert len(requests) == 1
    assert sleep.slept_ms == []


async def test_a_client_built_with_no_limiter_paces_itself_at_the_shipped_interval() -> None:
    """`DEFAULT_MIN_HOST_INTERVAL_MS` reaching a real request, with no limiter passed.

    `RetryingTransport` builds its own limiter when given none, sharing the injected
    sleep -- so the shipped interval is observable without a wall clock and without the
    test constructing the limiter it is trying to check.
    """
    inner, requests = scripted_transport(200)
    sleep = RecordingSleep()
    client = build_http_client(transport=inner, sleep=sleep)
    async with client:
        await perform(client)
        await perform(client)

    assert len(requests) == 2
    assert len(sleep.slept_ms) == 1
    # A real monotonic clock is running here, so the second request is paced by very
    # slightly less than a full interval. The bound is what is asserted, not a duration:
    # nothing here waits, and nothing here depends on how fast the host is.
    assert 0 < sleep.slept_ms[0] <= DEFAULT_MIN_HOST_INTERVAL_MS


async def test_all_four_timeouts_reach_the_request_and_not_only_the_client() -> None:
    """The timeouts observed on a request the transport actually received.

    `client.timeout` says what the object holds. This says what a request carries:
    `httpx` renders the four onto `request.extensions["timeout"]` when it builds one, and
    that dict is what the network transport reads. A client configured correctly whose
    per-request timeout was overridden somewhere in between would satisfy every assertion
    about `client.timeout` and time out on the default anyway.

    It is also the only way the four numbers are observable without waiting for one. A
    mutation sweep confirmed the gap: with the literal assertions deselected, changing any
    of the four left the whole suite green, because every other timeout assertion is
    derived from the same constant it is checking.
    """
    inner, requests = scripted_transport(200)
    sleep = RecordingSleep()
    client = build_http_client(transport=inner, sleep=sleep)
    async with client:
        await perform(client)

    assert requests[0].extensions["timeout"] == {
        "connect": CONNECT_TIMEOUT_MS / MILLISECONDS_PER_SECOND,
        "read": READ_TIMEOUT_MS / MILLISECONDS_PER_SECOND,
        "write": WRITE_TIMEOUT_MS / MILLISECONDS_PER_SECOND,
        "pool": POOL_TIMEOUT_MS / MILLISECONDS_PER_SECOND,
    }
    # The shipped seconds, written out, so the assertion above cannot agree with a
    # constant that moved. Together they say the number is both applied and intended.
    assert requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 20.0,
        "write": 10.0,
        "pool": 5.0,
    }


# --------------------------------------------------------------------------------------
# Two vendors on one host
# --------------------------------------------------------------------------------------


async def test_two_services_on_one_host_but_different_ports_do_not_share_a_budget() -> None:
    """A self-hosted Esplora and a self-hosted Kaspa node on the same Pi are two vendors.

    Keyed on the hostname alone they share one interval, so adding the second chain
    halves the rate of the first for no reason -- and the deployment this application
    actually targets is exactly that: several services on one machine, told apart by port.

    Driven through the client rather than the limiter, so it holds whatever the internal
    key is named.
    """
    inner, requests = scripted_transport(200)
    sleep = RecordingSleep()
    clock = FakeClock()
    limiter = HostRateLimiter(min_interval_ms=250, clock=clock, sleep=sleep)
    async with retrying_client(inner, sleep=sleep, limiter=limiter) as client:
        await perform(client, url="https://self.test:3002/anything")
        await perform(client, url="https://self.test:16110/anything")

    assert len(requests) == 2
    assert sleep.slept_ms == []


async def test_the_default_port_and_its_explicit_spelling_are_one_service() -> None:
    """`https://x.test` and `https://x.test:443` are the same endpoint, so one budget.

    The counterpart to the test above, and the one a naive "key on the whole netloc" gets
    wrong: it would treat the two spellings as different vendors and double our rate
    against a host that only ever saw one. `httpx` normalises the default port away, so
    the key does too.
    """
    inner, requests = scripted_transport(200)
    sleep = RecordingSleep()
    clock = FakeClock()
    limiter = HostRateLimiter(min_interval_ms=250, clock=clock, sleep=sleep)
    async with retrying_client(inner, sleep=sleep, limiter=limiter) as client:
        await perform(client, url="https://one.test/anything")
        await perform(client, url="https://one.test:443/anything")

    assert len(requests) == 2
    assert sleep.slept_ms == [250]


async def test_different_hosts_on_the_same_port_are_still_separate() -> None:
    """The keying must not collapse to the port either."""
    inner, requests = scripted_transport(200)
    sleep = RecordingSleep()
    clock = FakeClock()
    limiter = HostRateLimiter(min_interval_ms=250, clock=clock, sleep=sleep)
    async with retrying_client(inner, sleep=sleep, limiter=limiter) as client:
        await perform(client, url="https://one.test:8443/anything")
        await perform(client, url="https://two.test:8443/anything")

    assert len(requests) == 2
    assert sleep.slept_ms == []


# --------------------------------------------------------------------------------------
# The harness itself
# --------------------------------------------------------------------------------------


def test_the_scripted_handler_repeats_its_last_outcome() -> None:
    """A test that trusts its scaffolding without checking it is trusting nothing.

    `scripted_transport(500)` has to mean "always 500" for the attempt-ceiling tests to be
    about the ceiling. If it instead ran out and raised `IndexError` on the second call,
    those tests would still go red -- but for the wrong reason, and the message would send
    somebody to read the retry code.
    """
    handler, requests = scripted_handler(503, 200)
    request = httpx.Request("GET", f"{TEST_ORIGIN}/anything")

    statuses = [handler(request).status_code for _ in range(3)]

    assert statuses == [503, 200, 200]
    assert len(requests) == 3
