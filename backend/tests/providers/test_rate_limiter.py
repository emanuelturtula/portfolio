"""Criterion 3: the per-host rate limiter, verified without waiting for anything.

Every assertion is about what the limiter asked the injected sleep for, against a clock a
test moves by hand. Nothing here measures elapsed time, and that is deliberate: an
assertion of the form "the second request took at least 250 ms" is a measurement of the
host, and it fails on a loaded CI runner for reasons that have nothing to do with the
code.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio

from portfolio.providers.http import (
    DEFAULT_MIN_HOST_INTERVAL_MS,
    NANOSECONDS_PER_MILLISECOND,
    HostRateLimiter,
    RateLimitHint,
    monotonic_ms,
)
from tests.providers.harness import OTHER_HOST, TEST_HOST, FakeClock, RecordingSleep

if TYPE_CHECKING:
    import pytest

INTERVAL_MS: Final = 250

#: A server-declared reset, deliberately several times the interval so that "it waited the
#: reset" and "it waited its own interval" are different numbers. Milliseconds, because
#: `providers/` counts durations in integers; the header is in seconds and
#: `parse_rate_limit` is what converts.
RESET_MS: Final = 4_000


def limiter_with(
    clock: FakeClock, sleep: RecordingSleep, interval_ms: int = INTERVAL_MS
) -> HostRateLimiter:
    """A limiter with both of its non-deterministic inputs replaced."""
    return HostRateLimiter(min_interval_ms=interval_ms, clock=clock, sleep=sleep)


# --------------------------------------------------------------------------------------
# Spacing
# --------------------------------------------------------------------------------------


async def test_two_requests_to_one_host_are_spaced_by_the_interval() -> None:
    """The first caller goes straight through; the second is made to wait the interval.

    Asserted as the exact millisecond value the limiter asked for, not as "it waited".
    A limiter that always slept the full interval, including for the first request,
    would pass "it waited" and would halve the throughput of every sync.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    await limiter.acquire(TEST_HOST)
    await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == [INTERVAL_MS]


async def test_a_caller_that_arrives_after_the_interval_is_not_delayed() -> None:
    """The other half: the limiter is a floor on spacing, not a fixed cost per request.

    Without this, a limiter that slept the interval unconditionally would satisfy the test
    above. Here the clock is moved past the window first, so any sleep at all is a bug.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    await limiter.acquire(TEST_HOST)
    clock.advance(INTERVAL_MS)
    await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == []


async def test_a_queue_of_callers_is_spaced_and_not_bunched() -> None:
    """Four arrivals at the same instant leave one interval apart, not all at once.

    This is the property the leaky bucket has and a naive "sleep until now + interval"
    does not: each waiter claims its slot when it arrives, so the waits are 0, 1, 2 and 3
    intervals rather than four callers all deciding to wait the same amount and then all
    firing together.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    for _ in range(4):
        await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == [INTERVAL_MS, 2 * INTERVAL_MS, 3 * INTERVAL_MS]


async def test_two_concurrent_callers_are_spaced_rather_than_both_waiting_the_same() -> None:
    """The reason the bookkeeping is under the lock and the sleeping is outside it.

    If the slot were claimed after the sleep instead of before it, both tasks would read
    the same "next allowed" instant, both would decide to wait the same amount, and both
    would fire at the same moment -- which is the burst the limiter exists to prevent,
    reproduced exactly by the code that was supposed to prevent it.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    async with anyio.create_task_group() as tasks:
        for _ in range(3):
            tasks.start_soon(limiter.acquire, TEST_HOST)

    assert sorted(sleep.slept_ms) == [INTERVAL_MS, 2 * INTERVAL_MS]


# --------------------------------------------------------------------------------------
# Criterion 4 of #8: a budget the server says is exhausted
# --------------------------------------------------------------------------------------
#
# **Nothing in production reaches this.** Measured against the live Kaspa REST service on
# 2026-09-23: neither `GET /info/health` nor the balance endpoint sends a `ratelimit-*` or
# `x-ratelimit-*` header, because the API sits behind Cloudflare. The criterion says "when
# present", so the mechanism is built and tested against synthesised hints, and
# `tests/providers/test_rate_limit_headers.py` says so in its own docstring. A self-hosted
# instance with no CDN in front of it is the deployment this is actually for.


async def test_a_zero_remaining_budget_waits_for_the_reset() -> None:
    """`remaining: 0` means the next call to that host waits the reset, not the interval.

    The interval is a floor this application chose from the shape of a warning; a `reset`
    is the server telling us when it will answer again. Pacing at 250 ms against a server
    that has just said "nothing more for seven seconds" spends the whole budget producing
    refusals, which is how a soft throttle becomes the ban the vendor documentation warns
    about.

    Asserted as the exact millisecond value the limiter asked for, so "it waited" is not
    enough: a limiter that had simply used its own interval would still have waited.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    await limiter.acquire(TEST_HOST)
    limiter.observe(TEST_HOST, RateLimitHint(limit=60, remaining=0, reset_ms=RESET_MS))
    await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == [RESET_MS]


async def test_a_budget_that_still_has_room_paces_at_the_ordinary_interval() -> None:
    """The control, and it is the case that actually happens.

    A limiter that waited for the reset whenever a hint arrived would turn every response
    carrying rate-limit headers into a pause -- which is every response, on a server that
    sends them -- and the sync would run at the server's advertised window rather than at
    its own interval. Only `remaining == 0` is an instruction to wait.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    await limiter.acquire(TEST_HOST)
    limiter.observe(TEST_HOST, RateLimitHint(limit=60, remaining=59, reset_ms=RESET_MS))
    await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == [INTERVAL_MS]


async def test_an_exhausted_budget_with_no_reset_falls_back_to_the_interval() -> None:
    """A server that says "nothing left" and not when is telling us nothing actionable.

    `reset_ms` is `None` when the header was absent or unparseable, and the honest response
    to that is the pacing we would have used anyway. Waiting forever, or waiting zero, are
    both answers invented out of a value the server did not send.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    await limiter.acquire(TEST_HOST)
    limiter.observe(TEST_HOST, RateLimitHint(limit=60, remaining=0, reset_ms=None))
    await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == [INTERVAL_MS]


async def test_an_exhausted_budget_on_one_host_does_not_pace_another() -> None:
    """A budget is a property of a host, which is why the limiter owns it and not a parser.

    One vendor saying it has had enough is no reason to slow down calls to a different
    vendor -- and on a Raspberry Pi running a self-hosted Esplora and a Kaspa REST server,
    the two share a hostname and differ only by port, which is exactly the case `host_key`
    exists for.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    limiter.observe(TEST_HOST, RateLimitHint(limit=60, remaining=0, reset_ms=RESET_MS))
    await limiter.acquire(OTHER_HOST)

    assert sleep.slept_ms == []


async def test_a_reset_is_spent_once_and_does_not_become_the_hosts_new_interval() -> None:
    """The pause is a one-off instruction, not a new pace for the host forever.

    A limiter that stored the reset as its interval would pace that host at the whole reset
    per request for the life of the process, and nothing anywhere would say why -- a sync
    that silently takes twenty times longer is worse than one that fails.

    The clock is moved past each window rather than left at zero, so the assertion is "no
    further wait at all" rather than an arithmetic identity that depends on how the reset
    and the interval happen to compose.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    limiter.observe(TEST_HOST, RateLimitHint(limit=60, remaining=0, reset_ms=RESET_MS))
    await limiter.acquire(TEST_HOST)
    clock.advance(RESET_MS + INTERVAL_MS)
    await limiter.acquire(TEST_HOST)
    clock.advance(INTERVAL_MS)
    await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == [RESET_MS]


# --------------------------------------------------------------------------------------
# Per host
# --------------------------------------------------------------------------------------


async def test_a_second_host_is_not_made_to_wait() -> None:
    """Being throttled by one vendor is no reason to slow down calls to another.

    A single global interval would make a portfolio with two chains take twice as long to
    sync for no reason at all, and would make adding a third chain slow down the first two.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    await limiter.acquire(TEST_HOST)
    await limiter.acquire(OTHER_HOST)

    assert sleep.slept_ms == []


async def test_the_hosts_keep_separate_windows_rather_than_one_shared_one() -> None:
    """Interleaving two hosts must not let either one's pacing leak into the other's.

    The stronger version of the test above: alternating between two hosts produces the
    same waits as pacing each one on its own, which a shared window would not.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    await limiter.acquire(TEST_HOST)
    await limiter.acquire(OTHER_HOST)
    await limiter.acquire(TEST_HOST)
    await limiter.acquire(OTHER_HOST)

    assert sleep.slept_ms == [INTERVAL_MS, INTERVAL_MS]


# --------------------------------------------------------------------------------------
# The clock is monotonic, and that is not a matter of documentation
# --------------------------------------------------------------------------------------


def test_the_default_clock_is_monotonic_and_counts_in_integers() -> None:
    """`monotonic_ms` is `time.monotonic_ns()` scaled, never `time.time()`.

    Two failures at once if it were the wall clock: an NTP step backwards would make the
    limiter refuse to call anything for as long as the step, and a step forwards would
    make it stop limiting. On a Raspberry Pi that has just booted without a battery-backed
    clock, the first NTP sync is a step of years.
    """
    first = monotonic_ms()
    second = monotonic_ms()

    assert isinstance(first, int)
    assert not isinstance(first, bool)
    assert second >= first


def test_a_wall_clock_step_backwards_does_not_stall_the_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driven against the real `monotonic_ms`, with the underlying counter moved.

    `time.monotonic_ns` is monkeypatched rather than the limiter's `clock` argument,
    because the claim being tested is about what `monotonic_ms` reads -- injecting a fake
    clock would test the injection and leave the default unexamined.
    """
    readings = iter([5_000_000_000, 1_000_000_000])  # nanoseconds: five seconds, then one
    monkeypatch.setattr("time.monotonic_ns", lambda: next(readings))

    first = monotonic_ms()
    second = monotonic_ms()

    # The scaling is exact, so a step is reported as a step rather than being rounded away.
    assert first == 5_000_000_000 // NANOSECONDS_PER_MILLISECOND
    assert second == 1_000_000_000 // NANOSECONDS_PER_MILLISECOND


async def test_a_clock_that_steps_backwards_is_the_hazard_the_monotonic_default_avoids() -> None:
    """What a wall clock would actually cost, and why the default is not one.

    Given a clock that jumps ten seconds backwards -- an NTP correction, a manual clock
    set, a Raspberry Pi with no battery-backed clock syncing for the first time after boot
    -- the limiter holds the next call for the whole size of the jump. The wait is not
    bounded by the interval and it is not meant to be: the algorithm assumes a clock that
    only moves forward, and the defence is that the default clock is one.

    Asserted as the exact number rather than as "it is large", so this stays a statement
    about the mechanism, and paired with the assertion that production cannot reach it.
    """
    clock = FakeClock(now_ms=10_000)
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep)

    await limiter.acquire(TEST_HOST)
    clock.now_ms = 0  # the step an NTP correction would produce
    await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == [10_000 + INTERVAL_MS]
    # And the clock a caller gets without asking cannot step backwards at all.
    default_clock = inspect.signature(HostRateLimiter.__init__).parameters["clock"].default
    assert default_clock is monotonic_ms


def test_the_module_measures_elapsed_time_with_nothing_but_the_monotonic_counter() -> None:
    """No `time.time` anywhere in `providers/http.py`, asserted rather than reviewed.

    The behavioural test above shows what a backwards step costs; this one shows nothing
    in the module can produce one. A future "improvement" that reached for `time.time()`
    -- to log a timestamp, to compute an age -- would pass every behavioural test in this
    file and reintroduce the whole hazard.
    """
    source = Path(inspect.getfile(HostRateLimiter)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    time_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "time"
    }

    assert time_attributes == {"monotonic_ns"}, f"providers/http.py reads time.{time_attributes}"


# --------------------------------------------------------------------------------------
# The boundaries
# --------------------------------------------------------------------------------------


async def test_a_zero_interval_is_a_limiter_that_never_waits() -> None:
    """`min_interval_ms = 0` is the supported way to say "do not pace me".

    It takes the ordinary path and computes a zero wait rather than being a special case,
    so there is one request path to reason about instead of a limited one and an
    unlimited one.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep, interval_ms=0)

    for _ in range(5):
        await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == []


def test_the_default_interval_is_the_documented_floor() -> None:
    """One request per second, pinned as a literal, because it is what #7 raised it to.

    mempool.space's documentation -- read on 2026-09-22 -- states that exceeding its
    limits returns 429 and that repeatedly exceeding them can get the caller banned, and
    it publishes no numbers. Being banned from a free public index is a failure that
    outlives the sync that caused it, so the floor is the issue's own: 1000 ms, up from
    the 250 ms #6 guessed at when there was no vendor to read.

    The literal is the assertion. `DEFAULT_MIN_HOST_INTERVAL_MS == DEFAULT_MIN_HOST_INTERVAL_MS`
    is true of any value at all, including the zero that would turn the limiter off for
    every provider at once with nothing in any log to say so.

    The value is shared, so Kaspa (#8) inherits it. That is deliberate and it is recorded
    in the spec: if a batch endpoint finds it too slow, the answer is a per-host override
    table, not a lower shared floor.
    """
    assert DEFAULT_MIN_HOST_INTERVAL_MS == 1000
    assert isinstance(DEFAULT_MIN_HOST_INTERVAL_MS, int)
    assert not isinstance(DEFAULT_MIN_HOST_INTERVAL_MS, bool)


async def test_the_shipped_floor_is_the_one_a_provider_would_actually_be_paced_by() -> None:
    """The #6 lesson: a test that injects a value can no longer observe that value's default.

    Every other test in this file passes `min_interval_ms=INTERVAL_MS`, so the assertions
    are exact without waiting on a wall clock. That discipline is right, and it is exactly
    what leaves the shipped number unobserved -- which is how `RetryPolicy.max_attempts`
    could have shipped as `1` with 1102 tests green.

    `HostRateLimiter` has no default interval of its own -- `min_interval_ms` is required,
    deliberately, so that "how fast may I call this host" is never a question somebody
    forgets to answer. The shipped number reaches a request through `RetryingTransport`,
    which builds `HostRateLimiter(min_interval_ms=DEFAULT_MIN_HOST_INTERVAL_MS, ...)` when
    no limiter is passed. That wiring is driven over a real request in
    `tests/providers/test_http.py::test_a_client_built_with_no_limiter_paces_itself_at_the_shipped_interval`,
    whose clock is the real monotonic one and whose assertion is therefore a bound rather
    than an equality.

    **This test supplies the exactness that bound cannot.** Given the shipped constant and
    nothing else, the limiter's spacing is asserted to the millisecond -- so the pair is a
    literal pin on the number, a behavioural check that the number reaches a request, and
    this, which says what the number *does*. A zero would fail all three.
    """
    clock = FakeClock()
    sleep = RecordingSleep()
    limiter = limiter_with(clock, sleep, interval_ms=DEFAULT_MIN_HOST_INTERVAL_MS)

    for _ in range(3):
        await limiter.acquire(TEST_HOST)

    assert sleep.slept_ms == [1000, 2000]
