"""Criteria 3 and 4: the token is opaque and stored hashed, and both expiries hold."""

from __future__ import annotations

import string
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from portfolio.config import Settings
from portfolio.db.models import Session
from portfolio.domain.auth import LAST_SEEN_REFRESH_INTERVAL, SessionLifetime, hash_token
from tests.auth.conftest import SESSION_PATH, sign_in

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# `secrets.token_urlsafe(32)` renders 32 bytes as 43 base64url characters with no padding.
EXPECTED_TOKEN_LENGTH: Final = 43
URL_SAFE_ALPHABET: Final = frozenset(string.ascii_letters + string.digits + "-_")

# The two windows criterion 4 names, and the interval the design names, as literals.
SPEC_IDLE_DAYS: Final = 7
SPEC_ABSOLUTE_DAYS: Final = 30
SPEC_LAST_SEEN_REFRESH_INTERVAL: Final = timedelta(seconds=60)


async def read_session(factory: async_sessionmaker[AsyncSession]) -> Session:
    """The single session row, or a failure that says the assumption was wrong."""
    async with factory() as session:
        rows = list(await session.scalars(select(Session)))
    assert len(rows) == 1, rows
    return rows[0]


async def move_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    last_seen_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Rewrite a session's timestamps, so an expiry can be reached without waiting for it.

    The alternative is a test that sleeps for seven days. The settings are configurable in
    whole days precisely because no useful value can be waited out, so the clock is moved
    by moving the row rather than by faking time inside the application.
    """
    async with factory() as session:
        row = (await session.scalars(select(Session))).one()
        if last_seen_at is not None:
            row.last_seen_at = last_seen_at
        if expires_at is not None:
            row.expires_at = expires_at
        await session.commit()


async def test_issued_token_is_url_safe_and_32_bytes(auth_client: AsyncClient) -> None:
    """Criterion 3: 32 bytes from the CSPRNG, rendered url-safe so a cookie can carry it."""
    token = await sign_in(auth_client)

    assert len(token) == EXPECTED_TOKEN_LENGTH
    assert set(token) <= URL_SAFE_ALPHABET


async def test_database_never_contains_the_plaintext_token(
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    auth_environment: Path,
) -> None:
    """Criterion 3: a leaked database file yields no usable session cookie.

    Asserted twice over: the column holds the SHA-256 of the token, and the bytes of the
    database -- the write-ahead log included, which is where a recent commit actually
    lives -- contain the token nowhere.
    """
    token = await sign_in(auth_client)
    row = await read_session(sessionmaker)

    assert row.token_hash == hash_token(token)
    assert token not in row.token_hash

    encoded = token.encode("utf-8")
    for path in (auth_environment, auth_environment.with_suffix(".db-wal")):
        if path.is_file():
            assert encoded not in path.read_bytes(), path


def test_the_two_windows_are_the_ones_the_criterion_names() -> None:
    """Criterion 4's numbers, pinned against the shipped defaults rather than the live ones.

    Asserted on `model_fields` for the same reason the OWASP floor is: this suite overrides
    settings through the environment, so a check that read a live `Settings()` would be a
    check the suite could switch off.
    """
    defaults = Settings.model_fields

    assert defaults["session_idle_days"].default == SPEC_IDLE_DAYS
    assert defaults["session_absolute_days"].default == SPEC_ABSOLUTE_DAYS
    assert LAST_SEEN_REFRESH_INTERVAL == SPEC_LAST_SEEN_REFRESH_INTERVAL


async def test_the_absolute_expiry_is_thirty_days_from_creation(
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 4: the ceiling a login writes is thirty days, end to end.

    The two tests below prove the ceiling is enforced and that activity cannot move it, but
    both reach it by writing `expires_at` into the past by hand -- which they would still
    do if a login set the ceiling to one day, or to ten years. This asserts the value the
    application actually stores.
    """
    await sign_in(auth_client)
    row = await read_session(sessionmaker)

    assert row.expires_at - row.created_at == timedelta(days=SPEC_ABSOLUTE_DAYS)
    # And the ceiling is beyond the idle window, or the sliding window could never matter.
    assert row.expires_at - row.created_at > timedelta(days=SPEC_IDLE_DAYS)


async def test_session_expires_after_the_idle_window(
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 4: a session untouched for longer than the idle window is refused."""
    await sign_in(auth_client)
    assert (await auth_client.get(SESSION_PATH)).status_code == 200

    lifetime = SessionLifetime.from_days(idle_days=7, absolute_days=30)
    await move_session(
        sessionmaker,
        last_seen_at=datetime.now(UTC) - lifetime.idle - timedelta(minutes=1),
    )

    assert (await auth_client.get(SESSION_PATH)).status_code == 401
    # Refusing it also removes it, which is why this change ships no expiry sweeper.
    async with sessionmaker() as session:
        assert list(await session.scalars(select(Session))) == []


async def test_activity_slides_the_idle_window(
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 4: a request inside the window moves `last_seen_at` forward."""
    await sign_in(auth_client)
    stale = datetime.now(UTC) - timedelta(days=3)
    await move_session(sessionmaker, last_seen_at=stale)

    assert (await auth_client.get(SESSION_PATH)).status_code == 200

    row = await read_session(sessionmaker)
    assert row.last_seen_at > stale
    assert datetime.now(UTC) - row.last_seen_at < timedelta(minutes=1)


async def test_activity_cannot_push_past_the_absolute_expiry(
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 4: the ceiling is written once and nothing moves it.

    Both halves are asserted: a session that is active but past its ceiling is refused,
    and an ordinary authenticated request leaves `expires_at` exactly where it was.
    """
    await sign_in(auth_client)
    original = (await read_session(sessionmaker)).expires_at

    await move_session(sessionmaker, last_seen_at=datetime.now(UTC) - timedelta(days=2))
    assert (await auth_client.get(SESSION_PATH)).status_code == 200
    assert (await read_session(sessionmaker)).expires_at == original

    await move_session(sessionmaker, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert (await auth_client.get(SESSION_PATH)).status_code == 401


async def test_last_seen_is_not_written_on_every_request(
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A page that polls must not turn every authenticated read into a write.

    The window it is compared against is seven days long, so a minute of staleness costs
    nothing and the write amplification it avoids is real.
    """
    await sign_in(auth_client)
    before = (await read_session(sessionmaker)).last_seen_at

    for _ in range(3):
        assert (await auth_client.get(SESSION_PATH)).status_code == 200

    assert (await read_session(sessionmaker)).last_seen_at == before

    # ... and the write does happen once the row is stale enough to be worth it.
    await move_session(sessionmaker, last_seen_at=before - LAST_SEEN_REFRESH_INTERVAL * 2)
    assert (await auth_client.get(SESSION_PATH)).status_code == 200
    assert (await read_session(sessionmaker)).last_seen_at > before
