"""Criteria 5, 8 and 9: the cookie, uniform failure, and revocation on logout."""

from __future__ import annotations

from http.cookies import SimpleCookie
from time import perf_counter_ns
from typing import TYPE_CHECKING, Any, Final

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from portfolio.config import (
    INSECURE_SESSION_COOKIE_NAME,
    SECURE_SESSION_COOKIE_NAME,
    get_settings,
)
from portfolio.db.models import Session, User
from portfolio.main import create_app
from portfolio.services.password_hasher import PasswordHasher
from tests.auth.conftest import (
    JSON_HEADERS,
    LOGIN_PATH,
    LOGOUT_PATH,
    OWNER_PHRASE,
    OWNER_USERNAME,
    SESSION_PATH,
    WRONG_PHRASE,
    apply_auth_environment,
    sign_in,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.services.auth import LoginThrottle

# Timing equality is asserted as a ratio with a generous bound. A strict one is a flaky
# test on a shared runner: the two paths do the same work, but the machine underneath them
# is doing other things. Anything inside this band means no usable signal; a real leak --
# skipping the hash entirely when the user is absent -- shows up as orders of magnitude.
TIMING_RATIO_LOWER_BOUND: Final = 0.2
TIMING_RATIO_UPPER_BOUND: Final = 5.0
TIMING_SAMPLES: Final = 7


def parse_set_cookie(header: str) -> SimpleCookie:
    """The `Set-Cookie` header, parsed into name, value and attributes."""
    jar = SimpleCookie()
    jar.load(header)
    return jar


async def test_cookie_carries_every_required_attribute(auth_client: AsyncClient) -> None:
    """Criterion 5: `__Host-psid`, HttpOnly, Secure, SameSite=Lax, Path=/, no Domain.

    Read off the raw header rather than the cookie jar, because the jar keeps what it
    understood and this is a test about what was actually sent.
    """
    response = await auth_client.post(
        LOGIN_PATH,
        json={"username": OWNER_USERNAME, "password": OWNER_PHRASE},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 204
    cookie = parse_set_cookie(response.headers["set-cookie"])
    morsel = cookie[SECURE_SESSION_COOKIE_NAME]
    assert morsel["httponly"] is True
    assert morsel["secure"] is True
    assert morsel["samesite"].casefold() == "lax"
    assert morsel["path"] == "/"
    # A `__Host-` cookie with a Domain is silently dropped by the browser, and so is one
    # with a Max-Age that outlives the row -- the server owns expiry.
    assert morsel["domain"] == ""
    assert morsel["max-age"] == ""
    assert morsel["expires"] == ""


async def test_cookie_name_drops_the_host_prefix_when_insecure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name is derived from `session_cookie_secure`, not written down twice.

    The `__Host-` prefix is only valid on a `Secure` cookie. Sending `__Host-psid` without
    `Secure` produces a browser that drops the cookie without a word, so the name has to
    degrade with the flag. `prod` refuses this configuration outright; it exists for a
    developer on plain HTTP who is not on `localhost`.
    """
    apply_auth_environment(monkeypatch, tmp_path, secure_cookie=False)
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            response = await client.post(
                LOGIN_PATH,
                json={"username": OWNER_USERNAME, "password": OWNER_PHRASE},
                headers=JSON_HEADERS,
            )

    assert response.status_code == 204
    cookie = parse_set_cookie(response.headers["set-cookie"])
    assert INSECURE_SESSION_COOKIE_NAME in cookie
    assert SECURE_SESSION_COOKIE_NAME not in cookie
    assert cookie[INSECURE_SESSION_COOKIE_NAME]["secure"] == ""


async def test_unknown_user_and_wrong_password_are_indistinguishable(
    auth_client: AsyncClient,
) -> None:
    """Criterion 8: one problem document for both, byte for byte."""
    unknown = await auth_client.post(
        LOGIN_PATH,
        json={"username": "nobody", "password": OWNER_PHRASE},
        headers=JSON_HEADERS,
    )
    wrong = await auth_client.post(
        LOGIN_PATH,
        json={"username": OWNER_USERNAME, "password": WRONG_PHRASE},
        headers=JSON_HEADERS,
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.headers["content-type"] == wrong.headers["content-type"]
    # Neither response may hand back a cookie, or the failure itself becomes a signal.
    assert "set-cookie" not in unknown.headers
    assert "set-cookie" not in wrong.headers

    # Byte for byte, so `content-length` cannot differ either, and every header compared
    # rather than only the content type. A `WWW-Authenticate` on one branch and not the
    # other would be an oracle as surely as a different message would.
    assert unknown.content == wrong.content
    volatile = {"date", "server"}
    assert {k: v for k, v in unknown.headers.items() if k not in volatile} == {
        k: v for k, v in wrong.headers.items() if k not in volatile
    }
    assert "www-authenticate" not in unknown.headers


async def test_both_login_failures_perform_exactly_one_password_verification(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 8, proven by counting the work instead of by timing it.

    The clock cannot carry this claim in this suite and the timing test below should not
    be read as though it does. The fixtures run Argon2id at `time_cost=1` and
    `memory_cost=64` KiB, where one verification measures about 30 microseconds against a
    login request of about 1900 -- under two per cent. Deleting the dummy-hash
    verification altogether, which is precisely the leak this criterion exists to prevent,
    moves the ratio from roughly 1.04 to roughly 1.02 and sails through a band of 0.2 to
    5.0. Raising the parameters until the hash dominated would buy a test that took a
    quarter of a second per sample and was flaky anyway.

    Counting is exact, costs nothing and cannot be flaky: both branches must call the
    hasher once, and the absent-user branch must call it against the dummy hash -- because
    "the same amount of work" is the property that makes the two paths take the same time
    on the hardware where the parameters are real.
    """
    hasher: PasswordHasher = auth_app.state.password_hasher
    throttle: LoginThrottle = auth_app.state.login_throttle
    dummy = hasher.dummy_hash  # Warmed here so the first request does not pay to build it.
    real_verify = hasher.verify
    verified_against: list[str] = []

    def counting_verify(encoded_hash: str, password: str) -> bool:
        verified_against.append(encoded_hash)
        return real_verify(encoded_hash, password)

    monkeypatch.setattr(hasher, "verify", counting_verify)

    payloads = {
        "unknown": {"username": "nobody", "password": OWNER_PHRASE},
        "wrong": {"username": OWNER_USERNAME, "password": WRONG_PHRASE},
    }
    seen: dict[str, list[str]] = {}
    for kind, payload in payloads.items():
        throttle.clear(payload["username"])
        verified_against.clear()
        response = await auth_client.post(LOGIN_PATH, json=payload, headers=JSON_HEADERS)
        assert response.status_code == 401
        seen[kind] = list(verified_against)

    assert len(seen["unknown"]) == 1, "an absent username must still cost one verification"
    assert len(seen["wrong"]) == 1, "a wrong password must cost exactly one verification"
    assert seen["unknown"] == [dummy], "the absent branch must verify against the dummy hash"
    assert seen["wrong"] != [dummy], "the present branch must verify against the stored hash"


async def test_unknown_user_and_wrong_password_take_similar_time(
    auth_app: FastAPI,
    auth_client: AsyncClient,
) -> None:
    """Criterion 8: the same work, so the clock says nothing about which usernames exist.

    The service verifies an absent user against a hash of a random password for exactly
    this reason. Without it the unknown-user path returns without hashing at all, and the
    difference is large enough to enumerate usernames over a home network.

    Two things are arranged so the measurement means something:

    * a **warm-up** attempt per branch, because the dummy hash is computed lazily on first
      use and the first unknown-user login would otherwise be the slowest sample;
    * the **throttle is cleared** before every attempt. Sixteen failures for two usernames
      would otherwise be refused before verification from the sixth onward, and a refusal
      that skips the hash is exactly the fast path this test exists to rule out.
    """
    throttle: LoginThrottle = auth_app.state.login_throttle
    payloads = {
        "unknown": {"username": "nobody", "password": OWNER_PHRASE},
        "wrong": {"username": OWNER_USERNAME, "password": WRONG_PHRASE},
    }
    timings: dict[str, list[int]] = {"unknown": [], "wrong": []}
    for kind, payload in payloads.items():
        throttle.clear(payload["username"])
        await auth_client.post(LOGIN_PATH, json=payload, headers=JSON_HEADERS)  # Warm up.
        for _ in range(TIMING_SAMPLES):
            throttle.clear(payload["username"])
            started = perf_counter_ns()
            response = await auth_client.post(LOGIN_PATH, json=payload, headers=JSON_HEADERS)
            timings[kind].append(perf_counter_ns() - started)
            assert response.status_code == 401

    medians = {kind: sorted(values)[len(values) // 2] for kind, values in timings.items()}
    ratio = medians["unknown"] / medians["wrong"]
    assert TIMING_RATIO_LOWER_BOUND < ratio < TIMING_RATIO_UPPER_BOUND, medians


async def test_logout_revokes_the_session_server_side(
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 9: the row is deleted, so replaying the cookie is worth nothing.

    Replaying it is the point. A logout that only cleared the cookie would leave a token
    that anything holding a copy -- a proxy log, a browser extension, a backup -- could
    still present.
    """
    token = await sign_in(auth_client)
    assert (await auth_client.get(SESSION_PATH)).status_code == 200

    logged_out = await auth_client.post(LOGOUT_PATH, headers=JSON_HEADERS)
    assert logged_out.status_code == 204

    async with sessionmaker() as session:
        assert list(await session.scalars(select(Session))) == []

    # The client's jar was cleared by the response, so the cookie is replayed by hand.
    auth_client.cookies.set(SECURE_SESSION_COOKIE_NAME, token)
    replayed = await auth_client.get(SESSION_PATH)
    assert replayed.status_code == 401


@pytest.mark.parametrize("value", ["", " ", "\t", "   "])
async def test_a_blank_session_cookie_is_refused(auth_client: AsyncClient, value: str) -> None:
    """A cookie that is present but carries nothing is not a session.

    `AuthService.resolve_session` is annotated `token: str` rather than `str | None`, on
    the stated grounds that the middleware refuses an empty cookie first. Nothing asserted
    that, and the two values take different routes to the same answer -- an empty string
    is falsy and stops at the middleware, while a whitespace-only one is truthy, reaches
    the service and fails to match any stored digest. Both must end in 401, and the
    narrowed annotation is only safe while the first of them does.

    Sent as a raw header rather than through the cookie jar, because a jar is entitled to
    drop a valueless cookie and this is a test about what the server does with one.
    """
    response = await auth_client.get(
        SESSION_PATH,
        headers={"Cookie": f"{SECURE_SESSION_COOKIE_NAME}={value}"},
    )

    assert response.status_code == 401


async def test_an_empty_session_cookie_is_refused_without_opening_a_database_session(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the claim: an unauthenticated caller cannot make the server work.

    The middleware answers an absent or empty cookie without reaching for the session
    factory at all, so a scan cannot cost a database connection per request. The factory
    is replaced with something that fails the test if it is called.
    """

    def refuse_to_open(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        message = "a blank cookie must be refused before a database session is opened"
        raise AssertionError(message)

    monkeypatch.setattr(auth_app.state, "db_sessionmaker", refuse_to_open)

    response = await auth_client.get(
        SESSION_PATH,
        headers={"Cookie": f"{SECURE_SESSION_COOKIE_NAME}="},
    )

    assert response.status_code == 401


async def test_login_rehashes_a_password_stored_at_a_lower_cost(
    auth_app: FastAPI,
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Raising the parameters on the Pi has to reach the account that already exists.

    The plaintext is only in hand at a successful login, so that is the one moment an
    existing hash can be upgraded. Without this, tuning would apply to new accounts only --
    and in a single-user product there are no new accounts.
    """
    async with sessionmaker() as session:
        before = await session.scalar(select(User.password_hash))

    settings = get_settings()
    auth_app.state.password_hasher = PasswordHasher(
        time_cost=settings.argon2_time_cost + 1,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )
    await sign_in(auth_client)

    async with sessionmaker() as session:
        after = await session.scalar(select(User.password_hash))

    assert before is not None
    assert after is not None
    assert after != before
    assert f"t={settings.argon2_time_cost + 1}" in after
