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
import json
import secrets
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.providers import http as provider_http
from portfolio.providers.http import (
    ADDRESS_BALANCES,
    CONNECT_TIMEOUT_MS,
    DEFAULT_MIN_HOST_INTERVAL_MS,
    DEFAULT_RETRY_POLICY,
    DEFAULT_TIMEOUT,
    ENDPOINT_EXTENSION,
    IDEMPOTENT_EXTENSION,
    POOL_TIMEOUT_MS,
    READ_TIMEOUT_MS,
    RETRYABLE_STATUSES,
    WRITE_TIMEOUT_MS,
    HostRateLimiter,
    RetryingTransport,
    RetryPolicy,
    build_http_client,
    sleep_ms,
    utc_now,
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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


# --------------------------------------------------------------------------------------
# Criterion 10 of #8: a read expressed as a POST, retried without widening the policy
# --------------------------------------------------------------------------------------
#
# Kaspa's batch balance call is `POST /addresses/balances`, which is a read. #6 anticipated
# it and proposed that #8 opt in by widening `RetryPolicy.retry_methods`. That is wrong now
# that the consequence is visible: the policy lives on the transport, the transport is
# process-wide by construction, and widening it would make **every** future `POST`
# retryable -- including an exchange request that places an order, where a retry after a
# transport error can double a trade. One provider's convenience would silently become
# another's duplicate.
#
# So the opt-in is per request, deny by default, and visible at the one call site it
# applies to. Same shape as the endpoint label: the default says nothing, and saying more
# is a deliberate edit.

#: What a batch read actually carries, so the replay assertion is about a real payload
#: rather than about an empty object that would compare equal to an empty replay.
BATCH_PAYLOAD: Final[dict[str, list[str]]] = {"addresses": ["alpha", "beta", "gamma"]}


def body_recording_transport(*statuses: int) -> tuple[httpx.MockTransport, list[bytes]]:
    """A transport that snapshots the request body **at each attempt**.

    The snapshot is the whole point and it cannot be taken from the recorded requests.
    `httpx` hands the *same* `Request` object to the transport on every attempt, so
    `requests[1].content == requests[0].content` is comparing an object with itself and is
    true however badly the replay went. The bytes have to be copied out while the attempt
    is in progress, which is what this does.
    """
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(bytes(request.content))
        status = statuses[min(len(bodies) - 1, len(statuses) - 1)]
        return httpx.Response(status, json={"ok": True})

    return httpx.MockTransport(handler), bodies


async def post_batch(
    client: httpx.AsyncClient, *, idempotent: bool | None = True
) -> httpx.Response | BaseException:
    """One batch-shaped `POST`, with or without the idempotence declaration."""
    extensions: dict[str, object] = {ENDPOINT_EXTENSION: ADDRESS_BALANCES}
    if idempotent is not None:
        extensions[IDEMPOTENT_EXTENSION] = idempotent
    try:
        return await client.post(
            f"{TEST_ORIGIN}/addresses/balances", json=BATCH_PAYLOAD, extensions=extensions
        )
    except BaseException as error:
        # Returned rather than propagated, for the reason `perform` gives: a test can then
        # assert on the number of attempts without also having to agree, in the same
        # assertion, on what an exhausted retry produces.
        return error


async def test_a_retried_idempotent_post_sends_the_same_body_again() -> None:
    """The assertion is on the **body of the second request**, and nothing weaker will do.

    `httpx` consumes a request stream on the first attempt. A streamed body therefore
    replays as empty, the server answers about **no addresses**, and `align_balances` reads
    every requested address as missing -- which it turns into a zero, because an address
    with no history holds nothing. So the balances come back *wrong rather than missing*,
    and every other assertion in this suite still passes: the request count is right, the
    retry happened, the result has the right length and the right addresses in the right
    order, and every balance is a plausible zero.

    Asserting that a retry happened is therefore not enough. This captures the bytes at
    each attempt and asserts the second attempt carried the first attempt's payload.
    """
    inner, bodies = body_recording_transport(503, 200)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        outcome = await post_batch(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 200
    assert len(bodies) == 2, "the idempotent POST was not retried at all"
    assert bodies[0], "the first attempt sent an empty body, so the replay proves nothing"
    assert bodies[1] == bodies[0]
    assert [json.loads(body) for body in bodies] == [BATCH_PAYLOAD, BATCH_PAYLOAD]


async def test_a_transport_error_on_an_idempotent_post_replays_the_body_too() -> None:
    """The arm a status-only test misses, and the one the stream bug actually lives in.

    A 503 arrives as a response, so the request object is untouched; a transport error can
    arrive after the stream has already been drained. Driving both is what separates "the
    body survived a response" from "the body survives".
    """
    bodies: list[bytes] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        bodies.append(bytes(request.content))
        if attempts == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"ok": True})

    sleep = RecordingSleep()
    async with retrying_client(httpx.MockTransport(handler), sleep=sleep) as client:
        outcome = await post_batch(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert [json.loads(body) for body in bodies] == [BATCH_PAYLOAD, BATCH_PAYLOAD]


class StreamReadingTransport(httpx.AsyncBaseTransport):
    """An inner transport that consumes `request.stream`, the way a real one does.

    **`httpx.MockTransport` cannot see the hazard criterion 10 is about.** Its
    `handle_async_request` calls `await request.aread()` before handing the request to its
    handler, and `Request.aread` caches the bytes on the request *and replaces a
    non-replayable stream with a `ByteStream`*. So under `MockTransport` a body that could
    never have been replayed replays perfectly, and a test built on it would be green
    whether or not `RetryingTransport` does the materialising itself.

    This transport does the one thing a real one does -- iterate the stream and send it --
    and nothing else. A stream that has already been drained therefore yields nothing here,
    which is exactly what the server would receive.
    """

    def __init__(self, *statuses: int) -> None:
        self._statuses = statuses
        self.bodies: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        stream = request.stream
        assert isinstance(stream, httpx.AsyncByteStream), "an async client sends an async stream"
        self.bodies.append(b"".join([part async for part in stream]))
        status = self._statuses[min(len(self.bodies) - 1, len(self._statuses) - 1)]
        return httpx.Response(status, json={"ok": True})


class OneShotStream:
    """A request body that yields its bytes once and nothing at all afterwards.

    **Not an async generator, and that is the whole reason it is written by hand.** `httpx`
    wraps a generator in an `AsyncIteratorByteStream` that remembers it is one and raises
    `StreamConsumed` on a second pass -- a loud failure, and therefore not the dangerous
    case. Any other async iterable is wrapped without that flag, so re-iterating it simply
    yields nothing and the request goes out with an **empty body**.

    That is the failure criterion 10 exists for: the server answers about no addresses,
    `align_balances` reads every requested address as absent and turns it into a zero, and
    the sync reports a portfolio of empty wallets with the right length, the right
    addresses, the right order and no error anywhere.
    """

    def __init__(self, payload: bytes) -> None:
        self._remaining = [payload]

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while self._remaining:
            yield self._remaining.pop(0)


async def test_a_streaming_body_is_materialised_so_the_retry_can_replay_it() -> None:
    """The mechanism behind criterion 10, driven where a mock transport cannot reach.

    `RetryingTransport` reads the body into bytes before its first attempt, which also
    replaces a non-replayable stream with a replayable one. Without that, the second
    attempt sends nothing -- silently, for the reason `OneShotStream` documents.

    The Kaspa provider passes `json=`, so its own body is already bytes and would replay
    without this. That is what makes this the test that keeps the materialisation honest:
    it is the only one in the suite that fails if the transport stops doing it, until the
    day somebody writes a provider that streams -- and that provider's author will not
    know this rule exists.
    """
    payload = b'{"addresses": ["alpha", "beta"]}'
    inner = StreamReadingTransport(503, 200)
    sleep = RecordingSleep()
    client = build_http_client(
        transport=inner,
        policy=fast_policy(),
        limiter=HostRateLimiter(min_interval_ms=0, clock=FakeClock(), sleep=sleep),
        jitter=zero_jitter()[0],
        sleep=sleep,
    )
    async with client:
        response = await client.post(
            f"{TEST_ORIGIN}/addresses/balances",
            content=OneShotStream(payload),
            extensions={ENDPOINT_EXTENSION: ADDRESS_BALANCES, IDEMPOTENT_EXTENSION: True},
        )

    assert response.status_code == 200
    assert inner.bodies == [payload, payload], (
        "the retried attempt did not carry the first attempt's body, so the server was "
        "asked about nothing and every balance would come back a plausible zero"
    )


async def test_the_stream_reading_transport_would_notice_a_drained_stream() -> None:
    """The control on the test above, and it is not optional.

    `StreamReadingTransport` and `OneShotStream` are the only things standing between
    criterion 10 and a test that passes because `MockTransport` quietly repaired the
    request. So the pair is driven with nothing materialising anything: the second read
    must come back **empty**, silently, with no exception. If it raised instead, or if it
    replayed, the test above would be green for a reason that has nothing to do with the
    transport under test.
    """
    payload = b'{"addresses": ["alpha"]}'
    inner = StreamReadingTransport(200, 200)
    request = httpx.Request(
        "POST", f"{TEST_ORIGIN}/addresses/balances", content=OneShotStream(payload)
    )

    await inner.handle_async_request(request)
    await inner.handle_async_request(request)

    assert inner.bodies == [payload, b""]


async def test_a_post_without_the_extension_is_still_not_retried() -> None:
    """Deny by default. The extension is the opt-in, and its absence is not a shrug.

    A `POST` is not retried by default because a transport error can arrive after the
    server already applied the request, and this transport cannot know which. The whole
    value of the per-request opt-in is that it leaves that true for every request that did
    not ask.
    """
    inner, bodies = body_recording_transport(503, 200)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        outcome = await post_batch(client, idempotent=None)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 503
    assert len(bodies) == 1
    assert sleep.slept_ms == []


@pytest.mark.parametrize(
    ("declared", "why"),
    [
        pytest.param("false", "a non-empty string, which is truthy", id="the string false"),
        pytest.param("true", "the flag as a string, which is what a header would be", id="string"),
        pytest.param(1, "an int, which is what a flag read out of JSON looks like", id="one"),
        pytest.param(["yes"], "a non-empty list", id="a list"),
        pytest.param(object(), "any object at all, which extensions permits", id="an object"),
    ],
)
async def test_only_the_boolean_true_opts_a_post_into_being_retried(
    declared: object, why: str
) -> None:
    """`is True`, not truthiness, and this is the row that proves the difference.

    `request.extensions` is a plain mapping of **anything**, so the value here is whatever
    a caller happened to put in it. Under a truthiness test every row below opts in -- and
    the first is the one that shows why that is not a stylistic preference: `"false"` is a
    non-empty string, so a provider that threaded a flag through as text would make a
    request retryable by saying it is not.

    Measured against the shipped transport: changing `is True` to `bool(...)` left the
    entire suite green, because `test_an_explicit_false_does_not_opt_in_either` covers only
    `False`, which is falsey under both readings. The claim the code, the spec and
    `docs/providers.md` all make had no test until this one.

    A single attempt is the assertion. The alternative -- a `POST` retried because somebody
    passed the wrong kind of truthy -- is a duplicated request, and the request this
    mechanism exists beside is an exchange order.
    """
    del why  # In the parameter id, where a failure can read it.
    inner, bodies = body_recording_transport(503, 200)
    sleep = RecordingSleep()
    extensions: dict[str, object] = {
        ENDPOINT_EXTENSION: ADDRESS_BALANCES,
        IDEMPOTENT_EXTENSION: declared,
    }
    async with retrying_client(inner, sleep=sleep) as client:
        response = await client.post(
            f"{TEST_ORIGIN}/addresses/balances", json=BATCH_PAYLOAD, extensions=extensions
        )

    assert response.status_code == 503
    assert len(bodies) == 1, f"{declared!r} opted the request in; only the boolean True may"
    assert sleep.slept_ms == []


async def test_an_explicit_false_does_not_opt_in_either() -> None:
    """`idempotent: False` is a request saying no, and it has to be heard as one.

    The realistic version is a provider that computes the flag -- `idempotent=is_read` --
    and gets `False` for a write. A transport that tested for the key's *presence* rather
    than for its value would retry exactly the request that said not to, which is the
    inverse of what the extension is for.
    """
    inner, bodies = body_recording_transport(503, 200)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        outcome = await post_batch(client, idempotent=False)

    assert isinstance(outcome, httpx.Response), outcome
    assert outcome.status_code == 503
    assert len(bodies) == 1


async def test_one_idempotent_post_does_not_make_the_next_one_retryable() -> None:
    """The opt-in is per request, not a flag the transport picks up and keeps.

    The transport is process-wide by construction, so a flag stored on it would be exactly
    the widening this design exists to avoid -- reached by accident instead of on purpose,
    and invisible in a diff. Two requests on **one client**, because that is the only
    arrangement in which the leak can happen.
    """
    inner, bodies = body_recording_transport(503)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        await post_batch(client)
        attempts_after_opted_in = len(bodies)
        await post_batch(client, idempotent=None)

    assert attempts_after_opted_in == 3, "the opted-in POST should have used the whole budget"
    assert len(bodies) - attempts_after_opted_in == 1, (
        "the second POST did not declare itself idempotent and was retried anyway, so the "
        "opt-in leaked onto the transport that every provider shares"
    )


async def test_a_get_is_retried_without_needing_the_extension() -> None:
    """The control. An implementation that required the extension for everything would
    pass every test above and quietly stop retrying every read in the system.
    """
    inner, requests = scripted_transport(503, 200)
    sleep = RecordingSleep()
    async with retrying_client(inner, sleep=sleep) as client:
        outcome = await perform(client)

    assert isinstance(outcome, httpx.Response), outcome
    assert len(requests) == 2


def test_the_idempotence_extension_is_a_named_constant_and_the_policy_still_excludes_post() -> None:
    """Both halves of criterion 10, pinned where a diff will show them.

    The extension name is a constant rather than a string repeated at each call site, for
    the same reason `ENDPOINT_EXTENSION` is: a typo produces a request that is silently not
    retried, which looks exactly like a vendor that answered on the first attempt.

    And `retry_methods` must still be the idempotent pair. The point of the per-request
    opt-in is that the process-wide policy did **not** have to change; a change here would
    mean #8 took the route #6 proposed and the spec rejected, and every future `POST` --
    an exchange order included -- would become retryable.
    """
    assert IDEMPOTENT_EXTENSION == "idempotent"
    assert "POST" not in DEFAULT_RETRY_POLICY.retry_methods
    assert DEFAULT_RETRY_POLICY.retry_methods == frozenset({"GET", "HEAD"})


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
# Criterion 4 of #8, at the join: a `ratelimit-*` header reaching the limiter
# --------------------------------------------------------------------------------------
#
# **This section exists because the criterion was proven in two halves and not at the
# seam between them.** `parse_rate_limit` is tested pure in
# `tests/providers/test_rate_limit_headers.py`, and `HostRateLimiter.observe` is tested by
# direct call in `tests/providers/test_rate_limiter.py`. Both were green while nothing
# anywhere drove a header through a `RetryingTransport` -- measured: deleting the four
# lines in `handle_async_request` that call `observe(..., parse_rate_limit(...))` left the
# whole suite passing.
#
# That is #6's lesson in a new costume. A codec and a consumer that are each correct and
# never wired together is a feature that ships dead with every test green, and the only
# assertion that can see it is one that starts at a response and ends at a sleep.
#
# Everything here is synthesised. Measured against the live Kaspa REST service on
# 2026-09-23, neither endpoint sends a `ratelimit-*` or `x-ratelimit-*` header at all,
# because the API sits behind Cloudflare. The criterion says "when present"; a self-hosted
# instance with no CDN in front of it is the deployment this path is for.

#: The limiter's ordinary spacing for these tests. Deliberately unlike every reset below,
#: so "it waited the reset" and "it waited its own interval" can never be the same number.
PACED_INTERVAL_MS: Final = 250


def paced_client(
    inner: httpx.MockTransport,
    sleep: RecordingSleep,
    *,
    max_backoff_ms: int = 30_000,
) -> httpx.AsyncClient:
    """The real client, over a limiter that paces, with the clock and the sleep injected.

    The limiter is a *real* `HostRateLimiter` rather than the open one the retry tests use,
    because an interval of zero would make "the hint was honoured" and "nothing waited at
    all" indistinguishable.
    """
    return retrying_client(
        inner,
        policy=fast_policy(max_backoff_ms=max_backoff_ms),
        sleep=sleep,
        limiter=HostRateLimiter(min_interval_ms=PACED_INTERVAL_MS, clock=FakeClock(), sleep=sleep),
    )


async def test_an_exhausted_budget_in_a_response_paces_the_next_request() -> None:
    """The join, end to end: headers on a response become the next request's wait.

    Two requests. The first is not delayed -- nothing is booked yet -- and its response
    says the budget for this host is spent and will reset in two seconds. The second must
    therefore wait **2000 ms and not the 250 ms interval**, which is the only pair of
    numbers that can tell "the header was honoured" from "the limiter did what it always
    does".

    A 200 rather than a 429 on purpose: the transport reads these headers on *every*
    response, because a rule that only applies to the failure path is a rule that arrives
    after the throttling has already started.
    """
    inner, requests = scripted_transport(
        (200, {"ratelimit-remaining": "0", "ratelimit-reset": "2"})
    )
    sleep = RecordingSleep()
    async with paced_client(inner, sleep) as client:
        await perform(client)
        await perform(client)

    assert len(requests) == 2
    assert sleep.slept_ms == [2000], (
        "the second request was paced by the limiter's own interval, so the response's "
        "ratelimit headers never reached the limiter"
    )


async def test_a_budget_with_requests_left_does_not_slow_the_next_one() -> None:
    """The control, and it is the case that actually happens on a server that sends these.

    Every response from such a server carries the headers, so a transport that paused
    whenever a hint arrived would run the whole sync at the vendor's advertised window
    rather than at its own interval. Only `remaining: 0` is an instruction to wait.

    Without this, a transport that ignored `remaining` entirely would pass the test above.
    """
    inner, requests = scripted_transport(
        (200, {"ratelimit-remaining": "5", "ratelimit-reset": "2"})
    )
    sleep = RecordingSleep()
    async with paced_client(inner, sleep) as client:
        await perform(client)
        await perform(client)

    assert len(requests) == 2
    assert sleep.slept_ms == [PACED_INTERVAL_MS]


async def test_an_absurd_reset_cannot_stall_the_sync_at_the_join_either() -> None:
    """The clamp reaches the wiring, not only the parser.

    `parse_rate_limit` takes `cap_ms` as an argument, so a transport that passed no cap --
    or passed the wrong one -- would honour a server asking us to wait a day, and the pure
    tests in `test_rate_limit_headers.py` would all still pass. The assertion is the
    policy's ceiling to the millisecond.
    """
    inner, _ = scripted_transport((200, {"ratelimit-remaining": "0", "ratelimit-reset": "86400"}))
    sleep = RecordingSleep()
    async with paced_client(inner, sleep, max_backoff_ms=1000) as client:
        await perform(client)
        await perform(client)

    assert sleep.slept_ms == [1000]


async def test_a_refusal_carrying_an_exhausted_budget_paces_the_next_request_too() -> None:
    """A 403 is where a throttled vendor most plausibly says this, and it is not retried.

    `observe` runs before the retry decision, so a response the policy will never retry
    still contributes its headers. A transport that read them only on the retry path would
    pass every test above -- all of which use a 200 the policy also does not retry, but
    which takes the same early return -- and would ignore precisely the responses a
    struggling server sends.
    """
    inner, requests = scripted_transport(
        (403, {"ratelimit-remaining": "0", "ratelimit-reset": "3"})
    )
    sleep = RecordingSleep()
    async with paced_client(inner, sleep) as client:
        first = await perform(client)
        await perform(client)

    assert isinstance(first, httpx.Response), first
    assert first.status_code == 403
    assert len(requests) == 2
    assert sleep.slept_ms == [3000]


async def test_a_response_with_no_rate_limit_headers_changes_no_pacing() -> None:
    """The other control: the production path, where no such header ever arrives.

    `parse_rate_limit` returns `None` and `observe` does nothing with it, so the limiter
    keeps its own interval. A transport that treated a missing header as an exhausted
    budget would pause every Kaspa read for a reset nobody asked for.
    """
    inner, _ = scripted_transport((200, {"server": "cloudflare", "cf-cache-status": "DYNAMIC"}))
    sleep = RecordingSleep()
    async with paced_client(inner, sleep) as client:
        await perform(client)
        await perform(client)

    assert sleep.slept_ms == [PACED_INTERVAL_MS]


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
    # 1000 since #7, up from the 250 #6 guessed at with no vendor documentation to read.
    # mempool.space states that exceeding its unpublished limits returns 429 and that
    # doing so repeatedly may get the caller banned; one request per second is the issue's
    # own floor. `test_the_default_interval_is_the_documented_floor`, in
    # `tests/providers/test_rate_limiter.py`, carries the reasoning and the exact spacing
    # this number produces.
    assert DEFAULT_MIN_HOST_INTERVAL_MS == 1000
    # The factory's own defaults, so a test that passes the same values back in is not
    # quietly substituting something else for what production uses.
    factory = inspect.signature(build_http_client).parameters
    assert factory["policy"].default is DEFAULT_RETRY_POLICY
    assert factory["jitter"].default is secrets.randbelow
    assert factory["sleep"].default is sleep_ms
    assert factory["timeout"].default is DEFAULT_TIMEOUT
    assert factory["now"].default is utc_now
    assert factory["transport"].default is None
    assert factory["limiter"].default is None


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
