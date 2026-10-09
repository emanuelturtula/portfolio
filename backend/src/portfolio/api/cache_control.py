"""`Cache-Control: no-store` on every response under `/api` (spec 038, R5).

The application is reached through Cloudflare's edge, and may be opened in a browser that is
not the owner's. Every answer under `/api` is the owner's financial data or says something
about their session, so none of it may be kept: not by a shared cache on the way, should a
cache rule ever be widened to cover it, and not by a browser's disk cache on a machine
somebody else uses next. Until now the API sent no `Cache-Control` at all, which leaves the
decision to whoever is in between.

`ApiCacheControlMiddleware` is a **pure ASGI** middleware, for the reason
`api/request_context.py` gives, added between `RequestGuardMiddleware` and
`RequestContextMiddleware`: outside the guard, so the guard's own refusals -- the 401 for a
missing session, the 403 for a wrong origin -- carry the header as much as a route's answer
does.

It sets the header only where the response has none, so a route that ever sets its own is
taken to have meant it. A path outside `/api` is left alone: `web/spa.py` sets the single-page
application's caching, `no-cache` for its index and a year for its hashed assets, and both
are right for files that hold no data.

A 500 is the one response under `/api` that does not pass through here. Starlette's
`ServerErrorMiddleware` sends it from outside every middleware of the application's own, and
its body is a fixed problem document that carries nothing of the owner's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["API_CACHE_CONTROL", "ApiCacheControlMiddleware"]

API_CACHE_CONTROL: Final = "no-store"
"""The value every response under `/api` carries unless its route set another."""

_CACHE_CONTROL_HEADER: Final = "Cache-Control"


class ApiCacheControlMiddleware:
    """Adds `Cache-Control: no-store` to a response under `/api` that has none.

    `is_api_path` is the session guard's own test of whether a path is under `/api`, handed in
    by `create_app` as `RequestContextMiddleware`'s is, so the two never disagree about where
    the API ends.
    """

    def __init__(self, app: ASGIApp, *, is_api_path: Callable[[str], bool]) -> None:
        self._app = app
        self._is_api_path = is_api_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on; add the header to the response if it is the API's."""
        if scope["type"] != "http" or not self._is_api_path(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault(_CACHE_CONTROL_HEADER, API_CACHE_CONTROL)
            await send(message)

        await self._app(scope, receive, send_no_store)
