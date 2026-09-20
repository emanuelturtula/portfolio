"""The SPA mount: client-side routes survive a refresh, and a deploy is never cached."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from portfolio.web.spa import (
    IMMUTABLE_CACHE_CONTROL,
    NO_CACHE_CONTROL,
    default_dist_dir,
    mount_spa,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

INDEX_BODY = "<!doctype html><title>portfolio</title><div id='root'></div>"
ASSET_BODY = "export const version = 1;"


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    """A directory shaped like a production frontend build."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text(INDEX_BODY, encoding="utf-8")
    (assets / "app-abc123.js").write_text(ASSET_BODY, encoding="utf-8")
    return tmp_path


@pytest.fixture
async def spa_client(dist_dir: Path) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    assert mount_spa(app, dist_dir) is True
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_index_is_served_at_the_root(spa_client: AsyncClient) -> None:
    response = await spa_client.get("/")

    assert response.status_code == 200
    assert response.text == INDEX_BODY
    assert response.headers["cache-control"] == NO_CACHE_CONTROL


async def test_unknown_path_falls_back_to_the_index(spa_client: AsyncClient) -> None:
    response = await spa_client.get("/holdings/bitcoin")

    assert response.status_code == 200
    assert response.text == INDEX_BODY
    assert response.headers["cache-control"] == NO_CACHE_CONTROL


async def test_hashed_assets_are_cached_forever(spa_client: AsyncClient) -> None:
    response = await spa_client.get("/assets/app-abc123.js")

    assert response.status_code == 200
    assert response.text == ASSET_BODY
    assert response.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


def test_missing_bundle_is_skipped_instead_of_crashing(tmp_path: Path) -> None:
    app = FastAPI()

    assert mount_spa(app, tmp_path / "does-not-exist") is False
    assert not any(getattr(route, "name", None) == "spa" for route in app.routes)


def test_default_dist_dir_sits_inside_the_package() -> None:
    assert default_dist_dir().name == "dist"
    assert default_dist_dir().parent.name == "web"
