"""The session cookie: which one a request gets, how it is read back, and how it is removed.

One deployment is reached two ways -- over HTTPS through a tunnel, and over plain HTTP at
the host's address on the home network -- and the two need different cookies. A browser
drops a `Secure` cookie that arrives over plain HTTP from any host but `localhost`, so
the HTTPS cookie makes sign-in on the network address impossible; and `__Host-` is only
valid on a `Secure` cookie, so the name has to change with the flag.

The choice is therefore made per request, from the scheme the *browser* used, which is
what decides whether it keeps a `Secure` cookie. The `Origin` header carries exactly that
on every request that sets or clears the cookie, because all of them are POSTs. The
request URL is the fallback, and only for a client that sends no `Origin`: behind the
tunnel it reads `http` even when the browser used HTTPS.

Cookies are isolated by host, so a browser holds at most one of the two for a given
address, and the reader accepts either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

# The `__Host-` prefix is only valid on a cookie that is `Secure`, has `Path=/` and has no
# `Domain`; a browser silently drops one that arrives without them. The failure mode is a
# login that returns 204 and then does not work, with nothing in any log -- so the name is
# derived from the flag rather than written down twice.
SECURE_SESSION_COOKIE_NAME: Final = "__Host-psid"
INSECURE_SESSION_COOKIE_NAME: Final = "psid"

# `Lax` rather than `Strict`: `Strict` withholds the cookie on a top-level navigation from
# any other site, so following a bookmark from another tab would land the owner on a
# logged-out page. `Lax` still withholds it from a cross-site POST, and the JSON
# content-type rule in the middleware stops a cross-site write regardless.
COOKIE_SAME_SITE: Final[Literal["lax"]] = "lax"


def is_secure_request(request: Request) -> bool:
    """Whether the browser reached this deployment over HTTPS.

    An `Origin` of `null`, or one that does not parse to a scheme, falls back to the request
    URL like an absent one.
    """
    scheme = urlsplit(request.headers.get("origin", "")).scheme.casefold()
    if scheme not in {"http", "https"}:
        scheme = request.url.scheme
    return scheme == "https"


def session_cookie_name(*, secure: bool) -> str:
    """`__Host-psid` on HTTPS, `psid` on plain HTTP."""
    return SECURE_SESSION_COOKIE_NAME if secure else INSECURE_SESSION_COOKIE_NAME


def read_session_token(request: Request) -> str | None:
    """The session token the request carries under either name, or `None`."""
    return request.cookies.get(SECURE_SESSION_COOKIE_NAME) or request.cookies.get(
        INSECURE_SESSION_COOKIE_NAME
    )


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    """Attach the session cookie: `HttpOnly`, `SameSite=Lax`, `Path=/`, and no `Domain`.

    No `Max-Age` and no `Expires`, so it is a session cookie in the browser's sense as
    well. The server owns expiry -- it holds both the idle window and the absolute
    ceiling -- and a cookie that outlives the row it names would only ever mean a request
    that fails in a way the client cannot explain.
    """
    secure = is_secure_request(request)
    response.set_cookie(
        key=session_cookie_name(secure=secure),
        value=token,
        httponly=True,
        secure=secure,
        samesite=COOKIE_SAME_SITE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove both cookies, each with exactly the attributes it is set with, or it survives.

    Both, rather than the one this request's scheme would set: removing a cookie the
    browser does not hold costs nothing, and guessing wrong would leave a token behind.
    """
    for secure in (True, False):
        response.delete_cookie(
            key=session_cookie_name(secure=secure),
            httponly=True,
            secure=secure,
            samesite=COOKIE_SAME_SITE,
            path="/",
        )
