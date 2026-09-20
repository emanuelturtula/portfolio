"""Criterion 7: five failures inside fifteen minutes, and the sixth attempt is refused."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from portfolio.services.auth import (
    LOGIN_FAILURE_LIMIT,
    LOGIN_FAILURE_WINDOW,
    TOTAL_FAILURE_LIMIT,
    LoginThrottle,
)
from tests.auth.conftest import (
    JSON_HEADERS,
    LOGIN_PATH,
    OWNER_PHRASE,
    OWNER_USERNAME,
    WRONG_PHRASE,
    sign_in,
)

if TYPE_CHECKING:
    import pytest
    from fastapi import FastAPI
    from httpx import AsyncClient

WRONG_CREDENTIALS = {"username": OWNER_USERNAME, "password": WRONG_PHRASE}

# The two numbers criterion 7 actually names, written out as literals rather than read
# from the module under test.
#
# This matters more than it looks. Every other test in this file expresses its expectation
# in terms of `LOGIN_FAILURE_LIMIT` and `LOGIN_FAILURE_WINDOW`, so each one re-derives its
# own boundary from whatever the implementation currently says: raising the limit from five
# to fifty leaves all of them green while the throttle stops throttling, and shrinking the
# window to five minutes does the same. A test whose expected value is imported from the
# code it is checking can only prove the code is self-consistent. The criterion is about
# the numbers, so the numbers are pinned here.
SPEC_FAILURE_LIMIT: Final = 5
SPEC_FAILURE_WINDOW: Final = timedelta(minutes=15)

# Not from the criterion: the total counter is an addition, and this is the number the
# design settled on. Pinned for the same reason -- a limit read from the module it is
# checking can only prove the module agrees with itself.
SPEC_TOTAL_FAILURE_LIMIT: Final = 50


async def fail_login(client: AsyncClient, times: int) -> list[int]:
    """Attempt a wrong password `times` times, returning the status codes."""
    statuses = []
    for _ in range(times):
        response = await client.post(LOGIN_PATH, json=WRONG_CREDENTIALS, headers=JSON_HEADERS)
        statuses.append(response.status_code)
    return statuses


def test_the_limit_and_window_are_the_ones_the_criterion_names() -> None:
    """Criterion 7's two constants, pinned against literals."""
    assert LOGIN_FAILURE_LIMIT == SPEC_FAILURE_LIMIT
    assert LOGIN_FAILURE_WINDOW == SPEC_FAILURE_WINDOW


async def test_exactly_five_failures_are_tolerated_and_the_sixth_is_refused(
    auth_client: AsyncClient,
) -> None:
    """Criterion 7, counted out end to end in literals: 401 five times, then 429.

    The off-by-one is the whole criterion, so the expected sequence is written down rather
    than generated from the limit. `test_sixth_failure_within_the_window_is_rejected` above
    asserts the same shape relative to the constant and therefore cannot see the constant
    move; this one can.
    """
    statuses = await fail_login(auth_client, 6)

    assert statuses == [401, 401, 401, 401, 401, 429]


def test_the_window_is_fifteen_minutes_either_side_of_the_boundary() -> None:
    """Criterion 7's window, probed just inside and just outside fifteen minutes.

    `test_failures_outside_the_window_do_not_count` probes at `started + WINDOW`, which is
    the boundary wherever the boundary happens to be -- true for a window of five minutes
    and for one of a day. These two probes are absolute, so only fifteen minutes passes
    both.
    """
    throttle = LoginThrottle()
    started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for _ in range(SPEC_FAILURE_LIMIT):
        throttle.record_failure(OWNER_USERNAME, started)

    assert throttle.is_throttled(OWNER_USERNAME, started + timedelta(minutes=14, seconds=59))
    assert not throttle.is_throttled(OWNER_USERNAME, started + timedelta(minutes=15, seconds=1))


async def test_sixth_failure_within_the_window_is_rejected(auth_client: AsyncClient) -> None:
    """Five wrong passwords are answered 401; the sixth attempt is answered 429."""
    statuses = await fail_login(auth_client, LOGIN_FAILURE_LIMIT + 1)

    assert statuses[:LOGIN_FAILURE_LIMIT] == [401] * LOGIN_FAILURE_LIMIT
    assert statuses[LOGIN_FAILURE_LIMIT] == 429


async def test_throttled_attempt_does_not_verify_the_password(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal happens *before* the hash, which is what makes throttling worth having.

    A limiter that still pays for an Argon2id verification on every refused attempt hands
    an attacker a way to keep the machine busy at a quarter of a second per request. The
    hasher is replaced with one that fails the test if it is called at all.
    """
    await fail_login(auth_client, LOGIN_FAILURE_LIMIT)

    def refuse_to_verify(encoded_hash: str, password: str) -> bool:
        del encoded_hash, password
        message = "a throttled attempt must not reach the password hasher"
        raise AssertionError(message)

    monkeypatch.setattr(auth_app.state.password_hasher, "verify", refuse_to_verify)
    response = await auth_client.post(LOGIN_PATH, json=WRONG_CREDENTIALS, headers=JSON_HEADERS)

    assert response.status_code == 429


async def test_the_correct_password_is_also_refused_while_throttled(
    auth_client: AsyncClient,
) -> None:
    """The window applies to the username, not to the guess.

    A sustained attack therefore locks the owner out for fifteen minutes. That is accepted
    -- the application is only reachable over a private network -- and it is the price of
    not letting an attacker on that network rotate source addresses around an IP limit.
    """
    await fail_login(auth_client, LOGIN_FAILURE_LIMIT)

    response = await auth_client.post(
        LOGIN_PATH,
        json={"username": OWNER_USERNAME, "password": OWNER_PHRASE},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 429


async def test_successful_login_clears_the_failure_counter(auth_client: AsyncClient) -> None:
    """Signing in is proof of ownership, so the count starts again from zero."""
    assert await fail_login(auth_client, LOGIN_FAILURE_LIMIT - 1) == [401] * (
        LOGIN_FAILURE_LIMIT - 1
    )
    await sign_in(auth_client)

    # Without the reset, one more failure would be the sixth and would be refused.
    assert await fail_login(auth_client, 1) == [401]


def test_failures_outside_the_window_do_not_count() -> None:
    """The window slides: failures older than fifteen minutes are pruned, not counted.

    A unit test, because the window cannot be waited out and the throttle takes the
    current time as an argument precisely so that it does not have to be.
    """
    throttle = LoginThrottle()
    started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for index in range(LOGIN_FAILURE_LIMIT):
        throttle.record_failure(OWNER_USERNAME, started + timedelta(seconds=index))

    assert throttle.is_throttled(OWNER_USERNAME, started + timedelta(minutes=1))
    assert not throttle.is_throttled(OWNER_USERNAME, started + LOGIN_FAILURE_WINDOW)


def test_the_counter_is_case_folded() -> None:
    """Changing the capitalisation of the username is not a way around the counter."""
    throttle = LoginThrottle()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for _ in range(LOGIN_FAILURE_LIMIT):
        throttle.record_failure(OWNER_USERNAME.upper(), now)

    assert throttle.is_throttled(OWNER_USERNAME, now)


def test_clearing_a_username_that_never_failed_is_harmless() -> None:
    """A successful first login clears a counter that was never created."""
    throttle = LoginThrottle()

    throttle.clear("nobody")

    assert not throttle.is_throttled("nobody", datetime(2026, 1, 1, 12, 0, tzinfo=UTC))


# --------------------------------------------------------------------------------------
# The total counter: what stops an attacker simply varying the username.
# --------------------------------------------------------------------------------------


def test_the_total_limit_is_the_one_the_design_names() -> None:
    """Pinned against a literal, for the reason the two constants above are."""
    assert TOTAL_FAILURE_LIMIT == SPEC_TOTAL_FAILURE_LIMIT
    # An owner fumbling one password must never be able to reach the total on their own.
    assert TOTAL_FAILURE_LIMIT > SPEC_FAILURE_LIMIT


def test_varying_the_username_trips_the_total_counter() -> None:
    """The hole the per-username counter has, and the counter that closes it.

    Keying on the submitted username means an attacker who never repeats one is never
    throttled: measured on the running application before this existed, twenty logins with
    twenty distinct usernames left every key at a single failure and every one of them paid
    for a full Argon2id verification. Fifty is where that stops.
    """
    throttle = LoginThrottle()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    for index in range(SPEC_TOTAL_FAILURE_LIMIT):
        username = f"nobody-{index}"
        assert not throttle.is_throttled(username, now), index
        throttle.record_failure(username, now)

    # Every key holds one failure, far below the per-username limit, and yet:
    assert throttle.is_throttled("nobody-fresh", now)
    assert throttle.is_throttled(OWNER_USERNAME, now)


def test_the_total_counter_slides_with_the_same_window() -> None:
    """It expires like the per-username one; a lockout is fifteen minutes, not forever."""
    throttle = LoginThrottle()
    started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for index in range(SPEC_TOTAL_FAILURE_LIMIT):
        throttle.record_failure(f"nobody-{index}", started)

    assert throttle.is_throttled(OWNER_USERNAME, started + timedelta(minutes=14, seconds=59))
    assert not throttle.is_throttled(OWNER_USERNAME, started + timedelta(minutes=15, seconds=1))


def test_a_successful_login_does_not_reset_the_total() -> None:
    """Proving you own one account says nothing about the failures naming other usernames.

    If `clear` reset the total, an attacker who knew any working credential -- or who could
    make the owner sign in -- would hold the reset button for the limit that exists
    precisely because the per-username one can be side-stepped.
    """
    throttle = LoginThrottle()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for index in range(SPEC_TOTAL_FAILURE_LIMIT):
        throttle.record_failure(f"nobody-{index}", now)

    throttle.clear(OWNER_USERNAME)

    assert throttle.is_throttled(OWNER_USERNAME, now)


def test_the_counters_stop_growing_once_the_total_is_reached() -> None:
    """The memory bound, which is the other half of why the total exists.

    Nothing caps the size of the per-username dictionary, deliberately -- an attacker who
    could overflow a cap could use the overflow to evict the one entry that matters. The
    total bounds it instead: a refused attempt is never recorded, so neither structure
    grows past the limit however many usernames are tried afterwards.
    """
    throttle = LoginThrottle()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for index in range(SPEC_TOTAL_FAILURE_LIMIT):
        throttle.record_failure(f"nobody-{index}", now)

    tracked = throttle.tracked_usernames()

    # Every one of these is refused before verification, so the service never records it,
    # and neither structure grows. Asserted rather than assumed: a limiter that kept
    # counting refused attempts would keep allocating for as long as the flood lasted.
    for index in range(SPEC_TOTAL_FAILURE_LIMIT * 4):
        assert throttle.is_throttled(f"flood-{index}", now)

    assert throttle.tracked_usernames() == tracked
    assert tracked == SPEC_TOTAL_FAILURE_LIMIT


async def test_the_total_counter_refuses_a_login_over_http(
    auth_app: FastAPI,
    auth_client: AsyncClient,
) -> None:
    """End to end: fifty failures across fifty usernames, and the next login is a 429.

    Driven through the real application rather than the class, because the property that
    matters is that the request path consults it -- a counter nothing calls is not a limit.
    """
    throttle: LoginThrottle = auth_app.state.login_throttle
    now = datetime.now(UTC)
    # The spec literal, not the constant: a neighbouring test pins the two together, so
    # looping on the live value would only make a raised limit hang the suite instead of
    # failing it.
    for index in range(SPEC_TOTAL_FAILURE_LIMIT):
        throttle.record_failure(f"nobody-{index}", now)

    response = await auth_client.post(
        LOGIN_PATH,
        json={"username": OWNER_USERNAME, "password": OWNER_PHRASE},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 429
