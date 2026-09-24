"""Criterion 11: every registered API route is protected, and the allowlist is explicit.

This is the test that makes "deny by default" mean something a year from now. A new router
mounted under `/api` is covered by it the moment it is registered: nobody has to remember
to add a `Depends`, and nobody has to remember to add a test.

It is not tautological. It fails when a path is added to `PUBLIC_API_PATHS` without that
being argued in a review, it fails when a new route is mounted outside `/api` where the
middleware does not look, and it fails if the middleware is ever registered after
something that could answer first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from starlette.requests import Request
from starlette.routing import Mount, WebSocketRoute

from portfolio.api.dependencies import get_principal
from portfolio.api.errors import UnauthorizedError
from portfolio.api.middleware import PUBLIC_API_PATHS, is_api_path
from portfolio.main import create_app
from portfolio.web.spa import mount_spa
from tests.auth.conftest import JSON_HEADERS

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from fastapi import FastAPI
    from httpx import AsyncClient

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})

# Documented here rather than only in the middleware, so that the diff that widens the
# allowlist has to touch a test that says what each entry is for.
EXPECTED_PUBLIC_PATHS: Final = {
    "/api/health": "the container's health check runs before anyone signs in",
    "/api/auth/login": "it is how a session comes to exist",
}

# Routes FastAPI registers itself, which therefore appear in `app.routes` but not in the
# schema it generates. They are protected like everything else under `/api`.
FRAMEWORK_DOC_ROUTES: Final = {
    ("GET", "/api/openapi.json"),
    ("GET", "/api/docs"),
    ("GET", "/api/docs/oauth2-redirect"),
}


def walk_routes(routes: Iterable[object]) -> list[tuple[str, str]]:
    """Every `(method, path)` an application serves, however its framework stores them.

    Deliberately duck-typed rather than written against a route class. FastAPI 0.141
    stopped flattening an included router into `app.routes` and now keeps a wrapper that
    resolves its children on demand, so a walk written as `isinstance(route, APIRoute)`
    silently found only the framework's own `/api/docs` and `/api/openapi.json` -- and a
    contract test that quietly stops looking at the routes it is guarding is worse than no
    contract test at all. Anything carrying a `path` and `methods` is a route; anything
    that can enumerate children is asked for them.
    """
    found: list[tuple[str, str]] = []
    for route in routes:
        children = getattr(route, "effective_candidates", None)
        if callable(children):
            found.extend(walk_routes(children()))
            continue
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not methods:
            continue  # A mount, such as the SPA bundle: static files, and no data.
        found.extend(
            (method, path) for method in sorted(str(each) for each in methods) if method != "HEAD"
        )
    return found


def api_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every `(method, path)` the application serves under `/api`."""
    return [(method, path) for method, path in walk_routes(app.routes) if is_api_path(path)]


def test_the_walk_actually_finds_the_routes(auth_app: FastAPI) -> None:
    """A walk that found nothing would pass the contract test below without checking it."""
    routes = api_routes(auth_app)

    assert ("GET", "/api/health") in routes
    assert ("POST", "/api/auth/login") in routes
    assert ("GET", "/api/auth/session") in routes
    assert ("POST", "/api/auth/password") in routes
    # The wallet registry, named here so that criterion 9 of #5 is visibly covered by the
    # walk rather than merely covered in principle. A route that stopped being registered
    # would fail here instead of silently dropping out of the 401 sweep below.
    assert ("GET", "/api/wallets") in routes
    assert ("POST", "/api/wallets") in routes
    assert ("PATCH", "/api/wallets/{wallet_id}") in routes
    assert ("DELETE", "/api/wallets/{wallet_id}") in routes
    # #10's four. None of them is in `PUBLIC_API_PATHS`, so adding them protected them --
    # which is rule 8 working and is the reason this file needed no other edit. They are
    # named here anyway, so that the 401 sweep below visibly covers the endpoints that read
    # and write the owner's balances rather than covering them by accident.
    assert ("POST", "/api/balances/sync") in routes
    assert ("GET", "/api/balances/current") in routes
    assert ("GET", "/api/balances/runs") in routes
    assert ("GET", "/api/wallets/{wallet_id}/balances") in routes
    # FastAPI's own documentation endpoints are routes like any other, and are covered.
    assert ("GET", "/api/openapi.json") in routes
    assert ("GET", "/api/docs") in routes


def test_the_walk_finds_every_route_the_openapi_schema_declares(auth_app: FastAPI) -> None:
    """The walk is duck-typed, so what it might silently miss is a route *class*.

    `test_the_walk_actually_finds_the_routes` names six routes it expects to find, which
    proves the walk is not empty but cannot notice a seventh that it failed to see -- and
    a route the walk does not return is a route `test_every_api_route_requires_a_session`
    never asks for a 401. That is the failure mode the docstring on `walk_routes` records
    having already happened once, when FastAPI 0.141 changed how an included router is
    stored.

    The OpenAPI schema is built by the framework through a different code path, so it is an
    independent witness. Equality rather than containment in both directions: a route the
    walk invents is as wrong as one it drops.
    """
    declared = {
        (method.upper(), path)
        for path, operations in auth_app.openapi()["paths"].items()
        for method in operations
    }
    walked = set(api_routes(auth_app))

    assert declared - walked == set(), "the walk missed a route the schema declares"
    assert walked == declared | FRAMEWORK_DOC_ROUTES


async def test_every_api_route_requires_a_session(
    auth_app: FastAPI,
    auth_client: AsyncClient,
) -> None:
    """Without a cookie, every API path outside the allowlist answers 401."""
    protected = [
        (method, path) for method, path in api_routes(auth_app) if path not in PUBLIC_API_PATHS
    ]
    assert protected, "the walk found no protected routes, so this proves nothing"

    unprotected: list[tuple[str, str, int]] = []
    for method, path in protected:
        # The write guard runs before the session check, so a POST carries the headers it
        # needs to get past it: this test is about authentication, not about the guard.
        headers = {} if method in SAFE_METHODS else JSON_HEADERS
        response = await auth_client.request(method, path, headers=headers, json={})
        if response.status_code != 401:
            unprotected.append((method, path, response.status_code))

    assert unprotected == []


@pytest.mark.parametrize("path", sorted(EXPECTED_PUBLIC_PATHS))
async def test_the_public_paths_really_are_reachable(
    auth_client: AsyncClient,
    path: str,
) -> None:
    """The other half: an allowlisted path must not answer 401, or login is impossible."""
    method = "GET" if path == "/api/health" else "POST"
    headers = {} if method == "GET" else JSON_HEADERS
    response = await auth_client.request(method, path, headers=headers, json={})

    assert response.status_code != 401


def test_public_allowlist_contains_only_expected_paths() -> None:
    """Pinned against a literal set: widening it is a visible line in a diff."""
    assert set(EXPECTED_PUBLIC_PATHS) == PUBLIC_API_PATHS


def test_no_route_is_mounted_outside_the_api_prefix(auth_app: FastAPI) -> None:
    """The middleware only authenticates under `/api`, so nothing else may answer data.

    Everything outside the prefix is the SPA bundle: static files, no data, and it has to
    load before a session can exist. A router registered at the root would be public
    without anyone deciding that it should be.
    """
    outside = [
        (method, path) for method, path in walk_routes(auth_app.routes) if not is_api_path(path)
    ]

    assert outside == []


def test_the_principal_dependency_refuses_a_request_the_middleware_never_saw() -> None:
    """The guard behind the guard: a protected route with no principal answers 401.

    It cannot happen through the middleware, which is the point -- it is what a route
    mounted somewhere the middleware does not look would hit. Answering 401 rather than
    raising `AttributeError` keeps that mistake a refusal instead of a 500 that leaks a
    traceback into the log.
    """
    request = Request({"type": "http", "method": "GET", "path": "/api/auth/session", "headers": []})

    with pytest.raises(UnauthorizedError):
        get_principal(request)


# --------------------------------------------------------------------------------------
# What the walk above cannot see, and what the middleware cannot cover.
# --------------------------------------------------------------------------------------


def test_the_application_registers_no_websocket_route(auth_app: FastAPI) -> None:
    """A websocket would be authenticated by nothing, and `walk_routes` cannot see it.

    Two blind spots line up exactly. `BaseHTTPMiddleware.__call__` passes any scope whose
    type is not `http` straight through to the application, so the origin check and the
    session check never run for a websocket; and `walk_routes` keeps only routes that
    carry `methods`, which a `WebSocketRoute` does not, so the contract test above would
    stay green while the connection was accepted with no cookie at all. Driven with a real
    cookieless scope by review, a handler added at `/api/live` accepted and sent data.

    Nothing here uses websockets, so the honest guardrail is to fail the build on the
    route's existence rather than to extend the middleware to a shape the application does
    not have -- untested security code being worse than an explicit "not supported".

    If this ever fails, the fix is not to delete the assertion: it is to authenticate
    websockets in pure ASGI middleware, before the route is reached, and to teach
    `walk_routes` about them.
    """
    websockets = [route for route in auth_app.routes if isinstance(route, WebSocketRoute)]

    assert websockets == [], "websockets bypass the request guard; see this test's docstring"


def test_every_mount_is_the_single_page_application(
    auth_environment: Path,
    tmp_path: Path,
) -> None:
    """A mount is the other shape `walk_routes` cannot see into.

    A sub-application mounted under `/api` would answer requests the walk never enumerated.
    The SPA is the one mount this application has, it serves static files and no data, and
    it has to be reachable before a session exists -- so it is named, and anything else
    fails.

    Built with a real bundle directory, because in the test environment `web/dist` does not
    exist and the mount is skipped: without this the assertion would pass by having nothing
    to check.
    """
    del auth_environment  # Ordering only: settings are read while the app is built.
    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "index.html").write_text("<!doctype html>", encoding="utf-8")

    app = create_app()
    before = [route for route in app.routes if isinstance(route, Mount)]
    assert mount_spa(app, bundle) is True

    mounts = [route for route in app.routes if isinstance(route, Mount)]

    # Counted rather than compared against a literal: a checkout that happens to have a
    # built bundle under `web/dist` already carries the real mount, and this test is about
    # what a mount may be, not about how many times this one was added.
    assert len(mounts) == len(before) + 1
    assert {mount.name for mount in mounts} == {"spa"}
    assert mounts[-1].path == "", "the SPA is mounted at the root, below every API route"


def test_the_shipped_application_mounts_nothing_unexpected(auth_app: FastAPI) -> None:
    """The same rule on the real application, where the bundle may or may not be present."""
    mounts = [route for route in auth_app.routes if isinstance(route, Mount)]

    assert {mount.name for mount in mounts} <= {"spa"}
