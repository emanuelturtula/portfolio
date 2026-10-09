"""Spec 038, R5: `Cache-Control: no-store` on every response under `/api`.

The application is reached through Cloudflare's edge and may be opened in a browser that is
not the owner's, so nothing the API answers may be kept by a shared cache or a browser's disk
cache. What is pinned:

* **every status the application's own middleware sends under `/api`**: 200, a 401 from the
  session guard, a 403 from the origin check, a 404 and a 422, signed in or not;
* **nothing outside `/api` changes**: the single-page application's index stays `no-cache`
  and its hashed assets stay cacheable for a year, as `web/spa.py` sets them;
* **a route that sets its own header keeps it**, and anything that is not `http` passes
  through untouched;
* **where it sits**: inside `RequestContextMiddleware`, outside `RequestGuardMiddleware`, so
  the guard's refusals carry the header too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from httpx import ASGITransport, AsyncClient

from portfolio.api.cache_control import API_CACHE_CONTROL, ApiCacheControlMiddleware
from portfolio.api.middleware import RequestGuardMiddleware, is_api_path
from portfolio.api.request_context import RequestContextMiddleware
from portfolio.main import create_app
from portfolio.web.spa import IMMUTABLE_CACHE_CONTROL, NO_CACHE_CONTROL
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from starlette.types import Message, Receive, Scope, Send

OWN_CACHE_CONTROL: Final = "private, max-age=5"


def test_the_value_is_the_specs() -> None:
    assert API_CACHE_CONTROL == "no-store"


# --------------------------------------------------------------------------------------
# The real application
# --------------------------------------------------------------------------------------


async def test_every_status_under_api_is_no_store(
    signed_in_api_client: AsyncClient, api_app: FastAPI
) -> None:
    """Signed in and anonymous; the guard's refusals as much as the routes' answers."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
        responses = {
            "public": await anonymous.get("/api/health"),
            "no session": await anonymous.get("/api/wallets"),
            "wrong origin": await signed_in_api_client.post(
                "/api/wallets", json={}, headers={"Origin": "https://attacker.example"}
            ),
            "signed in": await signed_in_api_client.get("/api/wallets"),
            "unknown": await signed_in_api_client.get("/api/wallets/987654/balances"),
            "invalid": await signed_in_api_client.get("/api/wallets/not-a-number/balances"),
        }

    statuses = {name: response.status_code for name, response in responses.items()}
    assert statuses == {
        "public": 200,
        "no session": 401,
        "wrong origin": 403,
        "signed in": 200,
        "unknown": 404,
        "invalid": 422,
    }
    for name, response in responses.items():
        assert response.headers.get_list("cache-control") == [API_CACHE_CONTROL], name


@pytest.mark.usefixtures("api_environment")
async def test_the_single_page_application_keeps_its_own_caching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mounted the way production mounts it: `create_app` reads `default_dist_dir`."""
    bundle = tmp_path / "dist"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html><title>p</title>", encoding="utf-8")
    (bundle / "assets" / "app-abc123.js").write_text("export {};", encoding="utf-8")
    monkeypatch.setattr("portfolio.web.spa.default_dist_dir", lambda: bundle)

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            index = await client.get("/")
            route = await client.get("/wallets")
            asset = await client.get("/assets/app-abc123.js")
            api = await client.get("/api/wallets", headers=JSON_HEADERS)

    assert index.headers["cache-control"] == NO_CACHE_CONTROL
    assert route.headers["cache-control"] == NO_CACHE_CONTROL
    assert asset.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL
    assert api.headers["cache-control"] == API_CACHE_CONTROL


def test_it_sits_inside_the_request_context_and_outside_the_guard(app: FastAPI) -> None:
    """`user_middleware[0]` is the outermost; the guard has to be inside this one."""
    classes: list[object] = [entry.cls for entry in app.user_middleware]

    assert classes == [RequestContextMiddleware, ApiCacheControlMiddleware, RequestGuardMiddleware]
    assert app.user_middleware[1].kwargs == {"is_api_path": is_api_path}


# --------------------------------------------------------------------------------------
# The middleware on its own
# --------------------------------------------------------------------------------------


def answering(headers: list[tuple[bytes, bytes]]) -> Any:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": list(headers)})
        await send({"type": "http.response.body", "body": b""})

    return inner


async def sent(scope: Scope, headers: list[tuple[bytes, bytes]]) -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        messages.append(message)

    middleware = ApiCacheControlMiddleware(answering(headers), is_api_path=is_api_path)
    await middleware(scope, receive, send)
    return messages


def cache_control_of(messages: list[Message]) -> list[bytes]:
    [start] = [message for message in messages if message["type"] == "http.response.start"]
    return [value for name, value in start["headers"] if name.lower() == b"cache-control"]


async def test_a_route_that_sets_its_own_header_keeps_it() -> None:
    own = [(b"cache-control", OWN_CACHE_CONTROL.encode())]
    messages = await sent({"type": "http", "path": "/api/anything"}, own)

    assert cache_control_of(messages) == [OWN_CACHE_CONTROL.encode()]


async def test_a_path_outside_api_is_left_alone() -> None:
    messages = await sent({"type": "http", "path": "/apiary"}, [])

    assert cache_control_of(messages) == []


async def test_a_path_under_api_without_a_header_gets_one() -> None:
    messages = await sent({"type": "http", "path": "/api"}, [])

    assert cache_control_of(messages) == [API_CACHE_CONTROL.encode()]


async def test_anything_but_http_passes_through_untouched() -> None:
    messages = await sent({"type": "websocket", "path": "/api/anything"}, [])

    assert cache_control_of(messages) == []
