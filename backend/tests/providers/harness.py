"""The one place that knows how the HTTP client under test is wired together.

Every test that drives a request goes through `retrying_client`, so the knowledge of which
keyword takes the inner transport, which takes the sleep and which takes the clock lives
here rather than smeared across five modules. When that wiring changes, one function
changes.

**Nothing here sleeps, and nothing here reads a clock.** The sleep, the clock and the
jitter are all injected, so no assertion in this suite is a measurement of how fast the
machine running it happened to be. A test whose verdict depends on host speed is a test
that fails on a loaded CI runner and passes on a laptop, which is worse than no test: it
teaches the team to re-run the build instead of reading it.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Final

import anyio.lowlevel
import httpx

from portfolio.providers.http import (
    HostRateLimiter,
    RetryPolicy,
    build_http_client,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

#: Named once so no test multiplies or divides by a bare literal. Tests are not under the
#: rule 2 float ban -- `providers/` is -- but exact integer milliseconds are what the
#: assertions compare, so the conversion appears in exactly one place all the same.
MILLISECONDS_PER_SECOND: Final = 1000

#: A fictional host. `example` is reserved by RFC 2606 and resolves nowhere, so a test
#: that somehow escaped its mock transport fails to connect instead of reaching somebody's
#: real API. Rule 3 forbids a real hostname in the repository regardless.
TEST_HOST: Final = "api.example"
TEST_ORIGIN: Final = f"https://{TEST_HOST}"
OTHER_HOST: Final = "other.example"
OTHER_ORIGIN: Final = f"https://{OTHER_HOST}"

#: The label a provider puts in `request.extensions`, which is the only thing about a
#: request's target the transport is allowed to log.
ENDPOINT_LABEL: Final = "address_balance"

#: An outcome the scripted transport can be told to produce: a status code, a status with
#: headers, or an exception to raise instead of answering.
type Outcome = int | tuple[int, Mapping[str, str]] | BaseException


class RecordingSleep:
    """An injected sleep that records what it was asked for and yields, without waiting.

    Milliseconds, as integers, matching `providers.http.sleep_ms` -- which is what the
    transport and the limiter both default to, and which takes milliseconds because
    `providers/` cannot hold a float at all.

    **It yields a checkpoint, and that is load-bearing.** The first version simply
    recorded and returned. An `async def` with no `await` in it never suspends, so under
    `anyio.create_task_group` every task ran to completion before the next one started --
    and the concurrency test that was supposed to catch a limiter claiming its slot
    *after* sleeping instead of before passed against exactly that bug. A checkpoint is a
    suspension point and not a wall-clock wait, so the fix costs no time and restores
    the interleaving a real sleep would produce.
    """

    def __init__(self) -> None:
        self.slept_ms: list[int] = []

    async def __call__(self, milliseconds: int) -> None:
        self.slept_ms.append(milliseconds)
        await anyio.lowlevel.checkpoint()


class FakeClock:
    """A monotonic clock a test advances by hand.

    Integer milliseconds, and it only moves when a test moves it. The rate limiter's whole
    contract is expressed against this: with a real clock the only available assertion
    would be "it took about a quarter of a second", which is a measurement of the host and
    not of the limiter.
    """

    def __init__(self, now_ms: int = 0) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


def jitter_returning(
    fraction_of_bound: Callable[[int], int],
) -> tuple[Callable[[int], int], list[int]]:
    """A deterministic stand-in for `secrets.randbelow`, plus the bounds it was handed.

    The bounds are the interesting half. Full jitter draws from `[0, bound)`, so the
    sequence of bounds *is* the backoff schedule, and asserting on it is how a test tells
    full jitter from "the exponential plus a little random" -- which the spec rejects
    because the additive form leaves every client's retries clustered exactly where the
    exponential put them.
    """
    bounds: list[int] = []

    def jitter(bound: int) -> int:
        bounds.append(bound)
        return fraction_of_bound(bound)

    return jitter, bounds


def zero_jitter() -> tuple[Callable[[int], int], list[int]]:
    """Always draw zero. The discriminator between full jitter and the additive form.

    Under full jitter a zero draw is a zero sleep. Under `base + random` it is a sleep of
    `base`, and no amount of staring at the other assertions distinguishes the two.
    """
    return jitter_returning(lambda _bound: 0)


def top_of_range_jitter() -> tuple[Callable[[int], int], list[int]]:
    """Draw the largest value `randbelow(bound)` can return, which is `bound - 1`."""
    return jitter_returning(lambda bound: max(bound - 1, 0))


def scripted_handler(
    *outcomes: Outcome,
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    """A handler that answers a scripted sequence and records every request it saw.

    An entry is a status code, a `(status, headers)` pair, or an exception to raise. The
    last entry repeats, so `scripted_handler(500)` is "always fails" and
    `scripted_handler(500, 200)` is "fails once, then works".

    The recorded requests are the point: "was this retried" is a statement about how many
    times the *inner* transport was entered, and counting responses at the caller cannot
    see it.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        outcome = outcomes[min(len(requests) - 1, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, tuple):
            status, headers = outcome
            return httpx.Response(status, headers=dict(headers), json={"ok": True})
        return httpx.Response(outcome, json={"ok": True})

    return handler, requests


def scripted_transport(*outcomes: Outcome) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """`scripted_handler`, wrapped in the transport the client will wrap in turn."""
    handler, requests = scripted_handler(*outcomes)
    return httpx.MockTransport(handler), requests


def fast_policy(
    *,
    max_attempts: int = 3,
    base_backoff_ms: int = 100,
    max_backoff_ms: int = 1000,
    retry_methods: frozenset[str] = frozenset({"GET", "HEAD"}),
) -> RetryPolicy:
    """A policy with numbers small enough to write down and assert exactly.

    The values are deliberately not the production defaults. An assertion written against
    `DEFAULT_RETRY_POLICY` would pass for any production value at all and would have to be
    edited every time an operator's measurement moved one, which is how an assertion turns
    into a copy of the code it was supposed to check.
    """
    return RetryPolicy(
        max_attempts=max_attempts,
        base_backoff_ms=base_backoff_ms,
        max_backoff_ms=max_backoff_ms,
        retry_methods=retry_methods,
    )


def open_limiter(sleep: RecordingSleep) -> HostRateLimiter:
    """A limiter that never makes anyone wait, for the tests that are about retrying.

    A zero interval rather than no limiter at all -- `RetryingTransport` has no unlimited
    code path by design, so this is the supported way to say "do not pace me", and the
    limiter stays in the path being exercised.
    """
    return HostRateLimiter(min_interval_ms=0, clock=FakeClock(), sleep=sleep)


def retrying_client(
    inner: httpx.MockTransport,
    *,
    policy: RetryPolicy | None = None,
    sleep: RecordingSleep | None = None,
    jitter: Callable[[int], int] | None = None,
    limiter: HostRateLimiter | None = None,
    now: Callable[[], datetime] = utc_now,
) -> httpx.AsyncClient:
    """The client under test: the real factory, with every duration injected.

    `build_http_client` rather than assembling a `RetryingTransport` by hand, because the
    factory is what production calls and the timeouts it sets are part of criterion 3. A
    test that built the transport itself would verify a wiring nothing uses.
    """
    recording_sleep = sleep if sleep is not None else RecordingSleep()
    chosen_jitter = jitter if jitter is not None else zero_jitter()[0]
    return build_http_client(
        transport=inner,
        policy=policy if policy is not None else fast_policy(),
        limiter=limiter if limiter is not None else open_limiter(recording_sleep),
        jitter=chosen_jitter,
        sleep=recording_sleep,
        now=now,
    )


def client_with_the_shipped_defaults(
    inner: httpx.MockTransport,
    *,
    sleep: RecordingSleep,
    jitter: Callable[[int], int] = secrets.randbelow,
) -> httpx.AsyncClient:
    """`build_http_client` with **no `policy` argument** -- which is what production gets.

    Every other builder in this module injects `fast_policy()`, so that a test can assert
    exact millisecond values without waiting on a wall clock. That discipline is right and
    it opened a hole: a suite in which every retry test supplies its own policy never
    observes `DEFAULT_RETRY_POLICY` at all. The shipped default could be
    `max_attempts=1` -- the whole retry subsystem dead in the deployed application -- with
    every retry test still green. A mutation sweep found exactly that.

    So this builder passes `policy` nowhere. `sleep` and `limiter` are still injected,
    because the point is to let the default **decide**, not to let it **wait**; `now` is
    left alone; and `jitter` defaults to the same `secrets.randbelow` the factory would
    have used, so a test that does not care about the draw is not quietly substituting
    something else for it. `test_the_shipped_retry_policy_is_pinned_field_by_field`
    asserts that identity rather than trusting this docstring.
    """
    return build_http_client(
        transport=inner,
        limiter=HostRateLimiter(min_interval_ms=0, clock=FakeClock(), sleep=sleep),
        jitter=jitter,
        sleep=sleep,
    )


async def perform(
    client: httpx.AsyncClient,
    method: str = "GET",
    url: str = f"{TEST_ORIGIN}/anything",
    *,
    endpoint: str | None = ENDPOINT_LABEL,
) -> httpx.Response | BaseException:
    """Make one request and hand back whatever happened, response or exception.

    Returning the exception instead of letting it propagate is what lets a test assert on
    the *number of attempts* without also having to agree, in the same assertion, on what
    an exhausted retry produces. Those are two different questions and they get two
    different tests.
    """
    extensions = {"endpoint": endpoint} if endpoint is not None else {}
    try:
        return await client.request(method, url, extensions=extensions)
    except BaseException as error:
        return error
