"""Criterion 10: changing the password revokes every session, the caller's included."""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from portfolio.db.models import Session
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
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
