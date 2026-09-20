"""Security test: nothing this change handles may reach a response body or a log record.

Rule 3 in one sentence: credentials are read into `SecretStr`, are never persisted, are
never returned by any endpoint and are never logged. `tests/test_logging_redaction.py`
proves the logging processor redacts a value passed under a sensitive *key*; this file
proves the authentication paths never pass one at all -- under any key, sensitive or not.

The two secrets this change puts in flight are the submitted password and the session
token, and the redaction processor does catch both when they are logged under their own
names -- `password=` and `token=` both render as `[REDACTED]`, which was checked rather
than assumed. What it cannot catch, and is right not to try to, is a secret passed under
an innocuous key: `detail=<the session token>` reaches the log in full.

So the assertion here is the blunt one -- the literal string must appear nowhere in what
was written, under any key, sensitive or not. That is the property rule 3 states, and it
is one the redaction processor contributes to rather than guarantees.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from tests.auth.conftest import (
    JSON_HEADERS,
    LOGIN_PATH,
    LOGOUT_PATH,
    OWNER_PHRASE,
    OWNER_USERNAME,
    SESSION_PATH,
    sign_in,
)

if TYPE_CHECKING:
    import pytest
    from httpx import AsyncClient

# Invented, unique, and asserted absent from every rendered line and every response body.
# Called a phrase rather than a password because `S105` fires on the *name*, and silencing
# that rule inline -- in the one file whose whole subject is credential handling -- is a
# worse trade than choosing a name the rule has no reason to flag.
SENTINEL_PHRASE: Final = "sentinel-password-that-must-never-be-logged"


def rendered(caplog: pytest.LogCaptureFixture) -> str:
    """Everything the log pipeline produced, as one string to search.

    Both the formatted message and the record's own arguments, because a value can arrive
    as either depending on whether structlog rendered the event before stdlib saw it.
    """
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(str(getattr(record, "args", "")))
        parts.append(str(record.__dict__))
    return "\n".join(parts)


async def test_a_failed_login_leaks_the_submitted_password_nowhere(
    auth_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The refusal is logged -- the password that caused it is not.

    A failed login is the one moment a wrong password is in hand *and* something is
    written to the log, so it is the moment a careless `extra=` would leak it.
    """
    caplog.set_level(logging.DEBUG)

    response = await auth_client.post(
        LOGIN_PATH,
        json={"username": OWNER_USERNAME, "password": SENTINEL_PHRASE},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 401
    assert SENTINEL_PHRASE not in response.text
    assert SENTINEL_PHRASE not in rendered(caplog)
    assert caplog.records, "nothing was logged at all, so this would pass vacuously"


async def test_a_malformed_login_body_is_not_echoed_back(auth_client: AsyncClient) -> None:
    """A 422 from Pydantic must not quote the value it refused.

    FastAPI's default validation error includes the offending input. A password that is
    the wrong *type* is still a password, and it would travel back to the client and into
    the log with it.
    """
    response = await auth_client.post(
        LOGIN_PATH,
        json={"username": OWNER_USERNAME, "password": [SENTINEL_PHRASE]},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 422
    assert SENTINEL_PHRASE not in response.text


async def test_the_session_token_reaches_the_cookie_and_nothing_else(
    auth_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The token is a bearer credential: the `Set-Cookie` header is the only place for it.

    Not the body, not the log. The redaction processor covers a `token=` key, so a careless
    log line that names the field is caught twice over; what neither it nor any key-based
    rule can catch is the token handed to a field called something like `detail`, and that
    is the case this assertion is actually holding the line on.
    """
    caplog.set_level(logging.DEBUG)
    token = await sign_in(auth_client)

    session = await auth_client.get(SESSION_PATH)
    logged_out = await auth_client.post(LOGOUT_PATH, headers=JSON_HEADERS)

    assert session.status_code == 200
    assert logged_out.status_code == 204
    assert token not in session.text
    assert token not in rendered(caplog)
    assert caplog.records, "nothing was logged at all, so this would pass vacuously"


async def test_no_endpoint_returns_the_stored_password_hash(
    signed_in_client: AsyncClient,
) -> None:
    """`GET /api/auth/session` discloses the username and deliberately nothing else."""
    response = await signed_in_client.get(SESSION_PATH)

    assert response.status_code == 200
    assert response.json() == {"username": OWNER_USERNAME}
    assert "argon2" not in response.text
    assert OWNER_PHRASE not in response.text
