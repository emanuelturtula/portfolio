"""Criterion 6: every non-GET request needs a matching Origin and a JSON content type.

These run against the login endpoint, which is public, so a 403 here is unambiguously the
guard talking rather than the session check. The guard runs first for exactly that reason:
a cross-site form post should be refused before the server does anything with its body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.auth.conftest import (
    JSON_HEADERS,
    LOGIN_PATH,
    OTHER_ORIGIN,
    OWNER_PHRASE,
    OWNER_USERNAME,
)

if TYPE_CHECKING:
    from httpx import AsyncClient

CREDENTIALS = {"username": OWNER_USERNAME, "password": OWNER_PHRASE}


async def test_cross_origin_post_is_rejected(auth_client: AsyncClient) -> None:
    """An Origin that is not the configured one is refused, credentials or not."""
    response = await auth_client.post(
        LOGIN_PATH,
        json=CREDENTIALS,
        headers={"Origin": OTHER_ORIGIN, "Content-Type": "application/json"},
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "set-cookie" not in response.headers


async def test_post_without_an_origin_is_rejected(auth_client: AsyncClient) -> None:
    """A missing Origin is refused rather than waved through.

    Every browser sends one on a non-GET request, so its absence identifies a non-browser
    client -- and this API serves exactly one browser.
    """
    response = await auth_client.post(
        LOGIN_PATH,
        json=CREDENTIALS,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 403


async def test_form_encoded_post_is_rejected(auth_client: AsyncClient) -> None:
    """The rule that does the work: a form post is what an attacker's page can send.

    An HTML form can POST cross-origin with no CORS preflight, so `application/json` is
    the requirement that makes a cross-site write impossible rather than merely unlikely.
    The Origin here is the correct one, so the content type is the only thing under test.
    """
    response = await auth_client.post(
        LOGIN_PATH,
        data=CREDENTIALS,
        headers={"Origin": JSON_HEADERS["Origin"]},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "content_type",
    ["application/json; charset=utf-8", "APPLICATION/JSON", "application/json ;charset=UTF-8"],
)
async def test_json_content_type_with_charset_is_accepted(
    auth_client: AsyncClient,
    content_type: str,
) -> None:
    """Parameters are allowed and the media type is compared case-insensitively.

    A browser adds `charset` unbidden, and RFC 9110 says the media type is
    case-insensitive. Refusing either would reject a request that is perfectly correct.
    """
    response = await auth_client.post(
        LOGIN_PATH,
        content=b'{"username": "nobody", "password": "wrong but long enough"}',
        headers={"Origin": JSON_HEADERS["Origin"], "Content-Type": content_type},
    )

    # 401, not 403: the guard let it through and the credentials were simply wrong.
    assert response.status_code == 401


async def test_get_is_not_subject_to_the_origin_check(auth_client: AsyncClient) -> None:
    """A safe method is exempt: a GET from another origin cannot change anything.

    `/api/health` is public and is a GET, so this isolates the guard from the session
    check the way the POST tests isolate it from the credentials.
    """
    response = await auth_client.get("/api/health", headers={"Origin": OTHER_ORIGIN})

    assert response.status_code == 200


async def test_the_guard_covers_a_path_that_matches_no_route(auth_client: AsyncClient) -> None:
    """Running before routing is the point: a path with no route is still guarded.

    A dependency could not do this. It would never run, and the request would reach the
    SPA's catch-all mount instead.
    """
    response = await auth_client.post(
        "/api/nothing-here",
        json={},
        headers={"Origin": OTHER_ORIGIN, "Content-Type": "application/json"},
    )

    assert response.status_code == 403
