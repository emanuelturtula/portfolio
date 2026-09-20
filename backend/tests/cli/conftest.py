"""Fixtures for the command-line suite.

The commands are called in process, as `cli.main(argv)`, rather than through a subprocess.
That is what lets a test assert that `getpass` was the thing that asked for the password,
which is the criterion -- a subprocess could only observe that no password appeared on the
command line, and that is the weaker half of the claim.

The database is read back through a plain synchronous engine, deliberately not the one the
command used: reading a row back through a second, unconfigured connection is a stronger
check than reading it back through the machinery that wrote it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SyncSession

from portfolio.config import get_settings
from portfolio.db.models import Session, User

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

OWNER_USERNAME: Final = "owner"

# Passphrases with spaces in them: long enough for the policy, and shaped so that no
# entropy heuristic in a secret scanner mistakes one for a credential.
OWNER_PHRASE: Final = "a correct horse battery staple"
REPLACEMENT_PHRASE: Final = "a second correct horse staple"
UNUSED_PHRASE: Final = "an entirely unused horse phrase"


@pytest.fixture
def cli_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the settings at a temporary database with cheap hash parameters."""
    database_path = tmp_path / "cli" / "portfolio.db"
    monkeypatch.setenv("PORTFOLIO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("PORTFOLIO_ARGON2_TIME_COST", "1")
    monkeypatch.setenv("PORTFOLIO_ARGON2_MEMORY_COST", "64")
    monkeypatch.setenv("PORTFOLIO_ARGON2_PARALLELISM", "1")
    monkeypatch.setenv("PORTFOLIO_BOOTSTRAP_USERNAME", OWNER_USERNAME)
    monkeypatch.delenv("PORTFOLIO_BOOTSTRAP_PASSWORD", raising=False)
    get_settings.cache_clear()
    try:
        yield database_path
    finally:
        get_settings.cache_clear()


@pytest.fixture
def sync_engine(cli_database: Path) -> Iterator[Engine]:
    """A second, ordinary connection to the same file, for asserting on what was written."""
    engine = create_engine(f"sqlite:///{cli_database.as_posix()}")
    try:
        yield engine
    finally:
        engine.dispose()


def read_users(engine: Engine) -> list[User]:
    """Every account row, as the database has them."""
    with SyncSession(engine) as session:
        return list(session.scalars(select(User)))


def read_sessions(engine: Engine) -> list[Session]:
    """Every session row, as the database has them."""
    with SyncSession(engine) as session:
        return list(session.scalars(select(Session)))


def insert_session(engine: Engine, user_id: int, token_hash: str) -> None:
    """Give an account a session, so that a cascade has something to take with it."""
    now = datetime.now(UTC)
    with SyncSession(engine) as session:
        session.add(
            Session(
                user_id=user_id,
                token_hash=token_hash,
                created_at=now,
                last_seen_at=now,
                expires_at=now,
            )
        )
        session.commit()
