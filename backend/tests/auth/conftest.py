"""Fixtures for the authentication suite.

Three things are arranged here and nowhere else:

* **A real database file per test**, migrated by the application's own lifespan, because
  the thing under test is a request path that reads and writes rows. `:memory:` is never
  used, for the reasons `tests/db/conftest.py` sets out.
* **Deliberately cheap Argon2id parameters.** The shipped defaults cost roughly a quarter
  of a second per hash by design, and this suite hashes on nearly every test. The floor
  that matters is asserted against the *defaults* in `test_password_hasher.py`, which is
  immune to these overrides -- if it were asserted against the live settings, this file
  would be able to turn that check off.
* **An HTTPS base URL.** The session cookie is `Secure` in every configuration this
  product ships, and a cookie jar will not return a `Secure` cookie over `http://`. A test
  suite that quietly set `session_cookie_secure=False` to get around that would be testing
  a cookie the application never sends.

`get_settings` is `lru_cache`d, so the cache is cleared on both sides of the environment
override. Skipping the second clear would leave a temporary database URL cached for every
test that runs afterwards in the session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from httpx import ASGITransport, AsyncClient

from portfolio.config import get_settings
from portfolio.main import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

OWNER_USERNAME: Final = "owner"

# Passphrases, not passwords: long enough to pass the policy, and with spaces in them so
# that no entropy heuristic in a secret scanner mistakes one for a credential.
OWNER_PHRASE: Final = "a correct horse battery staple"
WRONG_PHRASE: Final = "not the horse you are looking for"
REPLACEMENT_PHRASE: Final = "a second correct horse staple"

# Fictional, and never a real hostname (rule 3). HTTPS because the cookie is `Secure`.
BASE_URL: Final = "https://testserver"
OTHER_ORIGIN: Final = "https://attacker.example"

JSON_HEADERS: Final[dict[str, str]] = {
    "Origin": BASE_URL,
    "Content-Type": "application/json",
}

LOGIN_PATH: Final = "/api/auth/login"
LOGOUT_PATH: Final = "/api/auth/logout"
SESSION_PATH: Final = "/api/auth/session"

# and a constant called `..._PATH` holding "/api/auth/password" is exactly the false
# positive it cannot distinguish -- which is a good trade for the times it is right.
PASSWORD_PATH: Final = "/api/auth/password"  # noqa: S105


def apply_auth_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    secure_cookie: bool = True,
    bootstrap: str | None = OWNER_PHRASE,
) -> Path:
    """Point the process-wide settings at a temporary database and cheap hash parameters."""
    database_path = tmp_path / "auth" / "portfolio.db"
    monkeypatch.setenv("PORTFOLIO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("PORTFOLIO_ALLOWED_ORIGIN", BASE_URL)
    monkeypatch.setenv("PORTFOLIO_SESSION_COOKIE_SECURE", "true" if secure_cookie else "false")
    monkeypatch.setenv("PORTFOLIO_ARGON2_TIME_COST", "1")
    monkeypatch.setenv("PORTFOLIO_ARGON2_MEMORY_COST", "64")
    monkeypatch.setenv("PORTFOLIO_ARGON2_PARALLELISM", "1")
    if bootstrap is None:
        monkeypatch.delenv("PORTFOLIO_BOOTSTRAP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("PORTFOLIO_BOOTSTRAP_PASSWORD", bootstrap)
    monkeypatch.setenv("PORTFOLIO_BOOTSTRAP_USERNAME", OWNER_USERNAME)
    get_settings.cache_clear()
    return database_path


@pytest.fixture
def auth_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The environment a signed-in test runs in, undone afterwards."""
    database_path = apply_auth_environment(monkeypatch, tmp_path)
    try:
        yield database_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
async def auth_app(auth_environment: Path) -> AsyncIterator[FastAPI]:
    """The real application, through its real lifespan: migrated, and with an owner."""
    del auth_environment  # Ordering only: the environment has to be set before the build.
    app = create_app()
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def sessionmaker(auth_app: FastAPI) -> async_sessionmaker[AsyncSession]:
    """The application's own session factory, for asserting on rows it wrote."""
    factory: async_sessionmaker[AsyncSession] = auth_app.state.db_sessionmaker
    return factory


@pytest.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client that talks to the application in process, with a cookie jar."""
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


async def sign_in(
    client: AsyncClient,
    username: str = OWNER_USERNAME,
    phrase: str = OWNER_PHRASE,
) -> str:
    """Sign in and return the session token the server set, failing loudly if it did not."""
    response = await client.post(
        LOGIN_PATH,
        json={"username": username, "password": phrase},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 204, response.text
    token = next(iter(client.cookies.values()))
    return token


@pytest.fixture
async def signed_in_client(auth_client: AsyncClient) -> AsyncClient:
    """A client holding a valid session cookie for the bootstrapped owner."""
    await sign_in(auth_client)
    return auth_client
