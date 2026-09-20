"""Application factory.

Run with `uvicorn portfolio.main:app`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from anyio import to_thread
from fastapi import FastAPI

from portfolio import __version__
from portfolio.api.errors import register_exception_handlers
from portfolio.api.routers import health
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

API_PREFIX = "/api"


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
        yield
    finally:
        await engine.dispose()


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
    app.include_router(health.router, prefix=API_PREFIX)

    # Mounted last and at the root: it matches every path, so any route registered
    # after it would be unreachable.
    mount_spa(app)

    return app


app = create_app()
