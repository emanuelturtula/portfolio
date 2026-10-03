"""A request id on every record a request writes, and one line per request (#23, spec 030).

`RequestContextMiddleware` is a **pure ASGI** middleware, added last in `create_app` so that it
is the outermost of the application's own middleware. Not a `BaseHTTPMiddleware`: one of those
runs the rest of the application in a child task, so a context variable bound below it never
reaches the layers outside it. Bound here, in the task the server runs the request in, it
reaches everything: the routes, `RequestGuardMiddleware` and its child task, and Starlette's
`ServerErrorMiddleware` outside it all, where the 500 handler runs.

For each `http` request it:

* makes a request id, `str(uuid4())`, clears the structlog context variables and binds
  `request_id` -- so every record the request writes carries it, the standard library's
  included, through `merge_contextvars`. **Hyphenated, and redacted like every other value**
  (spec 030, R5): its longest run of characters without a hyphen is 12, and the shortest
  thing the value rule redacts by pattern is 14 (a bech32 address, which the hyphen ends), so
  no id can ever look like an address or a key. `uuid4().hex`, 32 characters in one run,
  looked like a Base58 address about one time in 35;
* **ignores an inbound `X-Request-ID`**: it is never trusted, never logged and never echoed,
  because an id a client chose is an id a client can use to forge or splice log lines;
* adds `X-Request-ID` to the response it passes on. A 500 is the one response that does not
  pass through here -- `ServerErrorMiddleware` sends it from outside -- so
  `api/errors.handle_unexpected_error` sets the header itself, from `current_request_id`;
* logs `request_completed` once the response has gone, with the method, the `route`, the
  status and `duration_ms`. **Never the raw path and never the query.**

`duration_ms` is whole milliseconds, integer arithmetic on `time.perf_counter_ns()` (spec 030,
R11). Not `time.monotonic_ns()`: on Windows that is `GetTickCount64()`, which moves in steps of
15.625 ms, so every request measured 0, 15, 16 or 31. `perf_counter` is monotonic too, and
measured in steps of 100 ns there. The clock is a constructor argument, so a test can supply
one.

`route` is, the first that applies (spec 030, R9):

1. the matched route's path template, such as `/api/wallets/{wallet_id}`;
2. for one of FastAPI's documentation paths -- the OpenAPI document, the docs page and its
   OAuth2 redirect, as the application configures them -- that path, which has no parameter
   and so is its own template. Starlette sets no route for them;
3. `SPA_ROUTE` for a path outside `/api`: the single-page application and its assets, which
   the SPA's mount serves without a route either;
4. `UNMATCHED_ROUTE` for anything else under `/api`: a 404, or a request the session check
   refused before routing.

It is logged at INFO, except at DEBUG for `SPA_ROUTE` -- one page load fetches several assets
-- and for the container's health check, `GET /api/health`, every thirty seconds.

The context is **not** cleared at the end. Each request runs in a task of its own, and the 500
handler runs after this middleware has returned, still needing the id.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import structlog
from starlette.datastructures import MutableHeaders

from portfolio.logging import REQUEST_ID_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from fastapi import FastAPI
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "HEALTH_CHECK_PATH",
    "REQUEST_COMPLETED_EVENT",
    "REQUEST_ID_HEADER",
    "SPA_ROUTE",
    "UNMATCHED_ROUTE",
    "RequestContextMiddleware",
    "current_request_id",
    "documentation_paths",
]

REQUEST_ID_HEADER: Final = "X-Request-ID"
"""The response header carrying the request's id. Read from no request."""

REQUEST_COMPLETED_EVENT: Final = "request_completed"
"""The one line logged per request, replacing uvicorn's access line."""

UNMATCHED_ROUTE: Final = "unmatched"
"""`route` for a path under `/api` that no route matched, or that routing never reached."""

SPA_ROUTE: Final = "spa"
"""`route` for a path outside `/api`: the single-page application and its assets."""

HEALTH_CHECK_PATH: Final = "/api/health"
"""The container's health check, logged at DEBUG rather than INFO."""

_HEALTH_CHECK_METHOD: Final = "GET"
_STATUS_WITHOUT_A_RESPONSE: Final = 500
"""What a request that ended without starting a response is logged as: an exception escaped,
and `ServerErrorMiddleware` answers it with a 500."""

_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000

_logger = structlog.get_logger(__name__)


def current_request_id() -> str | None:
    """The id bound for the request being served, or `None` outside one."""
    value = structlog.contextvars.get_contextvars().get(REQUEST_ID_KEY)
    return value if isinstance(value, str) else None


def documentation_paths(app: FastAPI) -> frozenset[str]:
    """The documentation paths `app` serves, as it is configured to serve them.

    The OpenAPI document, the Swagger UI page, its OAuth2 redirect and the ReDoc page -- each
    only when `app` serves it, so a URL set to `None` is not here. Read from the application
    rather than copied, so that moving one moves its log label with it.
    """
    configured = (app.openapi_url, app.docs_url, app.swagger_ui_oauth2_redirect_url, app.redoc_url)
    return frozenset(path for path in configured if path)


def _template_of(scope: Scope) -> str | None:
    """The matched route's full path template, such as `/api/wallets/{wallet_id}`, or `None`.

    Read from the scope after the application has returned: the router writes what it matched
    into the dictionary this middleware passed down.

    **Why FastAPI's undocumented entry, measured on FastAPI 0.141.1 and Starlette 1.6.0** (spec
    030, R6). The documented ASGI derivation, `scope["root_path"] + route.path_format`, was
    compared with this function over every registered route. For all 25 API operations it
    gives the template without `/api` -- `/wallets/{wallet_id}`, not
    `/api/wallets/{wallet_id}` -- because `include_router(prefix=...)` no longer copies a
    route under its prefix: it keeps the route as its own router declared it, writes *that*
    route to `scope["route"]`, and leaves `root_path` at `""`. The prefixed template is only on
    the route context FastAPI keeps in `scope["fastapi"]["effective_route_context"]`, as
    `path_format`, so that is read first. It is read with `getattr` and falls back to the
    route's own template: should an upgrade move it, the line loses the prefix rather than the
    request, and never gains the raw path -- and a test fails loudly when that happens.
    """
    candidates = (
        getattr(scope.get("fastapi", {}).get("effective_route_context"), "path_format", None),
        getattr(scope.get("route"), "path_format", None),
        getattr(scope.get("route"), "path", None),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


class RequestContextMiddleware:
    """Binds a request id, returns it as `X-Request-ID`, and logs `request_completed`.

    `is_api_path` is the session guard's own test of whether a path is under `/api`, handed in
    by `create_app` rather than imported: `api/middleware.py` imports `api/errors.py`, which
    imports this module. `documentation_paths` is what `documentation_paths(app)` returns.
    `clock_ns` returns nanoseconds from a monotonic clock; `time.perf_counter_ns` unless a test
    supplies another.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        is_api_path: Callable[[str], bool],
        documentation_paths: Iterable[str] = (),
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._app = app
        self._is_api_path = is_api_path
        self._documentation_paths = frozenset(documentation_paths)
        self._clock_ns = clock_ns

    def _route_of(self, scope: Scope, path: str) -> str:
        """The `route` a request is logged under; the module docstring gives the order."""
        template = _template_of(scope)
        if template is not None:
            return template
        if path in self._documentation_paths:
            return path
        if not self._is_api_path(path):
            return SPA_ROUTE
        return UNMATCHED_ROUTE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one request with its id bound; anything but `http` passes through untouched."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = str(uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**{REQUEST_ID_KEY: request_id})
        started_ns = self._clock_ns()
        method = scope.get("method", "")
        path = scope.get("path", "")
        status = _STATUS_WITHOUT_A_RESPONSE

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self._app(scope, receive, send_with_id)
        finally:
            route = self._route_of(scope, path)
            quiet = route == SPA_ROUTE or (
                method == _HEALTH_CHECK_METHOD and path == HEALTH_CHECK_PATH
            )
            log = _logger.debug if quiet else _logger.info
            log(
                REQUEST_COMPLETED_EVENT,
                method=method,
                route=route,
                status=status,
                duration_ms=(self._clock_ns() - started_ns) // _NANOSECONDS_PER_MILLISECOND,
            )
