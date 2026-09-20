"""Serving of the built single-page application from the API process.

One container serves both the API and the frontend bundle, so there is no second origin
to configure and no CORS preflight on every request. Two rules make that work:

* any path that is not a real file falls back to `index.html`, so a client-side route
  survives a page refresh or a bookmark;
* hashed assets are immutable and cached for a year, while `index.html` is never cached,
  so a deploy takes effect on the next reload instead of stranding a client on an old
  bundle that references assets the new one no longer ships.

During local development the frontend runs on its own dev server and `dist/` does not
exist. That is expected, not an error: the mount is skipped and the API still starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.responses import Response
    from starlette.types import Scope

INDEX_FILE = "index.html"
ASSETS_PREFIX = "assets/"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_CACHE_CONTROL = "no-cache"

_logger = structlog.get_logger(__name__)


def default_dist_dir() -> Path:
    """Where the image build drops the compiled frontend bundle."""
    return Path(__file__).resolve().parent / "dist"


class SpaStaticFiles(StaticFiles):
    """Static files with an `index.html` fallback and deploy-safe cache headers."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve `path`, falling back to the SPA entry point for unknown routes."""
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            response = await super().get_response(INDEX_FILE, scope)
            path = INDEX_FILE

        # Starlette hands this method an OS-native path, so it is separated by
        # backslashes on Windows; compare on a normalised form.
        relative_path = path.replace("\\", "/")
        if relative_path.startswith(ASSETS_PREFIX):
            response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
        elif relative_path in {INDEX_FILE, "", "."}:
            response.headers["Cache-Control"] = NO_CACHE_CONTROL
        return response


def mount_spa(app: FastAPI, dist_dir: Path | None = None) -> bool:
    """Mount the built SPA at the root, or skip it when no bundle is present."""
    directory = dist_dir if dist_dir is not None else default_dist_dir()
    if not directory.is_dir():
        _logger.warning(
            "spa_bundle_missing",
            directory=str(directory),
            reason="serving the API only; expected when the frontend runs on its dev server",
        )
        return False

    app.mount("/", SpaStaticFiles(directory=directory, html=True), name="spa")
    return True
