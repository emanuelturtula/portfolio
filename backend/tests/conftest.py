"""Fixtures shared by the whole test suite.

Two families live here. `app` and `client` build the application without starting its
lifespan, which is all an endpoint with no database behind it needs. The `api_*` family
below starts the real lifespan against a real database file and signs a caller in, which
is what any endpoint behind the session check needs -- and what two suites now need, so it
sits at the root rather than in one of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from portfolio.config import get_settings
from portfolio.main import create_app
from tests.auth.conftest import BASE_URL as SECURE_BASE_URL
from tests.auth.conftest import apply_auth_environment, sign_in
from tests.offline_http import take_offline_attempts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BASE_URL = "http://testserver"


@pytest.fixture(autouse=True)
def no_request_reached_the_offline_client() -> Iterator[None]:
    """Fail any test after which the offline HTTP client had refused a request.

    The refusal itself is an exception, and the application catches exceptions on two paths
    on purpose -- a failed scheduler tick, and a chain's internal-error clause -- so a vendor
    call made from either is refused, swallowed, and the test passes. This is what turns it
    back into a failure: `tests/offline_http.py` records the host of every refused request,
    and this checks the record is empty once everything the test built has shut down.

    Autouse, so it is set up before any fixture a test requests and torn down after all of
    them: a vendor call made during a lifespan's *shutdown* is recorded by the time this
    looks. The record is emptied on the way in as well, so one test's leftover can never be
    reported against the next.
    """
    take_offline_attempts()
    yield
    attempts = take_offline_attempts()
    assert attempts == [], (
        f"the offline HTTP client refused requests to {attempts!r} during this test, and "
        "something caught the refusal. Stub the provider or price source above the client."
    )


@pytest.fixture
def app() -> FastAPI:
    """A freshly built application, wired exactly as the server wires it."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client that talks to the app in-process, without binding a port."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as http_client:
        yield http_client


# --------------------------------------------------------------------------------------
# An authenticated caller, over a real database
# --------------------------------------------------------------------------------------
#
# Built on the helpers in `tests/auth/conftest.py` rather than beside them. The environment
# those arrange -- a file-backed database per test, deliberately cheap Argon2id parameters,
# and an HTTPS base URL so a cookie jar will actually return a `Secure` cookie -- is the
# environment every authenticated endpoint needs, and a second copy of it would drift from
# the first the moment one of those three decisions changed.
#
# Named apart from the `auth_*` fixtures rather than replacing them, so that the suites
# already written against those keep the application they were written for.


@pytest.fixture
def api_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the process-wide settings at a temporary database, and undo it afterwards."""
    database_path = apply_auth_environment(monkeypatch, tmp_path)
    try:
        yield database_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
async def api_app(api_environment: Path) -> AsyncIterator[FastAPI]:
    """The real application through its real lifespan: migrated, and with an owner."""
    del api_environment  # Ordering only: the environment has to be set before the build.
    built = create_app()
    async with built.router.lifespan_context(built):
        yield built


@pytest.fixture
def api_sessionmaker(api_app: FastAPI) -> async_sessionmaker[AsyncSession]:
    """The application's own session factory, for asserting on the rows it wrote.

    A router test that only ever reads the API back cannot tell a soft archive from a hard
    delete: both answer `204` and both make the row disappear from `GET /api/wallets`.
    Reading the table is the only way to see the difference.
    """
    factory: async_sessionmaker[AsyncSession] = api_app.state.db_sessionmaker
    return factory


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client that talks to the application in process, with a cookie jar."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url=SECURE_BASE_URL) as authenticated:
        yield authenticated


@pytest.fixture
async def signed_in_api_client(api_client: AsyncClient) -> AsyncClient:
    """A client holding a valid session cookie for the bootstrapped owner."""
    await sign_in(api_client)
    return api_client
