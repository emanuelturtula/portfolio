"""The two checks every request passes before a route is ever chosen.

Middleware rather than dependencies, for one reason: a dependency only runs on the routes
that remember to declare it, and "somebody forgot" is precisely the failure both of these
exist to prevent. Running before routing also means a path that matches no route is
covered, and that an endpoint added tomorrow is protected by default rather than when
someone remembers.

**Write guard.** For any method outside `GET`, `HEAD` and `OPTIONS` the request must
carry an `Origin` equal to the configured one, and a `Content-Type` of `application/json`.
The content-type rule is the one that does the work: a form-encoded POST is the shape an
HTML form on any site can send cross-origin with no preflight, so refusing anything but
JSON is what makes a cross-site write impossible rather than merely unlikely. A missing
`Origin` is refused too -- every browser sends one on a non-GET request, so its absence
identifies a non-browser client, which this API does not serve.

**Authentication.** Every path under `/api` that is not in `PUBLIC_API_PATHS` requires a
valid session. Everything outside `/api` is the static SPA bundle, which carries no data
and has to load before a session can exist.

`/api/docs` and `/api/openapi.json` are deliberately *not* public. The owner's browser
sends the cookie, so Swagger UI still works for them; an unauthenticated scan of the API
surface does not.

Both rejections are RFC 9457 problem documents, rendered through the same helper the
exception handlers use, so a client needs exactly one code path for a failure. They are
built here rather than raised: an exception raised inside a middleware unwinds past the
application's exception handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

from portfolio.api.dependencies import auth_service_for
from portfolio.api.errors import ForbiddenError, UnauthorizedError, problem_response
from portfolio.services.auth import SESSION_REQUIRED_DETAIL, SessionInvalidError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    from portfolio.api.errors import AppError
    from portfolio.config import Settings

API_PREFIX: Final = "/api"

PUBLIC_API_PATHS: Final[frozenset[str]] = frozenset({"/api/health", "/api/auth/login"})
"""The only two API paths reachable without a session.

Health, because the container's health check runs before anyone signs in, and login,
because it is how a session comes to exist. A contract test asserts this set is exactly
these two, so widening it is a visible decision in a diff rather than a quiet one.
"""

SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
JSON_MEDIA_TYPE: Final = "application/json"

ORIGIN_REJECTED_DETAIL: Final = "The request origin is missing or not allowed."
CONTENT_TYPE_REJECTED_DETAIL: Final = "Requests that change state must be JSON."

_logger = structlog.get_logger(__name__)


def media_type_of(content_type: str) -> str:
    """The media type on its own: `application/json; charset=utf-8` is JSON.

    Parameters are allowed because a browser adds `charset` unbidden, and the comparison
    is case-insensitive because RFC 9110 says the media type is.
    """
    return content_type.split(";", 1)[0].strip().casefold()


def is_api_path(path: str) -> bool:
    """Whether a path belongs to the API rather than to the SPA bundle.

    The separator is checked explicitly so that a route named `/apiary` is not mistaken
    for one under `/api`.
    """
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")


def requires_session(path: str) -> bool:
    """Whether a path is behind the session check. Deny by default, allowlist by exception.

    A path is compared literally, so `/api/health/` -- with the trailing slash that would
    normally earn a redirect -- is treated as protected. Refusing to sign in a request
    that is one character away from a public path is the right side to err on.
    """
    return is_api_path(path) and path not in PUBLIC_API_PATHS


class RequestGuardMiddleware(BaseHTTPMiddleware):
    """Origin and content-type enforcement, then deny-by-default authentication."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Reject, or authenticate and pass on."""
        rejection = self._check_write_guard(request)
        if rejection is not None:
            return rejection

        if requires_session(request.url.path):
            authenticated = await self._authenticate(request)
            if authenticated is not None:
                return authenticated

        return await call_next(request)

    def _check_write_guard(self, request: Request) -> Response | None:
        """`None` when the request may proceed, a problem document when it may not."""
        if request.method in SAFE_METHODS:
            return None

        # A missing header reads as `None`, which is never equal to the configured origin,
        # so absence and mismatch are one comparison and one answer. They are deliberately
        # not told apart: a missing `Origin` on a non-GET request identifies a non-browser
        # client, and this API serves exactly one browser.
        if request.headers.get("origin") != self._settings.allowed_origin:
            return self._refuse(request, ForbiddenError(ORIGIN_REJECTED_DETAIL), "origin")

        if media_type_of(request.headers.get("content-type", "")) != JSON_MEDIA_TYPE:
            return self._refuse(
                request,
                ForbiddenError(CONTENT_TYPE_REJECTED_DETAIL),
                "content_type",
            )
        return None

    async def _authenticate(self, request: Request) -> Response | None:
        """Attach the principal to the request, or return the 401 that replaces it."""
        token = request.cookies.get(self._settings.session_cookie_name)
        if not token:
            # Answered without opening a database session: an unauthenticated scan must
            # not be able to make the server do work.
            return self._refuse(request, UnauthorizedError(SESSION_REQUIRED_DETAIL), "no_session")

        sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
        async with sessionmaker() as session:
            service = auth_service_for(request.app, session)
            try:
                principal = await service.resolve_session(token)
            except SessionInvalidError:
                return self._refuse(
                    request,
                    UnauthorizedError(SESSION_REQUIRED_DETAIL),
                    "session_invalid",
                )
        request.state.principal = principal
        return None

    def _refuse(self, request: Request, error: AppError, reason: str) -> Response:
        """Render a rejection, and log it with the path but never the query string.

        One provider signs its requests in the query string, so a full URL is never a safe
        thing to log anywhere in this application; the habit is kept here too.
        """
        _logger.warning(
            "request_refused",
            status=error.status,
            reason=reason,
            path=request.url.path,
            method=request.method,
        )
        return problem_response(error.as_problem(instance=request.url.path))
