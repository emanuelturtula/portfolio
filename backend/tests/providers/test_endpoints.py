"""The endpoint-failover loop, extracted out of `bitcoin.py` and now shared by two chains.

#7 gave `providers/chains/bitcoin.py` an ordered list of instances, sticky-within-a-call
failover, a `_Failure` record and a classification chosen by the *last* failure. #8 is the
second provider, and copying that loop would be the second invention of one thing -- which
`CLAUDE.md` names as exactly how two copies drift apart. So it moved here, and this file is
where it is tested on its own terms.

**This file is not the proof that the extraction was behaviour-preserving.**
`tests/providers/chains/test_bitcoin.py` is, and it is required to pass **untouched**: it
was written against the loop while the loop lived inside the provider, so it is the control
that says the move changed nothing. If it needs an edit to accommodate the shared loop, the
extraction changed behaviour and the change is unreviewed. Nothing in this file substitutes
for that, because a test written after a refactor can only ever describe what the refactor
produced.

What *is* new here is the shape the extraction had to grow for a second chain:

* a read expressed as a `POST`, because Kaspa's batch balance call is one;
* classification and cause-chaining asserted directly rather than through a provider, so a
  failure in either is reported as a failure in the loop rather than as a parsing bug.

The cause-chaining tests exist because of #7's fourth closing lesson: thirty-seven planned
tests, every one of them asserting an exception's *type*, and not one asking what it was
chained to -- while a `ProviderRateLimitedError` chained to a `ConnectError` from the other
instance sent whoever read the traceback after the wrong host.

Nothing here sleeps and nothing here reads a clock: the limiter, the jitter and the sleep
are injected, so no assertion in this file is a measurement of how fast the machine running
it happened to be.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.providers.endpoints import FALLBACK, PRIMARY, Endpoint, EndpointSet
from portfolio.providers.errors import (
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import (
    ADDRESS_BALANCE,
    ADDRESS_BALANCES,
    ENDPOINT_EXTENSION,
    IDEMPOTENT_EXTENSION,
    HostRateLimiter,
    RetryPolicy,
    build_http_client,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Fictional hosts. `example` is reserved by RFC 2606 and resolves nowhere, so a test that
#: somehow escaped its mock transport fails to connect rather than reaching somebody's real
#: index. Rule 3 forbids a real hostname in the repository regardless.
FIRST_HOST: Final = "first.example"
SECOND_HOST: Final = "second.example"
FIRST_URL: Final = f"https://{FIRST_HOST}/api"
SECOND_URL: Final = f"https://{SECOND_HOST}/api"

#: What the loop calls the vendor in an exhaustion message. A made-up name, so that an
#: assertion about the message cannot pass by accidentally matching "Esplora" or "Kaspa".
VENDOR: Final = "Testnet Index"

PATH: Final = "/things/1"
BODY: Final = '{"ok": true}'


async def no_sleep(_milliseconds: int) -> None:
    return


def client_over(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_attempts: int = 1,
) -> httpx.AsyncClient:
    """The production client over a scripted transport, with every duration injected.

    `build_http_client` rather than an assembled transport, because that is what #10 hands
    a provider and it is the wiring the failover composes with. One attempt by default,
    because this file is about **which endpoint was asked**; how many times a single
    endpoint is retried is `tests/providers/test_http.py`'s subject and is covered there.

    `max_attempts` is a parameter for exactly one pair of tests -- the ones that drive
    `idempotent=True` and `idempotent=False` against the same script, where the retry count
    *is* the observable difference and a budget of one would make the two indistinguishable.
    """
    return build_http_client(
        transport=httpx.MockTransport(handler),
        policy=RetryPolicy(max_attempts=max_attempts, base_backoff_ms=0, max_backoff_ms=0),
        limiter=HostRateLimiter(min_interval_ms=0, clock=lambda: 0, sleep=no_sleep),
        jitter=lambda bound: bound,
        sleep=no_sleep,
    )


def endpoint_set(client: httpx.AsyncClient, *urls: str, vendor: str = VENDOR) -> EndpointSet:
    """Build the set under test from base URLs, in order.

    One helper rather than the constructor spelled out in thirty tests, so that the shape
    of the factory is knowledge this module holds in a single place -- the same argument
    `tests/providers/harness.py` makes about `retrying_client`.
    """
    positions = (PRIMARY, FALLBACK)
    return EndpointSet.configured(client, tuple(zip(positions, urls, strict=True)), vendor=vendor)


class Recorder:
    """A scripted transport that records every request against the host that received it.

    The counting is the point, and it is the same point `tests/providers/chains/harness.py`
    makes at length: a loop that asked the first endpoint twenty times and then succeeded
    on the second returns exactly the same body as one that moved on after the first
    refusal, and only the per-host log tells them apart.
    """

    def __init__(self, first: object, second: object = 200) -> None:
        self._outcomes = {FIRST_HOST: first, SECOND_HOST: second}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self._outcomes[str(request.url.host)]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, str):
            return httpx.Response(200, content=outcome)
        assert isinstance(outcome, int)
        return httpx.Response(outcome, content=BODY)

    @property
    def hosts_in_order(self) -> list[str]:
        return [str(request.url.host) for request in self.requests]

    @property
    def counts(self) -> dict[str, int]:
        return {
            FIRST_HOST: self.hosts_in_order.count(FIRST_HOST),
            SECOND_HOST: self.hosts_in_order.count(SECOND_HOST),
        }


# --------------------------------------------------------------------------------------
# Which endpoints exist at all
# --------------------------------------------------------------------------------------


def test_an_endpoint_joins_its_base_url_to_a_path_by_concatenation() -> None:
    """The path always begins with a slash and the base URL never ends with one.

    A double slash is answered with a 404 by some reverse proxies and with a redirect by
    others, and the shared client does not follow redirects -- so the failure would be an
    endpoint that is configured correctly and never works.
    """
    endpoint = Endpoint(position=PRIMARY, base_url=FIRST_URL)

    assert endpoint.url(PATH) == f"{FIRST_URL}{PATH}"


def test_a_trailing_slash_on_a_base_url_cannot_produce_a_double_slash() -> None:
    """An operator's copy-and-paste adds one, and the set has to absorb it."""
    client = client_over(Recorder(200))
    endpoints = endpoint_set(client, f"{FIRST_URL}/", SECOND_URL)

    assert endpoints.endpoints[0].url(PATH) == f"{FIRST_URL}{PATH}"


@pytest.mark.parametrize(
    ("urls", "expected", "why"),
    [
        pytest.param((FIRST_URL, SECOND_URL), 2, "two different vendors", id="two"),
        pytest.param((FIRST_URL, ""), 1, "a blank fallback is one instance only", id="one blank"),
        pytest.param(
            ("", SECOND_URL), 1, "a blank primary still leaves a fallback", id="blank 1st"
        ),
        pytest.param(("", ""), 0, "nothing configured at all", id="both blank"),
        pytest.param((FIRST_URL, FIRST_URL), 1, "the same index named twice", id="identical"),
        pytest.param((FIRST_URL, f"{FIRST_URL}/"), 1, "the same index, one slash", id="slash"),
        pytest.param((FIRST_URL, f"  {FIRST_URL}  "), 1, "the same index, padded", id="padded"),
    ],
)
def test_the_configured_endpoints_drop_blanks_and_repeats(
    urls: tuple[str, str], expected: int, why: str
) -> None:
    """The rules #7 argued for, now applied to every provider rather than to one.

    **The two URLs being the same is not a fallback, and treating it as one is actively
    harmful.** A self-hoster who points both variables at their own index -- which people
    do, because two variables look like they both want filling -- would otherwise get a
    "fallback" that is the same host: a 429 costs the retry budget, and then the failover
    spends it again on the host that has just asked us to stop.

    Compared after trimming and after the trailing slash is removed, because the realistic
    version of this mistake is a copy-and-paste that picked up a slash or a space.
    """
    del why  # In the parameter id, where a failure can read it.
    client = client_over(Recorder(200))

    assert len(endpoint_set(client, *urls).endpoints) == expected


def test_the_first_spelling_wins_so_the_survivor_is_the_primary() -> None:
    """An operator who configured one index twice gets `primary`, not `fallback`.

    Order is preserved and the earlier entry is kept. The position is what reaches a
    `ProviderHealth.detail`, so the other choice would tell an operator their fallback
    answered when they have no fallback at all.
    """
    client = client_over(Recorder(200))

    endpoints = endpoint_set(client, FIRST_URL, f"{FIRST_URL}/")

    assert [endpoint.position for endpoint in endpoints.endpoints] == [PRIMARY]


def test_the_positions_are_the_two_names_an_operator_is_ever_shown() -> None:
    """Pinned as literals, because they reach a log and an operations view.

    A position rather than a URL: `detail` must never name the deployment. Pinning the
    strings is what stops somebody "improving" them into something derived from the URL.
    """
    assert PRIMARY == "primary"
    assert FALLBACK == "fallback"


# --------------------------------------------------------------------------------------
# Reading: which endpoint answered, and where the next read starts
# --------------------------------------------------------------------------------------


async def test_a_read_returns_the_body_and_the_index_that_answered() -> None:
    """The index is the whole of the sticky-failover mechanism.

    The caller carries it into the next read as `start`, so an endpoint that failed is
    never asked again within one call and nothing has to remember to skip it. A loop that
    returned only the body would make stickiness the caller's problem, which is how the two
    providers would end up disagreeing about it.
    """
    recorder = Recorder(BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        body, index = await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    assert body == BODY
    assert index == 0
    assert recorder.counts == {FIRST_HOST: 1, SECOND_HOST: 0}


async def test_a_start_index_skips_every_endpoint_before_it() -> None:
    """Starting at 1 means the first endpoint is not asked at all, not asked and ignored.

    Reading twenty addresses against an instance that just refused the first is how a soft
    throttle becomes a ban, and the count is the only thing that can tell "skipped" from
    "asked and discarded" -- the returned body is identical either way.
    """
    recorder = Recorder(500, BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        body, index = await endpoints.read(PATH, ADDRESS_BALANCE, 1)

    assert body == BODY
    assert index == 1
    assert recorder.counts == {FIRST_HOST: 0, SECOND_HOST: 1}


async def test_the_request_carries_the_label_it_was_given_and_no_query() -> None:
    """The label is what `request_target` renders into a log, and the path never is.

    A loop that dropped the extension would produce a correct sync whose every log line
    said `<unlabelled>`, which is impossible to find in production and impossible to notice
    in a test that only looks at balances.
    """
    recorder = Recorder(BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    request = recorder.requests[0]
    assert request.method == "GET"
    assert request.url == f"{FIRST_URL}{PATH}"
    assert request.url.query == b""
    assert request.extensions.get(ENDPOINT_EXTENSION) == ADDRESS_BALANCE


@pytest.mark.parametrize(
    ("status", "why"),
    [
        pytest.param(403, "a ban spelled 403, which is what a public index actually sends"),
        pytest.param(429, "the documented throttle"),
        pytest.param(451, "a legal block, which is per-jurisdiction and so per-endpoint"),
        pytest.param(404, "a route that moved, or a base URL missing its suffix"),
        pytest.param(401, "an auth proxy in front of one endpoint and not the other"),
        pytest.param(400, "a refusal of the request itself"),
        pytest.param(418, "a status nobody planned for"),
        pytest.param(301, "a redirect, which the shared client does not follow"),
        pytest.param(503, "the ordinary outage"),
    ],
)
async def test_every_failure_to_answer_moves_to_the_next_endpoint(status: int, why: str) -> None:
    """**Every** non-200 moves on, which is the correction review made to #7.

    A refusal scoped to an *endpoint* -- a ban spelled 403, an auth proxy, a base URL
    missing its `/api` -- is exactly the case a second endpoint exists for, and the
    original rule made that the one case where the second endpoint was never asked.

    Only a 200 whose body will not parse stops the call, and that happens in the provider
    rather than here: this method's job ends at "an endpoint answered".
    """
    del why  # In the parameter id.
    recorder = Recorder(status, BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        body, index = await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    assert body == BODY
    assert index == 1
    assert recorder.hosts_in_order == [FIRST_HOST, SECOND_HOST]


async def test_a_transport_error_moves_to_the_next_endpoint_too() -> None:
    """A connection that never opened is a reason to try the other endpoint.

    `httpx.ConnectError` propagates out of the transport as itself, so this loop is the
    layer that has to catch it -- and a loop that only handled failing *responses* would
    let a raw `httpx` exception escape into a service that `import-linter` forbids from
    importing `httpx` at all.
    """
    recorder = Recorder(httpx.ConnectError("connection refused"), BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        body, index = await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    assert body == BODY
    assert index == 1


# --------------------------------------------------------------------------------------
# Exhaustion: which error, and what it is chained to
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected", "why"),
    [
        pytest.param(429, ProviderRateLimitedError, "a throttle names its own remedy"),
        pytest.param(403, ProviderResponseError, "a ban is not an outage and not a throttle"),
        pytest.param(401, ProviderResponseError, "a credential, which waiting will not fix"),
        pytest.param(404, ProviderResponseError, "a route that is gone"),
        pytest.param(301, ProviderResponseError, "a redirect nobody followed"),
        pytest.param(503, ProviderUnavailableError, "an outage, which waiting does fix"),
        pytest.param(500, ProviderUnavailableError, "the other outage"),
    ],
)
async def test_the_exhausted_error_is_classified_by_the_last_failure(
    status: int, expected: type[Exception], why: str
) -> None:
    """Three outcomes, because the three have three different remedies.

    A 429 means our own interval is too short and the fix is configuration; a 5xx or a
    transport error means wait; anything else means a person has to look, and retrying it
    produces the same answer. `type(...) is expected` rather than `isinstance`, because
    `ProviderRateLimitedError` is a subclass of `ProviderUnavailableError` and a caller
    branching on the remedy reads the exact type.
    """
    del why  # In the parameter id.
    recorder = Recorder(status, status)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        with pytest.raises(expected) as caught:
            await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    assert type(caught.value) is expected
    assert recorder.counts == {FIRST_HOST: 1, SECOND_HOST: 1}


async def test_the_last_failure_decides_and_not_the_worst_one() -> None:
    """A throttled first endpoint and a broken second is unavailability, not throttling.

    Driven in both orders, because a rule written as `if any(...)` passes one of them and
    a rule written as "the last" passes both. Telling an operator to lengthen an interval
    while the vendor that actually failed is returning 500s sends them after the wrong
    thing entirely.
    """
    throttle_then_break = Recorder(429, 500)
    client = client_over(throttle_then_break)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)
    async with client:
        with pytest.raises(ProviderUnavailableError) as unavailable:
            await endpoints.read(PATH, ADDRESS_BALANCE, 0)
    assert not isinstance(unavailable.value, ProviderRateLimitedError)

    break_then_throttle = Recorder(500, 429)
    client = client_over(break_then_throttle)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)
    async with client:
        with pytest.raises(ProviderRateLimitedError):
            await endpoints.read(PATH, ADDRESS_BALANCE, 0)


async def test_the_cause_is_never_an_earlier_endpoints_exception() -> None:
    """#7's fourth lesson, asserted where the loop now lives.

    A first endpoint that refuses the connection and a second that answers 429 must raise
    `ProviderRateLimitedError` chained to nothing -- not to the first endpoint's
    `ConnectError`. The type and the cause would otherwise tell an operator two different
    stories about one sync: "you are being throttled", caused by "the connection was
    refused", which sends them to check a host that was never the problem.

    `is None` rather than "not a `ConnectError`", because the house rule is to pin the
    settled state rather than the first state that matches: a status-based failure has no
    exception to chain, in every ordering, and a weaker assertion would go on passing if
    the cause later became some other stale exception.
    """
    recorder = Recorder(httpx.ConnectError("connection refused"), 429)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        with pytest.raises(ProviderRateLimitedError) as caught:
            await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    assert caught.value.__cause__ is None, (
        "the error is classified from the second endpoint's 429 and chained to the first "
        f"endpoint's {type(caught.value.__cause__).__name__}, so the type and the "
        "traceback disagree about which host failed"
    )


async def test_the_cause_survives_when_the_last_failure_really_was_a_transport_error() -> None:
    """The control. A loop that simply stopped attaching a cause would pass the test above.

    Asserted by identity rather than by type, because two `ConnectError`s would satisfy an
    `isinstance` check whichever one was attached -- and which one is attached is the
    entire subject.
    """
    last = httpx.ConnectTimeout("timed out")
    recorder = Recorder(httpx.ConnectError("connection refused"), last)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    assert caught.value.__cause__ is last


async def test_nothing_configured_is_unavailable_and_chained_to_nothing() -> None:
    """Both URLs blank: nothing was asked, so nothing refused.

    Unavailable rather than a refusal, and it takes the ordinary exhaustion path rather
    than a branch of its own -- which is what keeps the no-endpoint case from being the one
    arm nobody exercised. The cause is `None` because there is no exception behind it, and
    a stale one here would be the same defect the test above is about.
    """
    recorder = Recorder(200)
    client = client_over(recorder)
    endpoints = endpoint_set(client, "", "")

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    assert not isinstance(caught.value, ProviderRateLimitedError)
    assert caught.value.__cause__ is None
    assert recorder.requests == []


async def test_no_exhaustion_message_names_a_url_a_host_or_a_body() -> None:
    """`ProviderError` messages reach a log and an operations view.

    A URL names the deployment, a host names the vendor's topology, and a body from a
    public index can echo the request -- which is to say the address. The vendor label the
    set was built with is the whole of what an operator is told, and it is the part they
    can act on.
    """
    recorder = Recorder(500, 500)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await endpoints.read(PATH, ADDRESS_BALANCE, 0)

    message = str(caught.value)
    assert VENDOR in message, "an operator cannot act on a failure that names no vendor"
    for forbidden in (FIRST_HOST, SECOND_HOST, FIRST_URL, SECOND_URL, PATH, BODY, "://"):
        assert forbidden not in message
    assert all(
        forbidden not in str(argument)
        for argument in caught.value.args
        for forbidden in (FIRST_HOST, SECOND_HOST, BODY)
    )


# --------------------------------------------------------------------------------------
# A read expressed as a `POST`, which is what the second chain needed
# --------------------------------------------------------------------------------------


async def test_a_post_read_sends_the_payload_as_a_json_body() -> None:
    """Kaspa's batch balance call is `POST /addresses/balances` with a JSON body.

    The body is asserted as bytes off the recorded request rather than as "a body was
    sent", because the failure this guards against -- a stream consumed on the first
    attempt and replayed empty -- produces a request that still has a body attribute and no
    content in it.
    """
    recorder = Recorder(BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)
    payload: dict[str, Sequence[str]] = {"addresses": ["one", "two"]}

    async with client:
        body, index = await endpoints.post(PATH, ADDRESS_BALANCES, 0, json=payload, idempotent=True)

    assert body == BODY
    assert index == 0
    request = recorder.requests[0]
    assert request.method == "POST"
    assert json.loads(request.content) == payload


async def test_a_post_read_declares_itself_idempotent_at_the_call_site() -> None:
    """The opt-in is per request, deny-by-default, and visible where it is made.

    #6 proposed widening `RetryPolicy.retry_methods` to include `POST`. That is wrong now
    that the consequence is visible: the policy lives on the transport, the transport is
    process-wide by construction, and widening it would make **every** future `POST`
    retryable -- including an exchange request that places an order, where a retry after a
    transport error can double a trade.
    """
    recorder = Recorder(BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        await endpoints.post(PATH, ADDRESS_BALANCES, 0, json={"addresses": []}, idempotent=True)

    request = recorder.requests[0]
    assert request.extensions.get(IDEMPOTENT_EXTENSION) is True
    assert request.extensions.get(ENDPOINT_EXTENSION) == ADDRESS_BALANCES


async def test_a_post_that_is_not_idempotent_carries_no_extension_at_all() -> None:
    """The key is **absent**, not present and `False`, and the difference is the contract.

    `RetryingTransport` tests `extensions.get(IDEMPOTENT_EXTENSION) is True`, so an absent
    key and a `False` behave identically today -- which is exactly why the spelling has to
    be pinned rather than left to whichever the implementation happened to pick. An absent
    key is the honest statement: this request never opted in. A present `False` invites the
    next reader to treat the key as a tri-state and write `if IDEMPOTENT_EXTENSION in
    extensions`, at which point deny-by-default is gone and nothing in the transport
    changed.

    **This is the arm the `EndpointSet` seam exists for.** The helper used to set the
    extension to `True` for every caller, which held deny-by-default at the transport and
    undid it one layer up -- and `EndpointSet` is precisely what an exchange provider with
    a primary and a fallback reaches for next. At that moment the failover loop would
    double-submit an order after a transport error.
    """
    recorder = Recorder(BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        await endpoints.post(PATH, ADDRESS_BALANCES, 0, json={"addresses": []}, idempotent=False)

    request = recorder.requests[0]
    assert IDEMPOTENT_EXTENSION not in request.extensions
    assert request.extensions.get(ENDPOINT_EXTENSION) == ADDRESS_BALANCES


async def test_a_post_that_is_not_idempotent_is_not_retried() -> None:
    """The behaviour the spelling above buys, driven rather than inferred.

    A 503 that a `GET` would have retried leaves a non-idempotent `POST` after a single
    attempt. The count is the assertion, because the exception a caller sees is the same
    either way -- and the difference between one attempt and three, for a request that
    places an order, is the difference between one trade and three.
    """
    recorder = Recorder(503, 503)
    client = client_over(recorder, max_attempts=3)
    endpoints = endpoint_set(client, FIRST_URL, "")

    async with client:
        with pytest.raises(ProviderUnavailableError):
            await endpoints.post(
                PATH, ADDRESS_BALANCES, 0, json={"addresses": []}, idempotent=False
            )

    assert recorder.counts == {FIRST_HOST: 1, SECOND_HOST: 0}


async def test_an_idempotent_post_is_retried_which_is_the_control() -> None:
    """The control on the test above. A seam that never retried anything would pass it.

    Same script, same endpoint, one word different at the call site: three attempts rather
    than one. That word is the whole mechanism, and this pair is the only place in the
    suite where both of its values are driven against the same transport.
    """
    recorder = Recorder(503, 503)
    client = client_over(recorder, max_attempts=3)
    endpoints = endpoint_set(client, FIRST_URL, "")

    async with client:
        with pytest.raises(ProviderUnavailableError):
            await endpoints.post(PATH, ADDRESS_BALANCES, 0, json={"addresses": []}, idempotent=True)

    assert recorder.counts == {FIRST_HOST: 3, SECOND_HOST: 0}


async def test_a_post_read_fails_over_exactly_as_a_get_does() -> None:
    """One failover rule, not one per verb.

    Two rules is how the `GET` path and the `POST` path end up disagreeing about what a
    403 means, which would present as one chain moving on from a ban and the other
    stopping -- with nothing in either file saying they were supposed to match.
    """
    recorder = Recorder(403, BODY)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        body, index = await endpoints.post(
            PATH, ADDRESS_BALANCES, 0, json={"addresses": []}, idempotent=True
        )

    assert body == BODY
    assert index == 1
    assert recorder.hosts_in_order == [FIRST_HOST, SECOND_HOST]


async def test_a_post_that_exhausts_every_endpoint_is_classified_the_same_way() -> None:
    """The classification is shared too, so a batch 429 still names its own remedy."""
    recorder = Recorder(429, 429)
    client = client_over(recorder)
    endpoints = endpoint_set(client, FIRST_URL, SECOND_URL)

    async with client:
        with pytest.raises(ProviderRateLimitedError):
            await endpoints.post(PATH, ADDRESS_BALANCES, 0, json={"addresses": []}, idempotent=True)

    assert recorder.counts == {FIRST_HOST: 1, SECOND_HOST: 1}
