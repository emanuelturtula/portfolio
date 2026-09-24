"""Application factory.

Run with `uvicorn portfolio.main:app`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from anyio import to_thread
from fastapi import FastAPI

from portfolio import __version__
from portfolio.api.dependencies import auth_service_for, install_auth_runtime
from portfolio.api.errors import register_exception_handlers
from portfolio.api.middleware import API_PREFIX, RequestGuardMiddleware
from portfolio.api.routers import auth, balances, health, wallets
from portfolio.config import get_settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import (
    create_database_engine,
    create_session_factory,
    ensure_database_directory,
)
from portfolio.logging import configure_logging

# Imported for its side effect: each module in `providers.chains` registers its provider
# class by decorator, and a decorator only runs when its module is imported. The registry
# deliberately does not discover them -- see `providers/registry.py` -- so this is the one
# line that makes `get_chain_provider` able to answer for any chain at all.
from portfolio.providers import chains as _registered_chain_providers  # noqa: F401
from portfolio.providers.http import build_http_client
from portfolio.providers.registry import get_chain_provider
from portfolio.repositories.sync_runs import SyncRunRepository
from portfolio.services.balance_sync import build_balance_sync_service
from portfolio.services.scheduler import build_balance_scheduler
from portfolio.services.sync_coordinator import SyncCoordinator
from portfolio.web.spa import mount_spa

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    import httpx

    from portfolio.config import Settings
    from portfolio.repositories.sync_runs import SyncRunSummary, SyncTrigger
    from portfolio.services.password_hasher import PasswordHasher
    from portfolio.services.scheduler import BalanceSyncScheduler
    from portfolio.services.sync_coordinator import SyncRunner

_logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bring the database up to date, then own everything with a lifetime for the process.

    The migration runs in a worker thread because Alembic's async `env.py` calls
    `asyncio.run`, which raises `RuntimeError` when a loop is already running in the
    calling thread -- and by the time a lifespan runs, one always is.

    Four things are owned here and all four are closed here: the engine, the shared
    `httpx.AsyncClient`, the sync coordinator and the scheduler. The client is the wiring
    #6 through #9 each deferred to this issue -- it is process-wide *by construction*,
    because the per-host rate limiter's state lives on its transport, so a second one would
    silently halve the interval it claims to enforce.

    Everything is published on `app.state` rather than held in a module global so that two
    applications in one process -- which is exactly what the test suite builds -- do not
    share a pool, a client or a run in flight.

    **The orphan sweep runs before the scheduler starts and again after it stops.** A
    `sync_runs` row is written at `running` before the first provider call, so a process
    killed mid-sync leaves one behind; without the sweep a crashed run and a live run are the
    same row. Doing it at shutdown too is what records a run that outlived the grace period,
    and doing it there rather than inside the cancelled task is what makes it reliable.
    """
    settings = get_settings()
    ensure_database_directory(settings.database_url)
    await to_thread.run_sync(upgrade_to_head, settings.database_url)

    engine = create_database_engine(settings.database_url)
    app.state.db_engine = engine
    app.state.db_sessionmaker = create_session_factory(engine)
    client = build_http_client()
    app.state.http_client = client
    coordinator: SyncCoordinator | None = None
    scheduler: BalanceSyncScheduler | None = None
    try:
        await bootstrap_owner(app, settings)
        await warm_password_hasher(app)
        await sweep_interrupted_runs(app)
        coordinator = SyncCoordinator(balance_sync_runner(app, client))
        app.state.sync_coordinator = coordinator
        scheduler = start_balance_scheduler(app, settings, coordinator)
        app.state.balance_scheduler = scheduler
        if scheduler is not None:
            await scheduler.start()
        yield
    finally:
        # Ordered, and the order is the content. The scheduler stops first so that no new
        # tick can start; the run already in flight then gets its grace period; the sweep
        # records whatever did not finish; and only then are the client and the engine taken
        # away, because a sync still running would need both.
        if scheduler is not None:
            await scheduler.stop()
        if coordinator is not None:
            await coordinator.drain(
                grace_seconds=max(0, settings.balance_sync_shutdown_grace_seconds)
            )
        await sweep_interrupted_runs(app)
        await client.aclose()
        await engine.dispose()


def balance_sync_runner(app: FastAPI, client: httpx.AsyncClient) -> SyncRunner:
    """Build the closure the coordinator runs: a session per run, over the shared client.

    **A run must not share the session of whatever asked for it.** A manual sync is joined
    rather than refused, so a run outlives the request that started it, and a session closed
    by a request dependency on the way out would be pulled out from under the work. Opening
    one here means the run's unit of work begins and ends with the run.

    `provider_for` is a lambda over `get_chain_provider` rather than the registry itself,
    which is what keeps `httpx` out of `services/` while the sync is the one thing in this
    application that actually causes network traffic. An unregistered chain raises
    `UnknownChainError` out of that call, which the sync records against that chain alone.
    """

    async def run(trigger: SyncTrigger) -> SyncRunSummary:
        sessionmaker = app.state.db_sessionmaker
        async with sessionmaker() as session:
            service = build_balance_sync_service(
                session,
                provider_for=lambda chain_key: get_chain_provider(chain_key, client),
            )
            return await service.sync(trigger)

    return run


def start_balance_scheduler(
    app: FastAPI,
    settings: Settings,
    coordinator: SyncCoordinator,
) -> BalanceSyncScheduler | None:
    """Build the scheduler, or `None` when the operator has switched it off.

    `PORTFOLIO_BALANCE_SYNC_ENABLED=false` disables **the loop and nothing else**:
    `POST /api/balances/sync` still works, because the manual trigger is the tool an operator
    debugging a vendor is reaching for, and taking it away with the same switch would be the
    opposite of what the switch is for.

    Returned rather than started, so that the caller's `finally` can be written against the
    same variable it will have to stop.
    """
    if not settings.balance_sync_enabled:
        _logger.info("balance_sync_scheduler_disabled")
        return None
    return build_balance_scheduler(
        settings,
        coordinator=coordinator,
        latest_finished_at=lambda: latest_finished_run(app),
    )


async def latest_finished_run(app: FastAPI) -> datetime | None:
    """When the newest finished sync ended, over a session of its own.

    The scheduler's startup condition: a fresh deployment syncs immediately rather than
    leaving the dashboard blank for a whole interval, and a crash-looping container does not
    hit two public indexes on every restart.
    """
    sessionmaker = app.state.db_sessionmaker
    async with sessionmaker() as session:
        return await SyncRunRepository(session).latest_finished_at()


async def sweep_interrupted_runs(app: FastAPI) -> None:
    """Mark every run still at `running` as `interrupted`, and say so if there were any.

    **Never raises.** At startup a failure here would stop an otherwise healthy application
    over bookkeeping; at shutdown it would mask whatever was already going wrong and leave
    the engine undisposed. Either way the next startup sweeps again, so the cost of skipping
    one is a row that stays `running` for an interval rather than data.
    """
    try:
        sessionmaker = app.state.db_sessionmaker
        async with sessionmaker() as session:
            swept = await SyncRunRepository(session).sweep_interrupted()
            await session.commit()
    except Exception:
        _logger.exception("balance_sync_orphan_sweep_failed")
        return
    if swept:
        _logger.warning("balance_sync_runs_marked_interrupted", runs=swept)


async def warm_password_hasher(app: FastAPI) -> None:
    """Compute the dummy hash now, so that no request is the first to pay for it.

    `PasswordHasher.dummy_hash` is what a login against an unknown username verifies
    against, and it is a cached property: whoever touches it first pays to build it. Left
    to the request path, that first toucher is the first login for a username that does
    not exist -- which then costs two Argon2id operations against one for a wrong
    password, roughly 500 ms against 250 ms on the Pi. That is a clean "no such user"
    signal, once per process, in the one place the design says there must not be one.

    Here rather than in `create_app`, deliberately: the image's build-time smoke check
    calls `create_app()`, so warming there would run a 64 MiB hash during `docker build`
    and would break the invariant that building the application has no side effects. A
    lifespan has already run the migrations by this point and is the right place to spend
    a quarter of a second.

    In a worker thread for the same reason the migration is: it is a CPU-bound call, and
    the event loop is the one thing in this process that must not be blocked.
    """
    hasher: PasswordHasher = app.state.password_hasher
    await to_thread.run_sync(lambda: hasher.dummy_hash)


async def bootstrap_owner(app: FastAPI, settings: Settings) -> None:
    """Create the owner account from the bootstrap password, if there is not one already.

    This is how a fresh deployment becomes usable without an interactive shell: the
    variable is set once in the host's environment file, the first start creates the
    account, and every start after that finds one and leaves it alone. Leaving the
    variable in place therefore does not reset the password on the next deploy, which is
    the failure this would otherwise cause on every single release.

    The password itself never reaches this function as a `str`: it is a `SecretStr`
    unwrapped at the call into the service, so it cannot be carried into a log record or
    a traceback frame by accident.
    """
    if settings.bootstrap_password is None:
        return
    sessionmaker = app.state.db_sessionmaker
    async with sessionmaker() as session:
        service = auth_service_for(app, session)
        created = await service.bootstrap_user(
            settings.bootstrap_username,
            settings.bootstrap_password.get_secret_value(),
        )
    _logger.info(
        "bootstrap_user_created" if created else "bootstrap_user_already_exists",
        username=settings.bootstrap_username,
    )


def create_app() -> FastAPI:
    """Build the application: settings, logging, error rendering, routes, SPA."""
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Portfolio API",
        version=__version__,
        # Everything the API owns lives under /api, including its own documentation,
        # because the SPA mount below takes over the rest of the URL space.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
        redoc_url=None,
        # Nothing here runs until the server starts the application, so `create_app()`
        # stays a pure, side-effect-free build -- which is what the image's build-time
        # smoke check and the ASGI-transport tests rely on.
        lifespan=lifespan,
    )

    register_exception_handlers(app)
    install_auth_runtime(app, settings)

    # Middleware runs before routing, which is the whole point: the SPA is mounted at the
    # root and matches every path, so a check that ran after routing would see an API
    # request only when a route happened to exist for it.
    app.add_middleware(RequestGuardMiddleware, settings=settings)

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(wallets.router, prefix=API_PREFIX)
    # After `wallets`, and it does not matter: the balance router declares its own full
    # paths -- `/wallets/{wallet_id}/balances` among them -- and none of them collides with
    # a wallet route. Only the SPA mount below is order-sensitive.
    app.include_router(balances.router, prefix=API_PREFIX)

    # Mounted last and at the root: it matches every path, so any route registered
    # after it would be unreachable.
    mount_spa(app)

    return app


app = create_app()
