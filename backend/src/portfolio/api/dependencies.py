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
from portfolio.services.accounting import (
    AccountingService,
    AccountingStatus,
    RecomputeReason,
    build_accounting_service,
)
from portfolio.services.adjustments import AdjustmentService, build_adjustment_service
from portfolio.services.auth import (
    SESSION_REQUIRED_DETAIL,
    AuthService,
    LoginThrottle,
    Principal,
    build_auth_service,
)
from portfolio.services.balances import BalanceService, build_balance_service
from portfolio.services.exchanges import ExchangeService, build_exchange_service
from portfolio.services.password_hasher import PasswordHasher
from portfolio.services.reconciliation import ReconciliationService, build_reconciliation_service
from portfolio.services.sync_coordinator import SyncCoordinator
from portfolio.services.wallets import WalletService, build_wallet_service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.config import Settings
    from portfolio.domain.exchanges import ExchangeKey
    from portfolio.services.balances import SyncRunSummary
    from portfolio.services.exchanges import ExchangeSyncRunSummary


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


async def get_balance_service(request: Request) -> AsyncIterator[BalanceService]:
    """Open a session for this request, hand the router a service, then close it.

    The same shape as `get_wallet_service`, and read-only: every method on `BalanceService`
    reads, so the session is never committed here and closing it on the way out discards
    nothing.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sessionmaker() as session:
        yield build_balance_service(session)


def get_sync_coordinator(request: Request) -> SyncCoordinator[SyncRunSummary]:
    """The process-wide sync coordinator, which the lifespan installed.

    **Not a service built per request, and that is the whole point.** A manual sync joins the
    run already in flight rather than starting a second one, so the coordinator has to
    outlive any single request -- and so does the run, which is why it opens its own session
    rather than borrowing the one a dependency would close on the way out.

    It lives on `app.state` rather than in a module global for the reason the engine does:
    the test suite routinely builds two applications in one process, and a global would make
    a sync started by one of them joinable from the other.

    Raises:
        RuntimeError: the application was built but its lifespan never ran, so there is no
            coordinator. Not reachable from a served request -- the same lifespan opens the
            database this endpoint's session comes from -- and a clear failure is better than
            an `AttributeError` two frames away from the cause.
    """
    coordinator = getattr(request.app.state, "sync_coordinator", None)
    if not isinstance(coordinator, SyncCoordinator):
        message = (
            "No sync coordinator is installed: the application's lifespan has not run. "
            "Balance sync is wired up in `portfolio.main.lifespan`."
        )
        raise RuntimeError(message)
    return coordinator


def get_exchange_sync_coordinator(request: Request) -> SyncCoordinator[ExchangeSyncRunSummary]:
    """The process-wide exchange sync coordinator, which the lifespan installed.

    A separate instance from the balance coordinator, on its own `app.state` attribute, for
    the reason `get_sync_coordinator` gives about outliving the request. It is installed
    **always**, whether or not the exchange timer is built, so a manual sync works with the
    timer switched off.

    Raises:
        RuntimeError: the lifespan never ran. Not reachable from a served request.
    """
    coordinator = getattr(request.app.state, "exchange_sync_coordinator", None)
    if not isinstance(coordinator, SyncCoordinator):
        message = (
            "No exchange sync coordinator is installed: the application's lifespan has not "
            "run. Exchange sync is wired up in `portfolio.main.lifespan`."
        )
        raise RuntimeError(message)
    return coordinator


def configured_exchanges_of(app: FastAPI) -> frozenset[ExchangeKey]:
    """The venues the lifespan found credentials for, or none if it has not run.

    **The only thing a request learns about credentials**: the keys of the provider mapping,
    published by the lifespan as `app.state.configured_exchanges`. The mapping itself, and
    the providers holding the credentials, never reach `app.state`.
    """
    configured: frozenset[ExchangeKey] = getattr(app.state, "configured_exchanges", frozenset())
    return configured


async def get_exchange_service(request: Request) -> AsyncIterator[ExchangeService]:
    """Open a session for this request and hand the router the read side of the exchanges.

    Read-only, like `get_balance_service`. `syncing` is read from the exchange coordinator
    now, as the request is served.
    """
    coordinator = get_exchange_sync_coordinator(request)
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sessionmaker() as session:
        yield build_exchange_service(
            session,
            configured=configured_exchanges_of(request.app),
            syncing=coordinator.in_flight,
        )


async def get_accounting_service(request: Request) -> AsyncIterator[AccountingService]:
    """Open a session for this request and hand the router the accounting service.

    The endpoint only reads through it -- `positions` -- so the session is never committed here
    and closing it discards nothing. The recompute, which does commit, is never run on a
    request's session: the trigger in `main.py` opens its own.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sessionmaker() as session:
        yield build_accounting_service(session)


async def get_reconciliation_service(request: Request) -> AsyncIterator[ReconciliationService]:
    """Open a session for this request and hand the router the reconciliation service.

    Read-only, like `get_balance_service`: the service compares what the syncs and the
    recompute stored, so the session is never committed here and closing it discards nothing.
    **Nothing is built from the provider mapping**: the venue balances it reads are the rows
    the exchange sync wrote, and no request path reaches a venue for them. The service's clock
    is the default one: a reading's age is measured against the time the request is served.
    """
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sessionmaker() as session:
        yield build_reconciliation_service(session)


async def get_adjustment_service(request: Request) -> AsyncIterator[AdjustmentService]:
    """Open a session for this request and hand the router the adjustment service.

    The service is built with an `after_change` that awaits the recompute trigger with
    `RecomputeReason.ADJUSTMENT`. The service commits its own write on this session first, so
    the recompute -- which opens a session of its own and takes the recompute lock -- never
    waits on this request's write.

    **The trigger is read from `app.state.accounting_recompute`**, because
    `run_accounting_recompute` lives in `main.py`, which nothing under `api` may import -- it
    imports every router (spec 023, *Triggering the recompute from a request*).
    `install_accounting_runtime` publishes it from `create_app`, so every application that
    serves a request has one, and it is read directly, as `db_sessionmaker` is. It is read per
    request, so a test can replace it.
    """
    recompute: Callable[[RecomputeReason], Awaitable[AccountingStatus]] = (
        request.app.state.accounting_recompute
    )

    async def after_change() -> AccountingStatus:
        return await recompute(RecomputeReason.ADJUSTMENT)

    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sessionmaker() as session:
        yield build_adjustment_service(session, after_change=after_change)


def get_accounting_status(request: Request) -> AccountingStatus | None:
    """The last recompute attempt the trigger recorded, or `None` before the first one.

    Read from `app.state.accounting_status`, which `create_app` installs as `None` and the
    trigger in `main.py` replaces after every attempt. `getattr` with a default so that an
    application whose state was never installed answers "no attempt yet" rather than raising.
    A value of any other type is treated the same way: the route renders what the trigger
    wrote, and nothing else.
    """
    status = getattr(request.app.state, "accounting_status", None)
    return status if isinstance(status, AccountingStatus) else None


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
