"""Criterion 12, and the two other configurations the application refuses to start on.

Refusing at settings construction rather than at first use is the point. A container that
starts and then behaves badly passes its health check, stays up, and is discovered by a
person; a container that refuses to start fails the health check, and the deployment
pipeline already knows how to roll one of those back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from portfolio.config import DEV_ALLOWED_ORIGIN, Settings, get_settings
from portfolio.db.models import User
from portfolio.main import create_app
from tests.auth.conftest import OWNER_PHRASE, OWNER_USERNAME, apply_auth_environment

if TYPE_CHECKING:
    from pathlib import Path

# Fictional, and never a real hostname: rule 3 keeps infrastructure out of the repository.
PRODUCTION_ORIGIN = "https://portfolio.example"


def build_settings(monkeypatch: pytest.MonkeyPatch, value: str) -> Settings:
    """Construct settings with a bootstrap password taken from the environment.

    Through the environment rather than as a keyword argument, because that is how the
    value actually arrives in production -- and because a string literal passed to an
    argument named `..._password` is the shape a lint rule is right to refuse.
    """
    monkeypatch.setenv("PORTFOLIO_BOOTSTRAP_PASSWORD", value)
    get_settings.cache_clear()
    return Settings()


def test_empty_bootstrap_password_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 12: an empty value is set, not absent, and it is refused."""
    with pytest.raises(ValidationError, match="PORTFOLIO_BOOTSTRAP_PASSWORD"):
        build_settings(monkeypatch, "")


def test_whitespace_bootstrap_password_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long enough to pass a length check, and still blank. Order of checks matters."""
    with pytest.raises(ValidationError, match="blank or only whitespace"):
        build_settings(monkeypatch, " " * 20)


@pytest.mark.parametrize("value", ["changeme", "CHANGEME", "letmein", "portfolio", "123456789012"])
def test_default_bootstrap_password_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """Criterion 12: the deny list is compared case-folded, so capitalising does not help."""
    with pytest.raises(ValidationError):
        build_settings(monkeypatch, value)


def test_a_short_bootstrap_password_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same policy `create-user` and the password endpoint apply, from one module."""
    with pytest.raises(ValidationError, match="12 characters"):
        build_settings(monkeypatch, "short one")


def test_an_acceptable_bootstrap_password_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusals above would be worthless if everything were refused."""
    settings = build_settings(monkeypatch, OWNER_PHRASE)

    assert settings.bootstrap_password is not None
    # Never in a repr, a traceback or a model dump: that is what `SecretStr` buys.
    assert OWNER_PHRASE not in repr(settings)


def test_no_bootstrap_password_is_a_valid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who runs `create-user` by hand sets no bootstrap variable at all."""
    monkeypatch.delenv("PORTFOLIO_BOOTSTRAP_PASSWORD", raising=False)
    get_settings.cache_clear()

    assert Settings().bootstrap_password is None


def test_prod_refuses_an_insecure_session_cookie() -> None:
    """Criterion 5's interpretation: the one configuration that loses `__Host-` is refused.

    Without `Secure` the prefix is invalid and the browser drops the cookie in silence, so
    production would sign a user in and then fail every request with nothing in any log.
    """
    with pytest.raises(ValidationError, match="PORTFOLIO_SESSION_COOKIE_SECURE"):
        Settings(
            environment="prod",
            session_cookie_secure=False,
            allowed_origin=PRODUCTION_ORIGIN,
        )


def test_prod_refuses_the_development_allowed_origin() -> None:
    """An unset origin in production rejects every write with a 403 and says why nowhere.

    The symptom -- "login works, nothing else does" -- does not name its cause, so the
    configuration is refused at startup instead of being discovered from a browser
    console.
    """
    with pytest.raises(ValidationError, match="PORTFOLIO_ALLOWED_ORIGIN"):
        Settings(environment="prod", allowed_origin=DEV_ALLOWED_ORIGIN)


def test_prod_accepts_a_deployed_origin_and_a_secure_cookie() -> None:
    """The configuration production is meant to run: both refusals stay quiet."""
    settings = Settings(environment="prod", allowed_origin=PRODUCTION_ORIGIN)

    assert settings.session_cookie_secure is True
    assert settings.session_cookie_name.startswith("__Host-")


async def test_bootstrap_creates_the_user_only_when_none_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The variable creates the account once, and is ignored forever after.

    This is what makes it safe to leave `PORTFOLIO_BOOTSTRAP_PASSWORD` in the host's
    environment file: without it, every deploy would silently reset the owner's password
    to whatever that file still says.
    """
    apply_auth_environment(monkeypatch, tmp_path)
    first = create_app()
    async with first.router.lifespan_context(first), first.state.db_sessionmaker() as session:
        created = (await session.scalars(select(User))).one()
        username, original_hash = created.username, created.password_hash

    assert username == OWNER_USERNAME

    # A second start, with a different bootstrap password. The account must not move.
    apply_auth_environment(monkeypatch, tmp_path, bootstrap="a different long phrase here")
    second = create_app()
    async with second.router.lifespan_context(second), second.state.db_sessionmaker() as session:
        rows = list(await session.scalars(select(User)))

    assert len(rows) == 1
    assert rows[0].password_hash == original_hash
