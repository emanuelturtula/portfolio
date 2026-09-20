"""Criterion 7: five failures inside fifteen minutes, and the sixth attempt is refused."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from portfolio.services.auth import LOGIN_FAILURE_LIMIT, LOGIN_FAILURE_WINDOW, LoginThrottle
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
