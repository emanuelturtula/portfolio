"""The shared HTTP client every provider makes its calls through.

Timeouts, bounded retry with full jitter, a per-host rate limiter and `Retry-After`
handling, all of them in an `httpx.AsyncBaseTransport` wrapper rather than in a helper
function. **A helper has to be remembered; a transport cannot be bypassed.** Every request
made through the client this module builds is covered whether or not its caller knew the
rule existed -- which is the same argument rule 8 makes about authentication middleware.

**This module does not translate failures into `ProviderError`s, and that boundary is
drawn one layer up on purpose.** `RetryingTransport` is an `httpx.AsyncBaseTransport`, and
a transport that raised a foreign exception would not be one: `httpx` annotates the
exceptions it recognises with the request that failed, and anything else loses that. So a
transport error propagates as the `httpx.TransportError` it is, and a failing response is
returned as a response. The *provider* is where `httpx` stops -- it catches both and
decides which is a `ProviderUnavailableError` and which is a `ProviderResponseError`,
because that decision is about what the vendor meant, not about how the bytes moved.

## A logged URL must not be able to carry an address

Two functions, because two different questions are being answered.

`strip_query` removes the query string, the fragment and any userinfo. That is the rule
`CLAUDE.md` states, and it exists because one exchange signs its requests *in the query
string*: the signature and the key that produced it would otherwise ride along in any URL
that reached a log. It is what a future exchange provider will use.

**For a chain provider that is necessary and not sufficient, and it is worth being precise
about why.** Both target APIs put the address in the *path*:

* Esplora: `GET /address/:address`
* Kaspa REST: `GET /addresses/{address}/balance`

Stripping the query does nothing for either. An address in a log is precisely the
disclosure `SENSITIVE_KEY_FRAGMENTS` and the wallet registry's no-address rule exist to
prevent, so `request_target` -- which is what this transport actually logs -- never emits
a path at all. It emits `"{scheme}://{host}/{label}"`, where the label is a constant the
provider sets in `request.extensions["endpoint"]`, such as `"address_balance"`. A request
that sets no label logs `"<unlabelled>"`.

Deny by default, the same shape as rule 8: adding an endpoint protects it, and saying more
about one is a deliberate edit that shows up in a diff. The alternative -- scanning each
path segment for something address-shaped -- is slow, breaks on a truncated address, and
is a guess dressed up as a control.

No log line in this module carries a response body.

## Every duration is an integer number of milliseconds

`float` is banned in `providers/` and an AST test enforces it, so `base_backoff_seconds:
float = 0.25` would fail the build. Every duration here is therefore an `int` named
`*_ms`, converted to the seconds `anyio.sleep` and `httpx.Timeout` want at exactly one
place each, by dividing by a named constant.

**This is not an evasion of rule 2, and the distinction is the point.** Rule 2 exists
because a portfolio that adds a few thousand fills in binary floating point reports a total
that is wrong and never says so. A sleep duration cannot corrupt a balance. Integer
milliseconds also happens to be the better representation for a value a test compares
exactly: `assert delay_ms == 250` says what it means, and the same assertion against a
float does not.

## What is measured and what is guessed

Confirmed against the vendors' published documentation: the endpoint shapes above, that
Esplora documents no batch endpoint, and that the Kaspa REST server's batch balance call is
`POST /addresses/balances`.

**Assumed, because neither vendor documents it:** any rate limit, any `Retry-After`
behaviour, and any cap on a batch. Esplora's documentation mentions no limit and points at
self-hosting instead. That is a reason to run our own limiter -- there is no server-side
contract to lean on -- rather than a reason to skip one. The defaults below are
conservative guesses awaiting a measurement, which is why they are a policy object and a
constructor argument rather than literals buried in the request path: #7 and #8 correct
them by changing a value. `docs/providers.md` records the same split.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final

import anyio
import httpx
import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "DEFAULT_TIMEOUT",
    "ENDPOINT_EXTENSION",
    "ENDPOINT_LABEL",
    "HTTP_ERROR_FLOOR",
    "RETRYABLE_STATUSES",
    "UNLABELLED",
    "HostRateLimiter",
    "RetryPolicy",
    "RetryingTransport",
    "build_http_client",
    "host_key",
    "monotonic_ms",
    "parse_retry_after",
    "request_target",
    "sleep_ms",
    "strip_query",
    "utc_now",
]

_logger = structlog.get_logger(__name__)

# The one place a duration stops being an integer. Dividing a name by a name is not a
# float literal, and `anyio.sleep` and `httpx.Timeout` both take seconds -- so the
# conversion has to happen somewhere, and it happens here rather than at each call site.
# A duration is not money: see the module docstring for why that distinction is the whole
# justification, not a loophole.
MILLISECONDS_PER_SECOND: Final = 1000
NANOSECONDS_PER_MILLISECOND: Final = 1_000_000
MICROSECONDS_PER_MILLISECOND: Final = 1000
SECONDS_PER_DAY: Final = 86_400

ENDPOINT_EXTENSION: Final = "endpoint"
"""The `httpx` request extension a provider puts its endpoint label in.

An extension rather than a header: a header would be sent to the vendor, and what we call
an endpoint internally is nobody else's business.
"""

UNLABELLED: Final = "<unlabelled>"
"""What a request with no usable endpoint label is logged as.

The default discloses nothing. Labelling a request is an opt-in to saying more about it,
so a provider added without one is quiet rather than leaky -- and so is one whose label
does not match `ENDPOINT_LABEL`.
"""

ENDPOINT_LABEL: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
"""The only shape an endpoint label may take: lower snake case, at most 32 characters.

A constant the provider author writes down, not a value derived from the request. The
pattern is what makes `request_target`'s promise structural instead of conventional: no
string containing a slash, a dot, a colon, a percent-escape, whitespace or an upper-case
character can reach a log through it, so `extensions={"endpoint": request.url.path}` --
the helpful thing a provider author reaches for when they cannot see their request --
renders as `UNLABELLED` rather than as the owner's address.

See `request_target` for what this does not cover.
"""

# All four explicit, none left to the library: `httpx`'s default is five seconds on
# everything. `read` is the generous one because a chain index answering a batch
# legitimately takes longer than a handshake.
#
# **These are four per-operation timeouts and they do not add up to a deadline.** `read`
# bounds the wait for *each* chunk, so a server trickling one byte every 19 seconds never
# trips a 20-second read and holds the connection indefinitely -- which is the failure
# these were originally described as preventing, and they do not. A whole-request deadline
# is a real design decision with a scheduler behind it: how long one address may take,
# what a partial sync means, whether a slow chain blocks a fast one. It belongs with #10,
# which has the caller, and a `fail_after` dropped in here would be a number invented
# without one. Recorded rather than fixed, deliberately.
#
# A test pins `READ_TIMEOUT_MS > CONNECT_TIMEOUT_MS` -- the *relationship* the sentence
# above claims -- rather than either number. All four are guesses awaiting a measurement,
# so a measurement must be free to move them; what it must not do is quietly invert them,
# because a read timeout at or below the connect timeout makes the generous one the
# binding one and turns every slow batch into a timeout nobody ordered.
CONNECT_TIMEOUT_MS: Final = 5_000
READ_TIMEOUT_MS: Final = 20_000
WRITE_TIMEOUT_MS: Final = 10_000
POOL_TIMEOUT_MS: Final = 5_000

DEFAULT_TIMEOUT: Final = httpx.Timeout(
    connect=CONNECT_TIMEOUT_MS / MILLISECONDS_PER_SECOND,
    read=READ_TIMEOUT_MS / MILLISECONDS_PER_SECOND,
    write=WRITE_TIMEOUT_MS / MILLISECONDS_PER_SECOND,
    pool=POOL_TIMEOUT_MS / MILLISECONDS_PER_SECOND,
)

HTTP_ERROR_FLOOR: Final = 400
"""The status at and above which a response is a failure worth logging at error."""

RETRYABLE_STATUSES: Final = frozenset({429, *range(500, 600)})
"""429 and every 5xx, and nothing else.

Spelled as a set rather than as `code == 429 or code >= 500` so that a policy can narrow
or widen it without editing the request path. **No other 4xx is here, deliberately**: a
400 retried three times is three identical wrong requests, and a 404 does not become a
200 by asking again.
"""

DEFAULT_MIN_HOST_INTERVAL_MS: Final = 250
"""A guess, not a measurement: neither vendor documents a rate limit.

Four requests a second to one host. This application reads a handful of addresses on a
schedule rather than bursting, so a conservative floor costs it seconds per sync and buys
it not being the reason a free public index starts refusing us. Replace it with a measured
number when one exists; `docs/providers.md` records that it is unverified.
"""


def utc_now() -> datetime:
    """The wall clock, timezone-aware, in one place so a test can replace it.

    Used only for `Retry-After`'s HTTP-date form, which is an absolute instant and so has
    to be compared against an absolute instant. Everything else in this module measures
    elapsed time and uses `monotonic_ms` instead.
    """
    return datetime.now(UTC)


def monotonic_ms() -> int:
    """Elapsed milliseconds on a clock that only ever moves forward.

    `time.monotonic_ns()` rather than `time.time()`, and rather than `time.monotonic()`:
    the first because an NTP step or a manual clock correction must not be able to make
    the rate limiter sleep for hours or stop limiting entirely, and the second because
    `monotonic()` returns a float and this module counts in integers.
    """
    return time.monotonic_ns() // NANOSECONDS_PER_MILLISECOND


async def sleep_ms(duration_ms: int) -> None:
    """Wait `duration_ms` milliseconds.

    Injected everywhere it is used, so that no test in this package has to assert on
    elapsed wall-clock time. A verdict that depends on how fast the host is, is a verdict
    that will eventually be wrong on somebody else's machine.
    """
    # The second and last conversion out of integer milliseconds. See the module docstring.
    await anyio.sleep(duration_ms / MILLISECONDS_PER_SECOND)


type Sleeper = Callable[[int], Awaitable[None]]
"""A PEP 695 alias so `Awaitable` and `Callable` can stay in the type-checking block."""


def strip_query(url: httpx.URL | str) -> httpx.URL:
    """The URL with its query string, fragment and userinfo removed.

    The query string because one exchange signs its requests there, so a logged URL would
    otherwise carry a valid signature and betray the key that produced it. The fragment
    because it is never sent to the server and has no business in a log either. The
    userinfo because `https://key:secret@host/...` is a credential written in a URL, and
    `httpx` will happily carry one.

    This is the rule `CLAUDE.md` states, available on its own for a future exchange
    provider. It is **not** what this module's transport logs -- for a chain provider the
    address is in the path, so `request_target` is stricter. Reaching for this one to log a
    chain request would meet the letter of the rule and leak the address anyway.
    """
    return httpx.URL(url).copy_with(query=None, fragment=None, userinfo=b"")


def host_key(url: httpx.URL) -> str:
    """What the rate limiter treats as one peer: host and port.

    Host alone was wrong for the deployment this product actually has. A Raspberry Pi
    running a self-hosted Esplora on one port and a Kaspa REST server on another gives
    both the same hostname, so they would have shared a single 250 ms budget -- halving
    the throughput of each because the other exists, while `HostRateLimiter`'s docstring
    promised the opposite.

    `httpx` normalises a default port away, so `https://x.test` and `https://x.test:443`
    remain one key and are one server. `:8443` is its own key, which is the case that
    matters.

    The scheme is not in the key. The same host and port over http and https is the same
    listener being addressed two ways, not two vendors, and pacing it twice as fast would
    be the original bug wearing a different hat.
    """
    return url.netloc.decode("ascii")


def request_target(request: httpx.Request) -> str:
    """What a request is allowed to be called in a log: scheme, host, and a label.

    **The path is never included.** Esplora's `GET /address/:address` and Kaspa's
    `GET /addresses/{address}/balance` both put the owner's address in the path, so a log
    line built from the path would disclose exactly what the wallet registry refuses to.

    The label comes from `request.extensions["endpoint"]` -- a constant the provider
    chooses, such as `"address_balance"`, which says what kind of call it was without
    saying what it was about.

    **The label is validated against `ENDPOINT_LABEL`, not merely checked for being a
    string, and that is the difference between a convention and a guarantee.** Until it
    was, this function's promise held only for labels we had thought of. Measured on this
    branch, with a well-behaved label the security tests stayed green throughout:

        'address_balance'              -> https://api.example/address_balance
        'address/tb1qw508d6q...'       -> https://api.example/address/tb1qw508d6q...
        '/address/tb1q.../utxo'        -> https://api.example//address/tb1q.../utxo
        'address_balance/tb1qw508d6qe' -> https://api.example/address_balance/tb1qw508d6qe

    The third line is the realistic one, and the cause is helpfulness rather than malice:
    a provider author who wants more detail in the log writes
    `extensions={"endpoint": request.url.path}`, or appends an address prefix to
    correlate two lines, and every retry and failure line carries it -- out of a function
    whose docstring says it cannot. Anything not matching the pattern now renders as
    `UNLABELLED`, empty string included.

    **The residual, stated so nobody reads more into the pattern than it says.** A label
    that *is* a short address still passes: a bech32 address is lowercase alphanumeric and
    `"tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx".isidentifier()` is `True`. What the
    pattern makes structurally impossible is the *accidental* case -- anything carrying a
    slash, a dot, a colon, a percent-escape, whitespace or an upper-case character cannot
    get through. Deliberately naming a label after an address is still possible and is no
    longer something anyone does by accident. The honest completion of this is an
    allowlist of known labels, the same shape as `PUBLIC_API_PATHS`; it is not here
    because there are no providers yet and an empty allowlist would render every real
    request `<unlabelled>`. It belongs with #7.

    The length cap is hygiene, not the control -- it bounds a log line, and it happens to
    reject a full 42-character bech32 address while doing nothing at all about a truncated
    one. A truncated address is still an address, which is why the shape and not the
    length is what this rests on.

    The port is left out too. It identifies a deployment, not a call, and it is one more
    thing a reader might mistake for part of the target.
    """
    label = request.extensions.get(ENDPOINT_EXTENSION)
    endpoint = label if isinstance(label, str) and ENDPOINT_LABEL.fullmatch(label) else UNLABELLED
    return f"{request.url.scheme}://{request.url.host}/{endpoint}"


def parse_retry_after(
    value: str | None,
    now: datetime,
    *,
    cap_ms: int | None = None,
) -> int | None:
    """`Retry-After` as a number of milliseconds to wait, or `None` if it says nothing.

    Pure: the clock arrives as an argument rather than being read, so the HTTP-date arm is
    testable without freezing time. `now` must be timezone-aware; `utc_now` is the one the
    transport passes.

    RFC 9110 section 10.2.3 gives two forms and section 5.6.7 gives three spellings of the
    second, all of which a recipient has to accept. Four details are worth writing down
    because each one was a real defect before it was a line of code:

    * **`delay-seconds` is `1*DIGIT`, which means ASCII digits and nothing else.**
      `str.isdigit()` is `True` for `"²"`, which `int()` then refuses with a `ValueError`,
      and for `"٢"`, which `int()` cheerfully reads as 2. So the guard is
      `isascii() and isdigit()`, which is the grammar rather than a defensive hack. It
      also settles `"-5"`, `"+5"` and `"5.5"`: none is `1*DIGIT`, none is an HTTP-date,
      so all three are simply invalid.
    * **`parsedate_to_datetime` returns a *naive* datetime for the asctime form.**
      `"Sun Nov  6 08:49:37 1994"` is the one of the three date spellings with no zone in
      it, and it comes back with `tzinfo=None`; subtracting an aware `now` from it raises
      `TypeError`. That fires only when a server sends an asctime `Retry-After` -- which is
      to say only during an outage, when this path is the thing keeping the sync alive.
      RFC 9110 says to read a zoneless HTTP-date as GMT, so that is what happens here.
      ruff's DTZ rules do not catch this; it is a runtime failure, not a lint.
    * **A date already in the past clamps to zero**, never to a negative sleep.
    * **`0` is not `None`.** `Retry-After: 0` means "immediately" and a missing header
      means "no opinion, use the computed backoff". The return type has to tell them apart,
      so the caller must test `is not None` rather than truthiness.

    An unparseable value returns `None` rather than raising. A malformed header is not a
    reason to fail a request that would have succeeded on the next attempt.

    Args:
        value: the raw header, or `None` when the response did not carry one.
        now: the current instant, timezone-aware. Only the HTTP-date form reads it.
        cap_ms: the ceiling to clamp the result to, normally `RetryPolicy.max_backoff_ms`.
            A hostile or broken server sending `Retry-After: 86400` must not be able to
            stall the sync for a day. Omitted, the header is reported as it stands.

    Returns:
        Milliseconds to wait, at least zero, or `None` if the header said nothing usable.

    Raises:
        ValueError: `now` is naive. Checked here, before either arm, rather than being
            left to the subtraction in `_http_date_ms`. A naive clock is a caller's bug
            in every case, but only the HTTP-date arm would notice it -- so
            `parse_retry_after("5", naive_now)` used to succeed and the defect waited for
            the first server that answered with a date, which is to say for an outage.
            A refusal at the boundary turns a latent crash into an immediate, obvious one.
    """
    if now.tzinfo is None:
        message = "parse_retry_after requires a timezone-aware `now`; got a naive datetime"
        raise ValueError(message)
    if value is None:
        return None
    candidate = value.strip()
    delay_ms = _delay_seconds_ms(candidate)
    if delay_ms is None:
        delay_ms = _http_date_ms(candidate, now)
    if delay_ms is None:
        return None
    return delay_ms if cap_ms is None else min(delay_ms, cap_ms)


def _delay_seconds_ms(candidate: str) -> int | None:
    """The `delay-seconds` form, or `None` if this is not one.

    `isascii()` before `isdigit()`: see `parse_retry_after` for the two Unicode digits
    that make the difference between a crash and a wrong answer.
    """
    if not candidate or not candidate.isascii() or not candidate.isdigit():
        return None
    return int(candidate) * MILLISECONDS_PER_SECOND


def _http_date_ms(candidate: str, now: datetime) -> int | None:
    """The HTTP-date form as a delay from `now`, clamped at zero, or `None`.

    The arithmetic runs on `timedelta`'s integer parts rather than on
    `total_seconds()`, which returns a float. Nothing here would be corrupted by that
    float, but the rule in this package is that durations are integers, and a single
    exception is how a rule becomes a suggestion.
    """
    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        return None
    # RFC 9110 section 5.6.7: an HTTP-date is always GMT, and the asctime spelling has no
    # zone to say so. Without this the subtraction below raises.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = parsed - now
    delay_ms = (
        delta.days * SECONDS_PER_DAY * MILLISECONDS_PER_SECOND
        + delta.seconds * MILLISECONDS_PER_SECOND
        + delta.microseconds // MICROSECONDS_PER_MILLISECOND
    )
    return max(0, delay_ms)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to try, how long to wait, and which requests are eligible.

    Pure data: no clock, no sleep, no random source. Those are constructor arguments on
    `RetryingTransport`, which keeps a policy comparable in a test and keeps the test
    seams in one place.

    `retry_methods` defaults to the idempotent pair. **Kaspa's batch balance call is a
    read expressed as `POST /addresses/balances`**, so #8 opts that provider in explicitly
    -- one visible line in a diff, which is the house rule for making something less safe.
    A `POST` is not retried by default because a transport error can arrive after the
    server already applied the request, and this transport cannot know which.
    """

    max_attempts: int = 3
    """Total attempts, not retries. `1` disables retrying without disabling the transport."""

    base_backoff_ms: int = 250
    """The bound on the first wait. Doubles per attempt until `max_backoff_ms`."""

    max_backoff_ms: int = 30_000
    """The ceiling on any single wait, computed or demanded by `Retry-After`.

    Thirty seconds rather than a few: a vendor that asks for sixty should get most of what
    it asked for, because ignoring a `Retry-After` is how a soft throttle becomes a ban.
    The ceiling is there for the server that asks for a day, not to second-guess a
    reasonable request.
    """

    retry_methods: frozenset[str] = frozenset({"GET", "HEAD"})
    """Upper-case HTTP method tokens. Compared against `request.method.upper()`."""

    retry_statuses: frozenset[int] = field(default=RETRYABLE_STATUSES)
    """Which response codes are worth asking again about. 429 and 5xx by default."""

    def __post_init__(self) -> None:
        """Refuse a policy that would make the request loop never run, or never stop.

        Only `max_attempts` is checked. A negative backoff simply produces a zero wait,
        and a validator for every field would be four branches nothing can reach in
        production; this one is different because `handle_async_request` loops until it
        decides an attempt was the last, and `max_attempts = 0` would mean there is no
        such attempt.

        Raises:
            ValueError: `max_attempts` is below one.
        """
        if self.max_attempts < 1:
            message = f"max_attempts must be at least 1, got {self.max_attempts}"
            raise ValueError(message)


DEFAULT_RETRY_POLICY: Final = RetryPolicy()


class HostRateLimiter:
    """A minimum interval between requests to the same host.

    A leaky bucket of size one rather than a token bucket with a burst allowance. Two
    reasons, and the first is the one that matters: it is verifiable against an injected
    clock without a single timing assertion, whereas a burst allowance needs either real
    time or a far more elaborate fake. The second is that this application polls a handful
    of addresses on a schedule and has no burst to allow for. If a measurement ever asks
    for one, adding it is a change to this class and to nothing else.

    Per host, because being throttled by one vendor is no reason to slow down calls to
    another. Keyed on host **and port** -- see `host_key` -- because on a Raspberry Pi
    running two self-hosted indexes the hostname is the same for both, and keying on it
    alone would make them share one budget while this docstring claimed they did not.

    The clock is monotonic and integer -- see `monotonic_ms` -- so a wall-clock step
    backwards cannot stall it and a step forwards cannot make it stop limiting.

    Not safe across processes, and it does not need to be: there is one instance of this
    application, and the limiter's state lives on the transport, which lives on the
    process-wide client.
    """

    def __init__(
        self,
        *,
        min_interval_ms: int,
        clock: Callable[[], int] = monotonic_ms,
        sleep: Sleeper = sleep_ms,
    ) -> None:
        self._min_interval_ms = min_interval_ms
        self._clock = clock
        self._sleep = sleep
        self._next_allowed_ms: dict[str, int] = {}
        self._lock = anyio.Lock()

    async def acquire(self, host: str) -> None:
        """Wait, if necessary, until this host may be called again.

        `host` is whatever string identifies a peer; the transport passes `host_key(url)`,
        which is host and port. A caller may pass a bare hostname and get per-hostname
        pacing, which is what a test usually wants.

        The bookkeeping happens under a lock and the sleeping happens outside it. That
        ordering is the whole design: each waiter claims its slot the moment it arrives,
        so two concurrent requests to one host are spaced by the interval instead of both
        reading the same "next allowed" instant, both deciding to wait the same amount,
        and both firing together.

        `min_interval_ms = 0` is a legitimate configuration meaning "do not limit"; it
        takes the same path and computes a zero wait, rather than being a special case
        with a branch of its own.
        """
        async with self._lock:
            now_ms = self._clock()
            start_ms = max(now_ms, self._next_allowed_ms.get(host, now_ms))
            self._next_allowed_ms[host] = start_ms + self._min_interval_ms
            wait_ms = start_ms - now_ms
        if wait_ms > 0:
            await self._sleep(wait_ms)


class RetryingTransport(httpx.AsyncBaseTransport):
    """Wraps a transport with rate limiting, bounded retry and the logging contract.

    A transport and not a helper function, because a helper is something a caller has to
    remember. `httpx.AsyncHTTPTransport(retries=...)` was the other candidate and does not
    do the job: it retries connection failures only, never a 429 or a 5xx, and it has no
    way to honour `Retry-After`.

    Everything non-deterministic is a constructor argument -- the jitter source, the
    sleep, the wall clock -- so that no test of this class asserts on elapsed time.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        limiter: HostRateLimiter | None = None,
        jitter: Callable[[int], int] = secrets.randbelow,
        sleep: Sleeper = sleep_ms,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """Wrap `transport`.

        `jitter` defaults to `secrets.randbelow` and is injected so a test can make the
        backoff deterministic. `random.uniform` was the obvious alternative and is wrong
        twice over: it returns a float, which is banned here, and it trips ruff's S311.
        `randbelow` returns an `int` and does neither. Nothing here needs a
        cryptographically strong number, but the one in the standard library that returns
        an integer happens to be the strong one.

        `limiter` defaults to a fresh `HostRateLimiter` on `DEFAULT_MIN_HOST_INTERVAL_MS`
        sharing this transport's `sleep`, so there is exactly one request path rather than
        a limited one and an unlimited one. A caller that genuinely wants no limiting
        passes `HostRateLimiter(min_interval_ms=0)`, which is a visible choice.
        """
        self._next = transport
        self._policy = policy
        self._limiter = (
            limiter
            if limiter is not None
            else HostRateLimiter(min_interval_ms=DEFAULT_MIN_HOST_INTERVAL_MS, sleep=sleep)
        )
        self._jitter = jitter
        self._sleep = sleep
        self._now = now

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Make the request, retrying within the policy's budget.

        Retried on a transport error and on any status in `policy.retry_statuses`, and
        only for a method in `policy.retry_methods`. A non-retryable response -- a 400, a
        404, a 200 -- is returned on the first attempt without inspection.

        **Two failure shapes leave here, and neither is a `ProviderError`.** A transport
        error that outlives the retry budget propagates as the `httpx.TransportError` it
        already is; a response with a failing status is returned as a response. The
        provider translates both. That split was argued the other way first -- have the
        transport raise `ProviderUnavailableError` so no service ever imports `httpx` --
        and it is wrong for two reasons worth recording, because the tempting version is
        the one that was rejected:

        * This class implements `httpx.AsyncBaseTransport`, and `httpx` annotates the
          exceptions it recognises with the request that failed. A transport raising
          something foreign is not a well-behaved transport, and the request context is
          lost from the traceback exactly when it is wanted.
        * Turning a connection failure into a `ProviderError` while a 503 stays a response
          would leave a provider handling one concept in two shapes. The transport owns
          mechanics -- pacing, attempts, backoff, what may be logged. What a failure
          *means* for a balance is the provider's, and that is where the one translation
          lives.

        Nothing is swallowed either way: no synthetic response is ever invented for a
        request that got no answer, because "the chain said nothing" and "the chain said
        something unhelpful" must not collapse into the same zero.
        """
        target = request_target(request)
        retryable = request.method.upper() in self._policy.retry_methods
        if retryable:
            # Materialise the body so a second attempt can replay it. A no-op for a GET
            # and for any request built from bytes, which is every request this
            # application makes today; it matters the moment a provider opts a streaming
            # POST into `retry_methods`, because a consumed stream would otherwise make
            # the retry send an empty body and the failure would look like a vendor bug.
            await request.aread()

        attempt = 0
        while True:
            attempt += 1
            final = attempt >= self._policy.max_attempts
            await self._limiter.acquire(host_key(request.url))
            try:
                response = await self._next.handle_async_request(request)
            except httpx.TransportError as error:
                if final or not retryable:
                    _logger.error(
                        "provider_request_failed",
                        target=target,
                        attempt=attempt,
                        reason=type(error).__name__,
                    )
                    raise
                await self._wait(
                    self._backoff_ms(attempt),
                    target=target,
                    attempt=attempt,
                    reason=type(error).__name__,
                )
                continue

            if final or not retryable or response.status_code not in self._policy.retry_statuses:
                self._log_outcome(target=target, attempt=attempt, status=response.status_code)
                return response

            delay_ms = self._response_delay_ms(response, attempt)
            # Release the connection before sleeping. The body is discarded unread and is
            # never logged: an error body from a public index can echo the request, which
            # is to say the address.
            await response.aclose()
            await self._wait(
                delay_ms,
                target=target,
                attempt=attempt,
                reason=f"status {response.status_code}",
            )

    async def aclose(self) -> None:
        """Close the wrapped transport, so `AsyncClient.aclose()` still frees the pool."""
        await self._next.aclose()

    def _log_outcome(self, *, target: str, attempt: int, status: int) -> None:
        """Log the response this transport is about to return, at a level its status earns.

        **Any failing status logs at error, whether or not it was retried**, and that is
        the correction to a real hole rather than a tidy-up. The two return paths used to
        log differently: an exhausted retry logged at error, while a response the policy
        never retried logged at debug -- invisible at every production log level. A 400, a
        401, a 404 all took that path, and so did a 503 on a `POST`, since `retry_methods`
        defaults to `{"GET", "HEAD"}` and Kaspa's batch balance call is a `POST`. So the
        most likely failing request in the system was also the quietest one, and because
        `docs/providers.md` tells a provider not to log its own URL, there would have been
        no record of the sync failing anywhere at all.

        One method rather than a level chosen at each `return`, so the two paths cannot
        drift apart again.
        """
        if status >= HTTP_ERROR_FLOOR:
            _logger.error("provider_request_failed", target=target, attempt=attempt, status=status)
        else:
            _logger.debug("provider_request", target=target, attempt=attempt, status=status)

    async def _wait(self, delay_ms: int, *, target: str, attempt: int, reason: str) -> None:
        """Log the retry and sleep for it.

        One place, so the warning and the sleep cannot drift apart and report a delay that
        is not the one actually taken.
        """
        _logger.warning(
            "provider_request_retry",
            target=target,
            attempt=attempt,
            delay_ms=delay_ms,
            reason=reason,
        )
        await self._sleep(delay_ms)

    def _response_delay_ms(self, response: httpx.Response, attempt: int) -> int:
        """How long to wait after this response: what the server asked for, or backoff.

        A `Retry-After` the server sent is honoured, clamped to `max_backoff_ms` above and
        floored by our own backoff below. An absent or unparseable header falls back to
        the backoff alone -- `parse_retry_after` returns `None` for both, which is why `0`
        has to be distinguishable from "nothing to say".

        **The floor matters as much as the ceiling, and only the ceiling was here first.**
        `Retry-After: 0` is valid and means "immediately", and so does any HTTP-date that
        has already passed -- five seconds of clock skew against an absolute date is
        enough. Taking the header at its word then fires every remaining attempt back to
        back, spaced only by the limiter, at the host that has just told us it is
        struggling. `max` means a server can lengthen our wait but never shorten it.

        Note what that does *not* claim: it is not a guarantee of a non-zero wait, because
        full jitter draws from `[0, bound)` and may legitimately return near zero. The
        guarantee is that the delay is never *less than what our own policy would have
        chosen*, so a server cannot turn a randomised backoff into a deterministic hammer.
        """
        demanded_ms = parse_retry_after(
            response.headers.get("retry-after"),
            self._now(),
            cap_ms=self._policy.max_backoff_ms,
        )
        backoff_ms = self._backoff_ms(attempt)
        return backoff_ms if demanded_ms is None else max(demanded_ms, backoff_ms)

    def _backoff_ms(self, attempt: int) -> int:
        """Full jitter: a draw from `[0, min(cap, base * 2**(attempt - 1)))`.

        **Full jitter, not backoff-plus-noise.** The additive form -- `base * 2**n` plus a
        small random amount -- leaves every client's retries clustered exactly where the
        exponential put them, so a fleet that was throttled together stays synchronised
        and retries together. Only drawing from the whole interval decorrelates them.
        There is one client here today, but the property is free and the alternative is
        the kind of thing nobody revisits.

        The exponential is a left shift rather than `2 ** attempt` for the reason
        `domain/money.py` gives for the same choice: typeshed types `int.__pow__` as
        returning `Any`, because a negative exponent produces a float, and an `Any` is how
        a float gets back into a package that bans them. `<<` is typed `int -> int`.

        `attempt` is one-based, so the first wait is bounded by `base_backoff_ms` itself.
        """
        bound_ms = min(self._policy.max_backoff_ms, self._policy.base_backoff_ms << (attempt - 1))
        # `secrets.randbelow` requires a positive bound, and a policy may legitimately
        # configure a zero backoff.
        return self._jitter(bound_ms) if bound_ms > 0 else 0


def build_http_client(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    limiter: HostRateLimiter | None = None,
    jitter: Callable[[int], int] = secrets.randbelow,
    sleep: Sleeper = sleep_ms,
    now: Callable[[], datetime] = utc_now,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    """The client every provider shares, with the retry transport already wrapped around it.

    **Process-wide by construction.** The rate limiter's state lives on the transport and
    the transport lives on the client, so two clients would not know about each other's
    requests and the interval would silently become half of what it says. Nothing calls a
    provider yet, so nothing builds one of these at startup; #10 creates it in the
    lifespan when it has a caller, and closing it there is how the connection pool is
    released. `docs/providers.md` records that as work #10 owns.

    Args:
        transport: the transport to wrap. Defaults to a real `httpx.AsyncHTTPTransport`;
            a test passes an `httpx.MockTransport` and gets the whole retry, limiter and
            logging path over it.
        policy: how many attempts, how long to wait, and for which methods and statuses.
        limiter: shared per-host pacing. Defaults to `DEFAULT_MIN_HOST_INTERVAL_MS`.
        jitter: the random source for the backoff, injected so a test is deterministic.
        sleep: how to wait, injected so a test never waits.
        now: the wall clock, read only for `Retry-After`'s HTTP-date form.
        timeout: connect, read, write and pool, all four explicit.
    """
    retrying = RetryingTransport(
        transport=transport if transport is not None else httpx.AsyncHTTPTransport(),
        policy=policy,
        limiter=limiter,
        jitter=jitter,
        sleep=sleep,
        now=now,
    )
    # Redirects are not followed. `httpx` would build the next request itself, which means
    # a request this transport's limiter never accounted for, to a host it never paced,
    # carrying a `Location` the vendor chose. A balance API that starts redirecting is a
    # change the provider should notice rather than absorb.
    return httpx.AsyncClient(transport=retrying, timeout=timeout, follow_redirects=False)
