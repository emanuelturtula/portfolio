"""Application factory.

Run with `uvicorn portfolio.main:app`.
"""

from __future__ import annotations

from fastapi import FastAPI

from portfolio import __version__
from portfolio.api.errors import register_exception_handlers
from portfolio.api.routers import health
from portfolio.config import get_settings
from portfolio.logging import configure_logging
from portfolio.web.spa import mount_spa

API_PREFIX = "/api"


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
    )

    register_exception_handlers(app)
    app.include_router(health.router, prefix=API_PREFIX)

    # Mounted last and at the root: it matches every path, so any route registered
    # after it would be unreachable.
    mount_spa(app)

    return app


app = create_app()
