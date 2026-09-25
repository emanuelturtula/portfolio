"""Criterion 3: `Retry-After`, in both RFC 9110 forms and in every way it goes wrong.

`parse_retry_after` is pure and takes the clock as an argument, so every case here is an
exact integer against a fixed instant. There is no frozen time, no monkeypatched
`datetime`, and no tolerance window.

The failure cases are the point. A `Retry-After` header is read exactly when something is
already going wrong upstream, which is the worst possible moment to discover that the
parser raises on a spelling the RFC requires a recipient to accept.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

import pytest

from portfolio.providers.http import (
    MAX_HEADER_DIGITS,
    MILLISECONDS_PER_SECOND,
    parse_retry_after,
)

#: A fixed instant with a timezone, because `parse_retry_after` compares against an aware
#: datetime and a naive one raises. Chosen rather than generated: a test that computes its
#: own "now" is a test whose failure nobody can reproduce tomorrow.
NOW: Final = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

ONE_MINUTE_MS: Final = 60 * MILLISECONDS_PER_SECOND


# --------------------------------------------------------------------------------------
# The two forms the RFC requires a recipient to accept
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected_ms"),
    [
        pytest.param("0", 0, id="immediately"),
        pytest.param("1", MILLISECONDS_PER_SECOND, id="one second"),
        pytest.param("120", 120 * MILLISECONDS_PER_SECOND, id="two minutes"),
        pytest.param("  30  ", 30 * MILLISECONDS_PER_SECOND, id="surrounded by spaces"),
    ],
)
def test_a_delay_in_seconds_is_honoured(header: str, expected_ms: int) -> None:
    """`delay-seconds`, converted to the milliseconds every duration here is measured in.

    The conversion is asserted as an exact product rather than as a magnitude: reading the
    header as milliseconds instead of seconds is a thousand-fold error that still produces
    a plausible-looking number, and a "greater than zero" assertion would not see it.
    """
    assert parse_retry_after(header, NOW) == expected_ms


@pytest.mark.parametrize(
    ("header", "expected_ms"),
    [
        pytest.param("Thu, 15 Jan 2026 12:01:00 GMT", ONE_MINUTE_MS, id="imf-fixdate"),
        pytest.param("Thursday, 15-Jan-26 12:01:00 GMT", ONE_MINUTE_MS, id="rfc 850"),
        pytest.param("Thu Jan 15 12:01:00 2026", ONE_MINUTE_MS, id="asctime, no zone"),
    ],
)
def test_an_http_date_is_honoured(header: str, expected_ms: int) -> None:
    """All three spellings of HTTP-date, because RFC 9110 5.6.7 requires all three.

    **The asctime row is the one that matters.** `parsedate_to_datetime` returns a *naive*
    datetime for it -- there is no zone in the format to read -- and subtracting an aware
    `now` from a naive datetime raises `TypeError`. That is a crash, not a wrong number,
    and it fires only when a server sends an asctime `Retry-After`, which is to say only
    during an outage, on the path that is supposed to be keeping the sync alive. ruff's
    DTZ rules do not catch it and a code review would have to know the quirk to spot it.
    """
    assert parse_retry_after(header, NOW) == expected_ms


def test_a_date_in_the_past_never_sleeps_backwards() -> None:
    """A clock skew between us and the server must not produce a negative delay.

    Negative milliseconds reach `anyio.sleep` as a negative number of seconds. Clamping
    here, at the parse, is what keeps every downstream duration non-negative by
    construction rather than by a second check somebody has to remember.
    """
    past = (NOW - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    assert parse_retry_after(past, NOW) == 0


def test_a_date_exactly_now_is_zero_and_not_none() -> None:
    """The boundary between "wait no time" and "the header said nothing"."""
    present = NOW.strftime("%a, %d %b %Y %H:%M:%S GMT")

    result = parse_retry_after(present, NOW)

    assert result == 0
    assert result is not None


# --------------------------------------------------------------------------------------
# Everything that is not one of those two forms
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(None, id="no header at all"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("soon", id="a word"),
        pytest.param("-5", id="negative seconds"),
        pytest.param("+5", id="a signed integer"),
        pytest.param("5.5", id="fractional seconds"),
        pytest.param("5s", id="a unit suffix"),
        pytest.param("1,000", id="a thousands separator"),
        pytest.param("Thu, 32 Jan 2026 12:00:00 GMT", id="an impossible day"),
        pytest.param("²", id="a superscript two, which isdigit() calls a digit"),
        pytest.param("٢", id="an arabic-indic two, which int() would read as 2"),
    ],
)
def test_an_unparseable_header_falls_back_to_the_computed_backoff(header: str | None) -> None:
    """`None` means "the header said nothing usable", and the caller uses its own backoff.

    Two of these rows are not defensive padding. `"²".isdigit()` is `True` and `int("²")`
    raises `ValueError`, so a bare `isdigit()` guard turns a stray character into a crash.
    `"٢".isdigit()` is `True` **and** `int("٢")` returns 2, so the same guard turns it into
    a silently wrong delay. `isascii() and isdigit()` is RFC 9110's `1*DIGIT` and refuses
    both. The other rows pin that a malformed header is ignored rather than raised on: a
    broken header is not a reason to fail a request that would have succeeded next time.
    """
    assert parse_retry_after(header, NOW) is None


def test_the_unicode_digit_cases_are_real_and_not_folklore() -> None:
    """The control for the two rows above, asserted against Python rather than assumed.

    If a future Python changed either behaviour, the guard in `parse_retry_after` might no
    longer be necessary -- and this test is what would say so, instead of leaving a
    defensive branch nobody dares remove because nobody remembers why it is there.
    """
    assert "²".isdigit() is True
    assert "²".isascii() is False
    with pytest.raises(ValueError, match=r"invalid literal"):
        int("²")

    assert "٢".isdigit() is True
    assert "٢".isascii() is False
    assert int("٢") == 2


# --------------------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------------------


def test_an_absurd_delay_is_clamped_to_the_ceiling() -> None:
    """A server asking for a day must not be able to stall the sync for a day.

    Hostile or merely broken -- `Retry-After: 86400` appears in the wild from
    misconfigured proxies -- and the outcome is the same either way: a Raspberry Pi that
    looks hung, with nothing in the log to say it is waiting rather than wedged.
    """
    cap_ms = 30_000

    assert parse_retry_after("86400", NOW, cap_ms=cap_ms) == cap_ms


def test_a_reasonable_delay_is_not_clamped() -> None:
    """The control. A cap that clamped everything would satisfy the test above.

    Ignoring a `Retry-After` is how a soft throttle becomes a ban, so the ceiling exists
    for the absurd value and must leave a sensible one alone.
    """
    cap_ms = 30_000

    assert parse_retry_after("5", NOW, cap_ms=cap_ms) == 5 * MILLISECONDS_PER_SECOND


def test_the_cap_applies_to_the_date_form_too() -> None:
    """Both forms reach the same clamp, or the clamp is a suggestion.

    A parser that capped `delay-seconds` and not HTTP-date would leave the whole hazard
    intact behind a header spelling, and the test for the seconds form would still pass.
    """
    far_future = (NOW + timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    cap_ms = 30_000

    assert parse_retry_after(far_future, NOW, cap_ms=cap_ms) == cap_ms


def test_without_a_cap_the_header_is_reported_as_it_stands() -> None:
    """The two-argument call is the parse; the clamp is the caller's policy.

    Keeping them separable is what lets this module be tested for what it read and the
    transport be tested for what it decided, rather than one test covering both and
    neither saying which was wrong.
    """
    assert parse_retry_after("86400", NOW) == 86_400 * MILLISECONDS_PER_SECOND


def test_a_cap_of_zero_is_honoured_rather_than_treated_as_absent() -> None:
    """`cap_ms=0` is "never wait", and a falsy check would read it as "no cap at all"."""
    assert parse_retry_after("60", NOW, cap_ms=0) == 0


# --------------------------------------------------------------------------------------
# The clock the caller passes in
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("5", id="delay-seconds, which never reads the clock"),
        pytest.param("Thu, 15 Jan 2026 12:01:00 GMT", id="an http-date, which does"),
        pytest.param("nonsense", id="unparseable, which returns before either arm"),
        pytest.param(None, id="no header at all"),
    ],
)
def test_a_naive_now_is_refused_whatever_the_header_says(header: str | None) -> None:
    """The guard goes at the top, because otherwise it only fires during an outage.

    Subtracting an aware datetime from a naive one raises `TypeError`, and only the
    HTTP-date arm subtracts. So before this guard, `parse_retry_after("5", naive)`
    succeeded and `parse_retry_after(date, naive)` crashed -- meaning a caller could pass
    a naive clock, see every test and every ordinary response work, and discover the bug
    on the first server that answered with a date. Which is to say: during an outage, on
    the path that is meant to be keeping the sync alive.

    Refusing every call with a naive `now` turns that into an immediate, total failure at
    the call site, which is what a programming error should be.
    """
    naive = datetime(2026, 1, 15, 12, 0, 0)  # noqa: DTZ001 - a naive clock is the subject

    with pytest.raises(ValueError, match=r"(?i)aware|naive|timezone|tzinfo"):
        parse_retry_after(header, naive)


def test_an_aware_now_in_another_zone_is_accepted() -> None:
    """The control: the guard is about awareness, not about UTC specifically.

    A guard written as `now.tzinfo is not UTC` would pass the test above and reject a
    perfectly good clock. Both arms are driven, because only one of them reads `now`.
    """
    elsewhere = NOW.astimezone(timezone(timedelta(hours=9)))

    assert parse_retry_after("5", elsewhere) == 5 * MILLISECONDS_PER_SECOND
    assert parse_retry_after("Thu, 15 Jan 2026 12:01:00 GMT", elsewhere) == ONE_MINUTE_MS


def test_zero_and_no_header_stay_different_answers_at_the_parser() -> None:
    """The concern that used to be asserted through the transport, kept where it now lives.

    `test_a_retry_after_of_zero_means_immediately_and_not_use_the_backoff` asserted this
    one layer up, and the `Retry-After` floor deliberately changed what the transport does
    with a zero -- a server can lengthen our wait but no longer shorten it. The mistake
    the old test was written to catch is still a real one and still lives here: `if
    demanded:` instead of `if demanded is not None:` collapses "wait no time" into "the
    header said nothing", and the two have to stay distinguishable at the boundary that
    produces them.
    """
    zero = parse_retry_after("0", NOW)
    absent = parse_retry_after(None, NOW)

    assert zero == 0
    assert zero is not None
    assert absent is None


# --------------------------------------------------------------------------------------
# A value the interpreter would refuse, which used to escape as a bare exception (#13)
# --------------------------------------------------------------------------------------
#
# `1*DIGIT` has no length limit, and `int()` refuses more than 4300 digits with a
# `ValueError`. The transport reads `Retry-After` on every retryable response, so a vendor
# header of five thousand digits made `client.get` raise past every provider's
# `except httpx.TransportError`. `MAX_HEADER_DIGITS` is the bound this application chose;
# past it, the header says nothing usable.

#: Ten nines: the longest run the bound admits, written out rather than derived from it.
AT_THE_BOUND: Final = "9999999999"


def test_the_header_digit_bound_is_ten() -> None:
    """Pinned as a literal: ten digits of seconds is over three centuries of waiting."""
    assert MAX_HEADER_DIGITS == 10
    assert len(AT_THE_BOUND) == MAX_HEADER_DIGITS


def test_the_five_thousand_digit_case_is_one_the_interpreter_refuses() -> None:
    """The premise: without the bound, this is what `int()` does with such a header."""
    assert sys.get_int_max_str_digits() < 5000
    with pytest.raises(ValueError, match="digits"):
        int("1" * 5000)


@pytest.mark.parametrize(
    ("header", "expected_ms"),
    [
        pytest.param(AT_THE_BOUND, 9_999_999_999_000, id="ten nines"),
        pytest.param("0000000001", 1_000, id="ten digits, leading zeros"),
    ],
)
def test_a_delay_at_the_digit_bound_is_read(header: str, expected_ms: int) -> None:
    assert parse_retry_after(header, NOW) == expected_ms


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("1" * 11, id="eleven digits"),
        pytest.param("0" * 10 + "1", id="eleven digits, leading zeros"),
        pytest.param("1" * 5000, id="five thousand digits"),
        pytest.param("1" * (sys.get_int_max_str_digits() + 1), id="past the interpreter's limit"),
    ],
)
def test_a_delay_past_the_digit_bound_is_unusable(header: str) -> None:
    """`None`, with a cap and without one: the bound is on the text, before `int()`."""
    assert parse_retry_after(header, NOW) is None
    assert parse_retry_after(header, NOW, cap_ms=30_000) is None


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("Sun, 06 Nov 99999999999999999999 08:49:37 GMT", id="a twenty-digit year"),
        pytest.param("Sun, 06 Nov 1994 99999999999999999999:49:37 GMT", id="a twenty-digit hour"),
        pytest.param("Sun, 06 Nov 1994 08:49:37 +99999999999999999999", id="a twenty-digit zone"),
    ],
)
def test_a_date_whose_fields_overflow_is_unusable(header: str) -> None:
    """`parsedate_to_datetime` raises `OverflowError` for these, which is not a `ValueError`."""
    assert parse_retry_after(header, NOW) is None
