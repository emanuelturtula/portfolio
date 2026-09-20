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

from portfolio.api.dependencies import get_principal
from portfolio.api.errors import UnauthorizedError
from portfolio.api.middleware import PUBLIC_API_PATHS, is_api_path
from tests.auth.conftest import JSON_HEADERS

if TYPE_CHECKING:
    from collections.abc import Iterable

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
