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
from portfolio.api.routers import auth, health
from portfolio.config import get_settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import (
    create_database_engine,
    create_session_factory,
    ensure_database_directory,
)
from portfolio.logging import configure_logging
from portfolio.web.spa import mount_spa

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from portfolio.config import Settings
    from portfolio.services.password_hasher import PasswordHasher

_logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bring the database up to date, then own the engine for the process's lifetime.

    The migration runs in a worker thread because Alembic's async `env.py` calls
    `asyncio.run`, which raises `RuntimeError` when a loop is already running in the
    calling thread -- and by the time a lifespan runs, one always is.

    The engine and the session factory are published on `app.state` rather than held in a
    module global so that two applications in one process (which is exactly what the test
    suite builds) do not share a pool.
    """
    settings = get_settings()
    ensure_database_directory(settings.database_url)
    await to_thread.run_sync(upgrade_to_head, settings.database_url)

    engine = create_database_engine(settings.database_url)
    app.state.db_engine = engine
    app.state.db_sessionmaker = create_session_factory(engine)
    try:
        await bootstrap_owner(app, settings)
        await warm_password_hasher(app)
        yield
    finally:
        await engine.dispose()


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

    # Mounted last and at the root: it matches every path, so any route registered
    # after it would be unreachable.
    mount_spa(app)

    return app


app = create_app()
