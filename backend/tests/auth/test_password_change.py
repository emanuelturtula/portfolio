"""Criterion 10: changing the password revokes every session, the caller's included."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from portfolio.db.models import Session
from portfolio.services.auth import LOGIN_FAILURE_LIMIT
from tests.auth.conftest import (
    BASE_URL,
    JSON_HEADERS,
    LOGIN_PATH,
    OWNER_PHRASE,
    OWNER_USERNAME,
    PASSWORD_PATH,
    REPLACEMENT_PHRASE,
    SESSION_PATH,
    WRONG_PHRASE,
    sign_in,
)

if TYPE_CHECKING:
    import pytest
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.services.auth import LoginThrottle


async def test_changing_the_password_revokes_every_session(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Every session, not just the caller's: that is the product's whole revocation story.

    Two browsers are simulated, because a change that only revoked the caller's own
    session would pass a single-client test while leaving the other device signed in --
    which is the exact scenario someone changes their password to end.
    """
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as other_client:
        await sign_in(auth_client)
        await sign_in(other_client)
        async with sessionmaker() as session:
            assert len(list(await session.scalars(select(Session)))) == 2

        changed = await auth_client.post(
            PASSWORD_PATH,
            json={"current_password": OWNER_PHRASE, "new_password": REPLACEMENT_PHRASE},
            headers=JSON_HEADERS,
        )

        assert changed.status_code == 204
        async with sessionmaker() as session:
            assert list(await session.scalars(select(Session))) == []
        # The second browser's cookie is still in its jar and is now worth nothing.
        assert (await other_client.get(SESSION_PATH)).status_code == 401

    # The caller's own cookie was cleared by the response, and the new password works.
    assert (await auth_client.get(SESSION_PATH)).status_code == 401
    await sign_in(auth_client, phrase=REPLACEMENT_PHRASE)
    assert (await auth_client.get(SESSION_PATH)).status_code == 200


async def test_the_old_password_stops_working(
    auth_client: AsyncClient,
) -> None:
    """The replacement is persisted, not merely accepted."""
    await sign_in(auth_client)
    await auth_client.post(
        PASSWORD_PATH,
        json={"current_password": OWNER_PHRASE, "new_password": REPLACEMENT_PHRASE},
        headers=JSON_HEADERS,
    )

    refused = await auth_client.post(
        LOGIN_PATH,
        json={"username": OWNER_USERNAME, "password": OWNER_PHRASE},
        headers=JSON_HEADERS,
    )

    assert refused.status_code == 401


async def test_wrong_current_password_is_rejected(
    signed_in_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Holding a session is not enough: the current password has to be proven.

    Without this, a borrowed browser -- or an XSS that could ride the cookie -- could set
    a new password and lock the owner out of their own instance.
    """
    response = await signed_in_client.post(
        PASSWORD_PATH,
        json={"current_password": WRONG_PHRASE, "new_password": REPLACEMENT_PHRASE},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 401
    # Nothing was revoked, so the caller is still signed in with the password they had.
    async with sessionmaker() as session:
        assert len(list(await session.scalars(select(Session)))) == 1
    assert (await signed_in_client.get(SESSION_PATH)).status_code == 200


async def test_new_password_must_meet_the_policy(signed_in_client: AsyncClient) -> None:
    """The same policy the bootstrap variable and `create-user` apply, from one module."""
    response = await signed_in_client.post(
        PASSWORD_PATH,
        json={"current_password": OWNER_PHRASE, "new_password": "short"},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == 422
    assert "12 characters" in body["detail"]
    assert (await signed_in_client.get(SESSION_PATH)).status_code == 200


async def test_a_password_change_needs_a_session(auth_client: AsyncClient) -> None:
    """It is not on the public allowlist, so an anonymous caller gets nowhere near it."""
    response = await auth_client.post(
        PASSWORD_PATH,
        json={"current_password": OWNER_PHRASE, "new_password": REPLACEMENT_PHRASE},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------------------
# The password change is throttled by the same counter as login.
# --------------------------------------------------------------------------------------


async def test_repeated_wrong_current_passwords_are_throttled(
    signed_in_client: AsyncClient,
) -> None:
    """The endpoint worth brute forcing is the one that was not counted.

    A hit here is terminal in a way a guessed login is not: this product has no password
    reset flow, so whoever changes the password owns the instance. The attacker already
    holds a session -- a borrowed browser, a script with the cookie -- and the current
    password is the only thing left in their way, so an unlimited number of guesses at it
    is the weakest point in the design.

    Five wrong guesses are answered 401 and the sixth is refused, exactly as login is,
    because it is the same counter keyed on the same username.
    """
    attempt = {"current_password": WRONG_PHRASE, "new_password": REPLACEMENT_PHRASE}
    statuses = [
        (await signed_in_client.post(PASSWORD_PATH, json=attempt, headers=JSON_HEADERS)).status_code
        for _ in range(6)
    ]

    assert statuses == [401, 401, 401, 401, 401, 429]


async def test_a_throttled_password_change_does_not_verify_the_password(
    auth_app: FastAPI,
    signed_in_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused before the hash, so the guesses cost the attacker rather than the Pi."""
    attempt = {"current_password": WRONG_PHRASE, "new_password": REPLACEMENT_PHRASE}
    for _ in range(LOGIN_FAILURE_LIMIT):
        await signed_in_client.post(PASSWORD_PATH, json=attempt, headers=JSON_HEADERS)

    def refuse_to_verify(encoded_hash: str, password: str) -> bool:
        del encoded_hash, password
        message = "a throttled password change must not reach the password hasher"
        raise AssertionError(message)

    monkeypatch.setattr(auth_app.state.password_hasher, "verify", refuse_to_verify)
    response = await signed_in_client.post(PASSWORD_PATH, json=attempt, headers=JSON_HEADERS)

    assert response.status_code == 429


async def test_failed_password_changes_also_throttle_login(
    auth_client: AsyncClient,
) -> None:
    """One counter, one username: guesses spent here are not free at the login endpoint.

    Two separate counters would let an attacker take five guesses at each, and would let
    the password endpoint be used to keep the login counter empty.
    """
    await sign_in(auth_client)
    attempt = {"current_password": WRONG_PHRASE, "new_password": REPLACEMENT_PHRASE}
    for _ in range(LOGIN_FAILURE_LIMIT):
        await auth_client.post(PASSWORD_PATH, json=attempt, headers=JSON_HEADERS)

    refused = await auth_client.post(
        LOGIN_PATH,
        json={"username": OWNER_USERNAME, "password": OWNER_PHRASE},
        headers=JSON_HEADERS,
    )

    assert refused.status_code == 429


async def test_a_rejected_new_password_is_not_counted_as_an_attempt(
    signed_in_client: AsyncClient,
) -> None:
    """A new password below policy is arithmetic, not a guess at the old one.

    Counting it would let a typo in the *new* password lock the owner out of the endpoint
    they are trying to use, and would tell an attacker nothing either way.
    """
    for _ in range(LOGIN_FAILURE_LIMIT + 1):
        response = await signed_in_client.post(
            PASSWORD_PATH,
            json={"current_password": OWNER_PHRASE, "new_password": "short"},
            headers=JSON_HEADERS,
        )
        assert response.status_code == 422

    changed = await signed_in_client.post(
        PASSWORD_PATH,
        json={"current_password": OWNER_PHRASE, "new_password": REPLACEMENT_PHRASE},
        headers=JSON_HEADERS,
    )

    assert changed.status_code == 204


async def test_a_successful_change_clears_the_counter(
    auth_app: FastAPI,
    auth_client: AsyncClient,
) -> None:
    """Knowing the current password is proof of ownership, exactly as a login is.

    The counter is read directly at the end, and that is not laziness -- it is the only
    vantage point left. The change has just revoked every session, so no HTTP route can
    add another failure without signing in first, and signing in *also* clears the
    counter, which would hide the very thing under test. Deleting the `clear` from
    `change_password` left this test green when it ended at the sign-in instead.

    The top-up is a full limit's worth for the same reason as in `test_throttling.py`:
    from zero it stays under the limit, from four it does not.
    """
    throttle: LoginThrottle = auth_app.state.login_throttle
    await sign_in(auth_client)
    attempt = {"current_password": WRONG_PHRASE, "new_password": REPLACEMENT_PHRASE}
    for _ in range(LOGIN_FAILURE_LIMIT - 1):
        await auth_client.post(PASSWORD_PATH, json=attempt, headers=JSON_HEADERS)

    changed = await auth_client.post(
        PASSWORD_PATH,
        json={"current_password": OWNER_PHRASE, "new_password": REPLACEMENT_PHRASE},
        headers=JSON_HEADERS,
    )
    assert changed.status_code == 204

    now = datetime.now(UTC)
    for _ in range(LOGIN_FAILURE_LIMIT - 1):
        throttle.record_failure(OWNER_USERNAME, now)

    assert not throttle.is_throttled(OWNER_USERNAME, now), (
        "a successful password change must reset the counter to zero"
    )
    # And the account really is usable with the new password afterwards.
    await sign_in(auth_client, phrase=REPLACEMENT_PHRASE)


async def test_failed_logins_also_throttle_the_password_change(
    auth_client: AsyncClient,
) -> None:
    """The other direction of the shared counter, asserted rather than left implied.

    `test_failed_password_changes_also_throttle_login` covers one way round. This is the
    way an attacker would actually travel it: burn the login guesses, then switch to the
    endpoint that needs a session and try there. One counter means the guesses are already
    spent -- five at the login endpoint is five in total, not five at each.
    """
    await sign_in(auth_client)
    for _ in range(LOGIN_FAILURE_LIMIT):
        await auth_client.post(
            LOGIN_PATH,
            json={"username": OWNER_USERNAME, "password": WRONG_PHRASE},
            headers=JSON_HEADERS,
        )

    refused = await auth_client.post(
        PASSWORD_PATH,
        json={"current_password": WRONG_PHRASE, "new_password": REPLACEMENT_PHRASE},
        headers=JSON_HEADERS,
    )

    assert refused.status_code == 429
