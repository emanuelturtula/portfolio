"""The SPA mount: client-side routes survive a refresh, a deploy is never cached, and a path
under `/api` is never answered with the bundle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from portfolio.main import create_app
from portfolio.web.spa import (
    IMMUTABLE_CACHE_CONTROL,
    NO_CACHE_CONTROL,
    default_dist_dir,
    mount_spa,
)
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from pathlib import Path

    from httpx import Response

INDEX_BODY = "<!doctype html><title>portfolio</title><div id='root'></div>"
ASSET_BODY = "export const version = 1;"
FAVICON_BODY = "<svg xmlns='http://www.w3.org/2000/svg'/>"

UNKNOWN_ENDPOINT: Final = ("GET", "/api/does-not-exist")
CLIENT_ROUTE: Final = ("GET", "/holdings/bitcoin")

# Each of these was answered by the bundle while it was mounted (#132), and each was
# answered differently by the router without it.
API_REQUESTS: Final = (
    UNKNOWN_ENDPOINT,  # was index.html; the router's 404
    ("POST", "/api/does-not-exist"),  # was the static files' 405; the router's 404
    ("GET", "/api"),  # the prefix itself is the API's, not a client-side route
    ("GET", "/api/health/"),  # was index.html; the router's trailing-slash redirect
    ("GET", "/api/auth/login"),  # was index.html; a 405 with `Allow: POST`
    ("DELETE", "/api/health"),  # was a 405 with no `Allow`; the router's carries one
)


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    """A directory shaped like a production frontend build.

    A directory of its own rather than `tmp_path` itself, which is where `api_environment`
    puts the database: a bundle directory is served file by file.
    """
    bundle = tmp_path / "dist"
    assets = bundle / "assets"
    assets.mkdir(parents=True)
    (bundle / "index.html").write_text(INDEX_BODY, encoding="utf-8")
    (assets / "app-abc123.js").write_text(ASSET_BODY, encoding="utf-8")
    (bundle / "favicon.svg").write_text(FAVICON_BODY, encoding="utf-8")
    return bundle


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


async def test_a_root_file_that_is_not_the_entry_point_gets_no_cache_header(
    spa_client: AsyncClient,
) -> None:
    """The third arm of the cache-header branch: served, but neither hashed nor the entry.

    A file sitting at the root of `dist/` is not under `assets/` so it gets no immutable
    header, and it is not `index.html` so it gets no no-cache header either -- it falls
    through with whatever Starlette set. `favicon.svg` is the real instance: Vite copies
    `public/` to the root of the bundle unhashed.

    Worth a test of its own because the coverage note in `pyproject.toml` claimed this
    branch was unreachable. It is reached by one request.
    """
    response = await spa_client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.text == FAVICON_BODY
    assert response.headers.get("cache-control") != IMMUTABLE_CACHE_CONTROL
    assert response.headers.get("cache-control") != NO_CACHE_CONTROL


async def test_only_a_missing_file_falls_back_to_the_index(spa_client: AsyncClient) -> None:
    """A write to a client-side route is refused, not answered with the entry point.

    The static files refuse any method but `GET` and `HEAD` with a 405, and the fallback
    re-raises it rather than serving `index.html`. The coverage notes in `pyproject.toml`
    counted this re-raise as a miss that no test could take back. This request reaches it.
    """
    response = await spa_client.post("/holdings/bitcoin")

    assert response.status_code == 405
    assert response.text != INDEX_BODY


@dataclass(frozen=True)
class Answer:
    """Everything about a response that a client could tell apart, bar its request id."""

    status: int
    content_type: str | None
    cache_control: str | None
    allow: str | None
    location: str | None
    body: bytes

    @classmethod
    def of(cls, response: Response) -> Answer:
        return cls(
            status=response.status_code,
            content_type=response.headers.get("content-type"),
            cache_control=response.headers.get("cache-control"),
            allow=response.headers.get("allow"),
            location=response.headers.get("location"),
            body=response.content,
        )


async def answers(
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
    requests: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], Answer]:
    """The real application's answer to each request, signed in, with `bundle` as `dist/`.

    `create_app` mounts whatever `default_dist_dir` names, so the bundle is mounted the way
    production mounts it rather than by a second `mount_spa` call after the fact. Should it
    stop asking `default_dist_dir`, the patch would mount nothing, and a comparison of two
    unbundled applications would pass for the wrong reason -- so the mount is checked here.
    """
    monkeypatch.setattr("portfolio.web.spa.default_dist_dir", lambda: bundle)
    app = create_app()
    mounted = any(getattr(route, "name", None) == "spa" for route in app.routes)
    assert mounted == await anyio.Path(bundle).is_dir()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            return {
                (method, path): Answer.of(
                    await client.request(method, path, headers=JSON_HEADERS),
                )
                for method, path in requests
            }


@pytest.mark.usefixtures("api_environment")
async def test_an_unknown_endpoint_is_the_same_404_with_the_bundle_mounted(
    monkeypatch: pytest.MonkeyPatch,
    dist_dir: Path,
    tmp_path: Path,
) -> None:
    """#132: a signed-in request for an endpoint that does not exist got `index.html` and 200.

    The client route is the control on both sides. Without it, a bundle that silently failed
    to mount would make the two applications agree for the wrong reason.
    """
    requests = (UNKNOWN_ENDPOINT, CLIENT_ROUTE)
    without = await answers(monkeypatch, tmp_path / "no-bundle", requests)
    with_bundle = await answers(monkeypatch, dist_dir, requests)

    assert without[CLIENT_ROUTE].status == 404, "the baseline must have no bundle"
    assert with_bundle[CLIENT_ROUTE].body == INDEX_BODY.encode(), "the bundle must be mounted"

    unknown = with_bundle[UNKNOWN_ENDPOINT]
    assert unknown.status == 404
    assert unknown.body != INDEX_BODY.encode()
    assert unknown == without[UNKNOWN_ENDPOINT]


@pytest.mark.usefixtures("api_environment")
async def test_every_api_path_is_answered_as_if_no_bundle_were_mounted(
    monkeypatch: pytest.MonkeyPatch,
    dist_dir: Path,
    tmp_path: Path,
) -> None:
    """The router's other answers too: a 405 with `Allow`, and the trailing-slash redirect.

    These are why the mount refuses an API path at matching rather than in its fallback: a
    route that matches the path but not the method is used only when no route matches in
    full, and a mount at the root always does.
    """
    without = await answers(monkeypatch, tmp_path / "no-bundle", API_REQUESTS)
    with_bundle = await answers(monkeypatch, dist_dir, API_REQUESTS)

    assert with_bundle == without
    assert {request: answer.status for request, answer in with_bundle.items()} == {
        ("GET", "/api/does-not-exist"): 404,
        ("POST", "/api/does-not-exist"): 404,
        ("GET", "/api"): 404,
        ("GET", "/api/health/"): 307,
        ("GET", "/api/auth/login"): 405,
        ("DELETE", "/api/health"): 405,
    }


@pytest.mark.usefixtures("api_environment")
@pytest.mark.parametrize(
    "path",
    [
        "/holdings/bitcoin",
        # Starts with the letters `api` but is not under `/api`: `is_api_path` checks the
        # separator, so this is a client-side route like any other.
        "/apiary",
    ],
)
async def test_a_client_side_route_still_falls_back_to_the_index(
    monkeypatch: pytest.MonkeyPatch,
    dist_dir: Path,
    path: str,
) -> None:
    request = ("GET", path)

    answer = (await answers(monkeypatch, dist_dir, [request]))[request]

    assert answer.status == 200
    assert answer.body == INDEX_BODY.encode()
    assert answer.cache_control == NO_CACHE_CONTROL
