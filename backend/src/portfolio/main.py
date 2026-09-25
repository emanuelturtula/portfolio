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
from portfolio.providers.prices.registry import price_sources
from portfolio.providers.registry import get_chain_provider
from portfolio.repositories.prices import PriceRepository
from portfolio.repositories.sync_runs import SyncRunRepository
from portfolio.services.balance_sync import build_balance_sync_service
from portfolio.services.price_refresh import build_price_refresh_service
from portfolio.services.scheduler import IntervalScheduler
from portfolio.services.sync_coordinator import SyncCoordinator, SyncTrigger
from portfolio.web.spa import mount_spa

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    import httpx

    from portfolio.config import Settings
    from portfolio.repositories.sync_runs import SyncRunSummary
    from portfolio.services.password_hasher import PasswordHasher
    from portfolio.services.price_refresh import RefreshReport
    from portfolio.services.sync_coordinator import SyncRunner

_logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bring the database up to date, then own everything with a lifetime for the process.

    The migration runs in a worker thread because Alembic's async `env.py` calls
    `asyncio.run`, which raises `RuntimeError` when a loop is already running in the
    calling thread -- and by the time a lifespan runs, one always is.

    Five things are owned here and all five are shut down here: the engine, the shared
    `httpx.AsyncClient`, the sync coordinator, and the two timers -- the balance sync and the
    price refresh. The client is the wiring #6 through #9 each deferred to this issue; it is
    process-wide *by construction*, because the per-host rate limiter's state lives on its
    transport, so a second one would silently halve the interval it claims to enforce.

    **The two timers are separate tasks on separate intervals with separate switches**, and
    that separation is the isolation: neither can stop the other, because they share nothing
    but a class. They answer to different vendors on different schedules -- chain indexes
    that ban you for asking too often, against market-data APIs where one call covers every
    configured pair -- and one switch for both would mean an operator waiting out a chain
    outage also stopped valuing the balances they already had.

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
    schedulers: list[IntervalScheduler] = []
    try:
        await bootstrap_owner(app, settings)
        await warm_password_hasher(app)
        await sweep_interrupted_runs(app)
        coordinator = SyncCoordinator(balance_sync_runner(app, client))
        app.state.sync_coordinator = coordinator
        app.state.balance_scheduler = balance_scheduler_for(app, settings, coordinator)
        app.state.price_scheduler = price_scheduler_for(app, settings, client)
        # Two timers, two tasks, sharing nothing but a class. That is what makes "a failed
        # price refresh does not stop the balance sync" structural rather than a promise.
        schedulers = [
            timer
            for timer in (app.state.balance_scheduler, app.state.price_scheduler)
            if timer is not None
        ]
        for scheduler in schedulers:
            await scheduler.start()
        yield
    finally:
        # Ordered, and the order is the content. Both timers stop first so that no new tick
        # can start; the sync already in flight then gets its grace period; the sweep records
        # whatever did not finish; and only then are the client and the engine taken away,
        # because a sync still running would need both.
        for scheduler in reversed(schedulers):
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


def balance_scheduler_for(
    app: FastAPI,
    settings: Settings,
    coordinator: SyncCoordinator,
) -> IntervalScheduler | None:
    """Build the balance timer, or `None` when the operator has switched it off.

    `PORTFOLIO_BALANCE_SYNC_ENABLED=false` disables **the loop and nothing else**:
    `POST /api/balances/sync` still works, because the manual trigger is the tool an operator
    debugging a vendor is reaching for, and taking it away with the same switch would be the
    opposite of what the switch is for.

    The tick goes through the coordinator rather than straight to the sync, which is what
    makes criterion 6 hold from both directions: a tick arriving while a manual refresh is
    still in flight joins it instead of piling a second run on a public index.

    `at_startup` becomes the run's recorded `trigger`. That distinction is the whole reason
    `ScheduledRun` carries the flag -- `startup` is the run an operator is looking at when
    they ask whether the deploy worked.

    Returned rather than started, so that the caller's `finally` can be written against the
    same list it will have to stop.
    """
    if not settings.balance_sync_enabled:
        _logger.info("scheduler_disabled", scheduler="balance-sync")
        return None

    async def run(at_startup: bool) -> None:
        await coordinator.sync(SyncTrigger.STARTUP if at_startup else SyncTrigger.SCHEDULED)

    return IntervalScheduler(
        name="balance-sync",
        interval_minutes=settings.balance_sync_interval_minutes,
        last_run_at=lambda: latest_sync_attempt(app),
        run=run,
    )


def price_scheduler_for(
    app: FastAPI,
    settings: Settings,
    client: httpx.AsyncClient,
) -> IntervalScheduler | None:
    """Build the price timer, or `None` when the operator has switched it off.

    **This is the caller `services/price_refresh.py` was written without.** #9 shipped
    `refresh_prices()` with no caller in the running application so the call budget could be
    measured by hand first, and `docs/providers.md` recorded the scheduling as #10's. Without
    it a deployed instance reads balances every fifteen minutes and reports every one of them
    `unpriced / never_fetched` forever, which is the flagship endpoint of the dashboard
    returning a zero total on a working install.

    `price_sources` is called **here, in the composition root**, and the built sources are
    handed to the service -- the same shape `cli.run_price_refresh` uses, and what keeps
    `services/price_refresh.py` dependent on the `PriceSource` protocol rather than on which
    vendors exist. Built once rather than per tick, because which vendors exist is a
    process-wide decision like the client itself.

    Its own switch and its own interval rather than sharing the balance pair: the two answer
    to different vendors, and an operator waiting out a chain outage should not also stop
    valuing the balances they already have.
    """
    if not settings.price_refresh_enabled:
        _logger.info("scheduler_disabled", scheduler="price-refresh")
        return None
    sources = price_sources(client, settings=settings)

    async def run(at_startup: bool) -> None:
        del at_startup  # A refresh is the same work whenever it happens; nothing records it.
        sessionmaker = app.state.db_sessionmaker
        async with sessionmaker() as session:
            service = build_price_refresh_service(session, sources=sources)
            report = await service.refresh_prices()
        _report_price_refresh(report)

    return IntervalScheduler(
        name="price-refresh",
        interval_minutes=settings.price_refresh_interval_minutes,
        last_run_at=lambda: latest_price_refresh(app),
        run=run,
    )


def _report_price_refresh(report: RefreshReport) -> None:
    """Log what one refresh did. **Counts and pairs, never an amount.**

    A price is public market data and `cli.refresh-prices` prints it, because printing it is
    the point of running that command by hand. A scheduled refresh is different: nobody is
    reading its output, it happens every hour forever, and a log line carrying a number
    invites the habit of putting values in logs -- which is one careless edit away from a log
    line carrying a *quantity*, and a quantity is the owner's holdings.

    Warning when anything went unpriced, because that is the line an operator should see;
    `every_source_failed` is the one of the four reasons that means "go and look at a vendor".
    """
    unavailable = tuple(
        f"{entry.asset_symbol}/{entry.quote_currency}" for entry in report.unavailable
    )
    if unavailable:
        _logger.warning(
            "price_refresh_incomplete",
            refreshed=len(report.refreshed),
            unavailable=unavailable,
        )
        return
    _logger.info("price_refresh_finished", refreshed=len(report.refreshed))


async def latest_price_refresh(app: FastAPI) -> datetime | None:
    """When the newest price row was written, over a session of its own.

    The price timer's startup condition, and the counterpart of `latest_sync_attempt`. A
    fresh deployment prices its holdings immediately rather than showing `never_fetched` for
    an hour.

    **Unlike the balance timer, this counts successes**, because there is no record of a
    price *attempt* -- the refresh writes rows only for what it fetched, and a history of
    refresh runs was ruled out of scope. The residual is bounded and stated in
    `docs/operations.md`: while every source is failing, a crash loop costs one price request
    per restart, and the first refresh that succeeds writes rows and suppresses the next one.
    """
    sessionmaker = app.state.db_sessionmaker
    async with sessionmaker() as session:
        return await PriceRepository(session).latest_fetched_at()


async def latest_sync_attempt(app: FastAPI) -> datetime | None:
    """When the newest sync of any status started, over a session of its own.

    The balance timer's "last run", and it counts **attempts** rather than successes: a
    crash-looping container leaves `interrupted` runs with no `finished_at`, and a guard that
    only counted finished runs let every restart sync again against both public indexes. The
    property is "at most one sync per interval across restarts", which `docs/operations.md`
    states in those words.
    """
    sessionmaker = app.state.db_sessionmaker
    async with sessionmaker() as session:
        return await SyncRunRepository(session).latest_started_at()


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
