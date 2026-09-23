"""Criterion 4 of #8: `ratelimit-*` response headers, parsed and distrusted.

**Every header in this file is synthesised, and nothing in production sends one.**
Measured against the live Kaspa REST service on 2026-09-23: neither `GET /info/health` nor
the balance endpoint returns any `ratelimit-*` or `x-ratelimit-*` header. What they return
instead is `Server: cloudflare`, `cf-cache-status` and `CF-RAY` -- the API sits behind a
CDN, so the throttle, when it arrives, is Cloudflare's: a 429 carrying `Retry-After`, which
`parse_retry_after` has honoured since #6, or a 403 for a block, which #7's failover moves
on from.

The criterion says "when present", so the parser is built and the criterion is met. It is
built knowing that nothing in production exercises it, and this docstring says so because
**a suite that looks like it exercises production behaviour when it cannot is the kind of
green that lies**. A self-hosted Kaspa instance without a CDN in front of it may well send
these, which is the deployment this parser is actually for.

## What is asserted, and why each one is a separate test

The function is pure -- headers in, a hint or `None` out, no clock, no host, no state --
because the pacing decision belongs to `HostRateLimiter` and a parser that also paced would
be two subjects in one place. So the parsing is tested here against synthesised headers and
the *consequence* is tested in `tests/providers/test_rate_limiter.py`, where
`test_a_zero_remaining_budget_waits_for_the_reset` drives the hint through the limiter.

**Every value is clamped and every failure is ignored rather than raised**, for the reason
`parse_retry_after` already gives: a malformed header is not a reason to fail a request
that would otherwise have succeeded, and a server asking us to wait a day must not be able
to stall a sync for a day.
"""

from __future__ import annotations

import inspect
from typing import Final

import httpx
import pytest

from portfolio.providers.http import (
    DEFAULT_RETRY_POLICY,
    RateLimitHint,
    parse_rate_limit,
)

#: A ceiling small enough to write down, and deliberately not the shipped one. An assertion
#: written against `DEFAULT_RETRY_POLICY.max_backoff_ms` would pass for any production
#: value at all; `test_the_shipped_ceiling_is_what_clamps_an_absurd_reset` pins that
#: separately, which is the pair this suite uses everywhere else.
CAP_MS: Final = 5_000

#: Seconds, as the IETF draft words `ratelimit-reset`: a delay, not an instant. Nothing in
#: this module reads a wall clock, so an NTP step cannot turn a pause into an hour.
#:
#: Deliberately **below** `CAP_MS`, and `test_a_reset_below_the_ceiling_is_left_exactly_as
#: _it_stands` asserts that relationship rather than trusting this comment -- the first
#: draft of this file had 7 seconds against a 5 second ceiling, so the test that was
#: supposed to prove the clamp leaves a small value alone was driving a value the clamp
#: was right to cut. A fixture that has stopped being the case a test is about is a test
#: that passes for the wrong reason, which is worse than one that fails.
RESET_SECONDS: Final = 2
RESET_MS: Final = RESET_SECONDS * 1000

#: A day in seconds. The value a hostile or broken server sends, and the reason for a cap.
ONE_DAY_SECONDS: Final = 86_400


def headers(**fields: str) -> httpx.Headers:
    """`httpx.Headers`, because that is what a response hands the transport.

    Built as the real type rather than as a `dict`, and that is load-bearing: `httpx`
    matches header names case-insensitively, which is the whole reason the two spellings
    are two lookups rather than four. A `dict` would make this file pass while the
    production lookup missed every header a server actually sent in title case.
    """
    return httpx.Headers({name.replace("_", "-"): value for name, value in fields.items()})


def raw_reset(value: str) -> httpx.Headers:
    """A header block whose `ratelimit-reset` is arbitrary **bytes**, not a `str`.

    `httpx.Headers({...})` encodes a value as ASCII and raises on anything else, so the two
    Unicode-digit rows below cannot reach the parser through the mapping constructor at
    all. That is a fact about the fixture rather than about the parser: on the wire a
    header is bytes, a server can send whatever it likes, and `httpx` decodes what it
    receives -- so the only honest way to drive those rows is to build the block the way a
    response does.
    """
    return httpx.Headers([(b"ratelimit-remaining", b"0"), (b"ratelimit-reset", value.encode())])


# --------------------------------------------------------------------------------------
# The two spellings
# --------------------------------------------------------------------------------------


def test_both_header_spellings_are_read() -> None:
    """The IETF draft's `ratelimit-*` and the older `x-ratelimit-*`, to the same hint.

    Both are in the wild and a server sends one or the other, never both. Reading only the
    draft spelling would make this parser silently dead against every server that predates
    it -- which is most of them -- and the symptom would be no symptom at all: the pacing
    simply would not happen, and nothing anywhere would say why.

    Asserted as equality between the two hints rather than field by field on each, so a
    parser that read one spelling into the right fields and the other into the wrong ones
    fails here.
    """
    draft = parse_rate_limit(
        headers(ratelimit_limit="60", ratelimit_remaining="0", ratelimit_reset=str(RESET_SECONDS))
    )
    legacy = parse_rate_limit(
        headers(
            x_ratelimit_limit="60",
            x_ratelimit_remaining="0",
            x_ratelimit_reset=str(RESET_SECONDS),
        )
    )

    assert draft == RateLimitHint(limit=60, remaining=0, reset_ms=RESET_MS)
    assert legacy == draft


def test_the_header_names_are_matched_without_regard_to_case() -> None:
    """A server sending `RateLimit-Remaining` in title case is the ordinary case.

    `httpx.Headers` folds the case, so this is a property of the type rather than of the
    parser -- which is exactly why it is asserted: a well-meaning rewrite onto a plain
    `dict`, or onto `headers.raw`, would lose it, and every assertion above would still
    pass because they all spell their names in lower case.
    """
    hint = parse_rate_limit(
        headers(**{"RateLimit-Remaining": "0", "RateLimit-Reset": str(RESET_SECONDS)})
    )

    assert hint is not None
    assert hint.remaining == 0
    assert hint.reset_ms == RESET_MS


def test_the_draft_spelling_wins_when_a_server_sends_both() -> None:
    """A deterministic answer for a case that should not happen and sometimes does.

    A proxy adding the legacy spelling in front of an origin that already sent the draft
    one produces both, with different numbers. Either choice is defensible; what is not
    defensible is "whichever the dict happened to iterate first", which is a coin flip that
    reads as a flaky pause in production.
    """
    hint = parse_rate_limit(
        headers(
            ratelimit_remaining="0",
            ratelimit_reset=str(RESET_SECONDS),
            x_ratelimit_remaining="99",
            x_ratelimit_reset="1",
        )
    )

    assert hint is not None
    assert hint.remaining == 0
    assert hint.reset_ms == RESET_MS


# --------------------------------------------------------------------------------------
# Nothing usable means nothing, and that is not the same as zero
# --------------------------------------------------------------------------------------


def test_a_response_with_no_rate_limit_headers_at_all_says_nothing() -> None:
    """`None`, which is what every real response from this vendor produces today.

    This is the production path, and it is the one arm of this parser that actually runs:
    the service is behind Cloudflare and sends none of these. A parser returning a hint
    full of zeros here would be read by the limiter as "the budget is exhausted", and every
    Kaspa read would pause for a reset that nobody asked for.
    """
    assert parse_rate_limit(httpx.Headers()) is None
    assert parse_rate_limit(headers(server="cloudflare", cf_cache_status="DYNAMIC")) is None


@pytest.mark.parametrize(
    ("value", "why"),
    [
        pytest.param("", "an empty header, which a proxy can add", id="empty"),
        pytest.param("   ", "whitespace only", id="whitespace"),
        pytest.param("soon", "prose", id="prose"),
        pytest.param("-5", "a negative delay", id="negative"),
        pytest.param("+5", "an explicit plus, which int() accepts", id="explicit plus"),
        pytest.param("5.5", "a decimal, which the draft's grammar does not allow", id="decimal"),
        pytest.param("1e3", "scientific notation", id="scientific notation"),
        pytest.param("0x10", "hexadecimal", id="hexadecimal"),
        pytest.param("٣٤٥", "arabic-indic digits, which str.isdigit accepts", id="unicode digits"),
        pytest.param("²", "a superscript two, which int() then refuses", id="superscript"),
        pytest.param("Wed, 21 Oct 2026 07:28:00 GMT", "an HTTP-date, which this is not", id="date"),
    ],
)
def test_an_unparseable_header_is_ignored(value: str, why: str) -> None:
    """Ignored, never raised, and never guessed at. The rule `parse_retry_after` set.

    A malformed header is not a reason to fail a request that would otherwise have
    succeeded. The two Unicode rows are the ones a hand-rolled check gets wrong in opposite
    directions: `"٣٤٥".isdigit()` is `True` and `int("٣٤٥")` is `345`, so a parser built on
    either accepts a value no server sends and turns it into a pause; `"²".isdigit()` is
    also `True` and `int("²")` raises, so the same parser crashes inside a logging path.

    Every arm here has `remaining` present and valid, so a parser that bailed out on the
    whole header block rather than on the one bad field would fail this too -- the hint
    must still carry what *was* readable.

    Built from bytes rather than from a mapping, because `httpx.Headers({...})` refuses a
    non-ASCII value outright and the two Unicode rows could otherwise never be driven. See
    `raw_reset`.
    """
    del why  # In the parameter id, where a failure can read it.

    hint = parse_rate_limit(raw_reset(value))

    assert hint is not None
    assert hint.remaining == 0
    assert hint.reset_ms is None


def test_a_reset_of_zero_is_not_the_same_as_a_missing_reset() -> None:
    """`0` means "immediately" and a missing header means "no opinion".

    The same distinction `parse_retry_after` is built around, and the reason the field is
    `int | None` rather than an `int` with a sentinel. A caller must be able to test
    `is not None` rather than truthiness, because `0` is falsey and "wait no time" is a
    different instruction from "I said nothing about waiting".
    """
    said_now = parse_rate_limit(headers(ratelimit_remaining="0", ratelimit_reset="0"))
    said_nothing = parse_rate_limit(headers(ratelimit_remaining="0"))

    assert said_now is not None
    assert said_now.reset_ms == 0
    assert said_nothing is not None
    assert said_nothing.reset_ms is None


def test_a_header_block_whose_every_value_is_junk_says_nothing() -> None:
    """Three unusable values is no information, and no information is `None`.

    A hint object full of `None`s would be indistinguishable to a caller from one carrying
    a real answer until every field had been checked, which is a check every call site
    would have to remember. `None` at the boundary means the caller writes one `if`.
    """
    assert (
        parse_rate_limit(
            headers(ratelimit_limit="lots", ratelimit_remaining="none", ratelimit_reset="soon")
        )
        is None
    )


# --------------------------------------------------------------------------------------
# The clamp
# --------------------------------------------------------------------------------------


def test_an_absurd_reset_is_clamped_to_the_ceiling() -> None:
    """A server asking us to wait a day must not be able to stall a sync for a day.

    The same ceiling argument `parse_retry_after` already carries, and the same failure it
    prevents: an application that does what a broken or hostile upstream asks is one whose
    sync stops for as long as the upstream says so, with nothing in any log naming the
    cause. The clamp is asserted to the millisecond, because "it was smaller than a day" is
    satisfied by any number at all.
    """
    hint = parse_rate_limit(
        headers(ratelimit_remaining="0", ratelimit_reset=str(ONE_DAY_SECONDS)), cap_ms=CAP_MS
    )

    assert hint is not None
    assert hint.reset_ms == CAP_MS


def test_a_reset_below_the_ceiling_is_left_exactly_as_it_stands() -> None:
    """The control. A clamp that returned the cap unconditionally would pass the test above.

    Ignoring a `reset` a server asked for is how a soft throttle becomes the ban that
    mempool.space's documentation warns about, and the argument applies to any vendor with
    an unpublished limit. The ceiling exists for the server that asks for a day, not to
    second-guess a reasonable request.
    """
    hint = parse_rate_limit(
        headers(ratelimit_remaining="0", ratelimit_reset=str(RESET_SECONDS)), cap_ms=CAP_MS
    )

    assert hint is not None
    assert hint.reset_ms == RESET_MS
    assert RESET_MS < CAP_MS, "the fixture stopped being the case this test is about"


def test_no_cap_reports_the_header_as_it_stands() -> None:
    """`cap_ms` is optional and omitting it means "report what the server said".

    The same signature `parse_retry_after` has, for the same reason: the clamp is a policy
    decision that belongs to whoever holds the policy, and a pure parser that applied one
    unconditionally would have a number baked into it that no configuration could move.
    """
    hint = parse_rate_limit(headers(ratelimit_remaining="0", ratelimit_reset=str(ONE_DAY_SECONDS)))

    assert hint is not None
    assert hint.reset_ms == ONE_DAY_SECONDS * 1000


def test_the_shipped_ceiling_is_what_clamps_an_absurd_reset() -> None:
    """The number production actually applies, pinned behaviourally rather than inherited.

    Every test above injects `CAP_MS`, which is the discipline that keeps the assertions
    exact -- and it is exactly what leaves `RetryPolicy.max_backoff_ms` unobserved on this
    path. #6's lesson: a suite in which every test supplies its own value never observes
    the default, and the whole subsystem could ship with a ceiling of zero with every other
    test in this file green.
    """
    hint = parse_rate_limit(
        headers(ratelimit_remaining="0", ratelimit_reset=str(ONE_DAY_SECONDS)),
        cap_ms=DEFAULT_RETRY_POLICY.max_backoff_ms,
    )

    assert hint is not None
    assert hint.reset_ms == 30_000
    assert DEFAULT_RETRY_POLICY.max_backoff_ms == 30_000


# --------------------------------------------------------------------------------------
# The hint is data, and the parser is pure
# --------------------------------------------------------------------------------------


def test_the_parser_reads_no_clock_and_no_state() -> None:
    """Pure: the same headers produce the same hint, and there is no `now` to pass.

    `Retry-After` has an HTTP-date form and therefore needs a clock; `ratelimit-reset` is a
    **delay in seconds** by the draft, so it does not -- and must not, because the limiter
    runs on `time.monotonic` and a parser that mixed a wall-clock reading into it would let
    an NTP step turn a seven-second pause into an hour.

    Asserted twice over: the signature has no clock parameter at all, and two calls with
    one header block are equal. The signature assertion is the one that survives somebody
    adding a clock with a default.
    """
    block = headers(ratelimit_limit="60", ratelimit_remaining="3", ratelimit_reset="2")

    assert parse_rate_limit(block) == parse_rate_limit(block)
    parameters = inspect.signature(parse_rate_limit).parameters
    assert "now" not in parameters
    assert set(parameters) == {"headers", "cap_ms"}


def test_the_hint_is_frozen_so_a_limiter_cannot_edit_what_a_server_said() -> None:
    """Immutable, because it crosses from a response into the limiter's own bookkeeping.

    A mutable hint is a record the limiter can edit in place, and a record edited in place
    is one whose provenance -- "this is what the server said" -- stops being true without
    anything saying so.
    """
    hint = RateLimitHint(limit=60, remaining=0, reset_ms=RESET_MS)

    with pytest.raises((AttributeError, TypeError)):
        hint.remaining = 5  # type: ignore[misc]


def test_every_parsed_number_is_an_integer_and_never_a_bool() -> None:
    """`providers/` cannot hold a float, and `True` is an `int` that would read as one.

    The float half is rule 2's AST ban, which reads source and cannot see a value that
    arrived from a header. The `bool` half is the one the type annotation cannot stand in
    for at all: `bool` is a subtype of `int`, so a `True` that got in here would pass every
    `isinstance(..., int)` check downstream and pace the next request for one millisecond.
    """
    hint = parse_rate_limit(
        headers(ratelimit_limit="60", ratelimit_remaining="1", ratelimit_reset="2")
    )

    assert hint is not None
    for value in (hint.limit, hint.remaining, hint.reset_ms):
        assert isinstance(value, int)
        assert not isinstance(value, bool)
