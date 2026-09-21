"""Per-request wiring: the database session, the service built on it, and the caller.

This module is why `portfolio.api.routers` can stay free of `sqlalchemy` and of every
repository without anyone having to remember the rule. A router asks for an `AuthService`
and gets one; the session it runs on is opened here, closed here, and never named in a
route signature. `import-linter` enforces the half of that which is mechanical, and this
file is the half that makes it comfortable.

The hasher, the session lifetime and the login throttle are process-wide and live on
`app.state`: the throttle because an in-process counter rebuilt per request would count
nothing, and the hasher because its dummy hash is computed once and reused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# A runtime import, deliberately. FastAPI resolves a dependency's annotations when the
# application is built, and `request: Request` is how it knows to pass the request object
# rather than to treat the parameter as a query string value. Moved into a type-checking
# block -- which is what TC002 asks for -- the name would be missing at that moment and
# the server would fail to start, while mypy stayed green.
from fastapi import Request  # noqa: TC002

from portfolio.api.errors import UnauthorizedError
from portfolio.domain.auth import SessionLifetime
from portfolio.services.auth import (
    SESSION_REQUIRED_DETAIL,
    AuthService,
    LoginThrottle,
    Principal,
    build_auth_service,
)
from portfolio.services.password_hasher import PasswordHasher
from portfolio.services.wallets import WalletService, build_wallet_service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.config import Settings


def install_auth_runtime(app: FastAPI, settings: Settings) -> None:
    """Publish the process-wide authentication objects on the application state.

    Called from the application factory rather than the lifespan so that the objects
    exist for a test that never starts the lifespan, and so that two applications in one
    process -- which the test suite builds routinely -- never share a throttle.
    """
    app.state.password_hasher = PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )
    app.state.session_lifetime = SessionLifetime.from_days(
        idle_days=settings.session_idle_days,
        absolute_days=settings.session_absolute_days,
    )
    app.state.login_throttle = LoginThrottle()


def auth_service_for(app: FastAPI, session: AsyncSession) -> AuthService:
    """Build the service over an already-open session, from the application's state.

    Takes the application rather than the request because three callers need it and only
    one of them has a request: the route dependency, the middleware -- which runs before
    dependency injection exists -- and the lifespan, which bootstraps the owner account
    before the server accepts anything at all.
    """
    hasher: PasswordHasher = app.state.password_hasher
    lifetime: SessionLifetime = app.state.session_lifetime
    throttle: LoginThrottle = app.state.login_throttle
    return build_auth_service(session, hasher=hasher, lifetime=lifetime, throttle=throttle)


async def get_auth_service(request: Request) -> AsyncIterator[AuthService]:
    """Open a session for this request, hand the router a service, then close it.

    The session is closed on the way out whatever happened, so an exception leaves
    uncommitted work rolled back rather than half applied.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sessionmaker() as session:
        yield auth_service_for(request.app, session)


async def get_wallet_service(request: Request) -> AsyncIterator[WalletService]:
    """Open a session for this request, hand the router a service, then close it.

    The same shape as `get_auth_service`, and for the same reason: the router asks for a
    `WalletService` and never names a session, which is what keeps `sqlalchemy` out of
    `api.routers` without anyone having to remember the rule.

    Nothing process-wide is needed here -- no hasher, no throttle -- so the service is
    built from the session alone rather than from `app.state`.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sessionmaker() as session:
        yield build_wallet_service(session)


def get_principal(request: Request) -> Principal:
    """The authenticated caller, as resolved by the middleware before routing.

    The middleware has already rejected every unauthenticated request to a non-public
    path, so this reads what it left behind rather than resolving the session a second
    time. The guard is for the case that is supposed to be impossible: a route that the
    middleware did not cover. It answers 401 rather than raising `AttributeError` and
    becoming a 500, because a missing principal is exactly "you are not signed in".
    """
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise UnauthorizedError(SESSION_REQUIRED_DETAIL)
    return principal
