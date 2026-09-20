"""Application settings, loaded from the environment.

Every value is read from a `PORTFOLIO_`-prefixed environment variable so that the same
image can run in development and in production without a rebuild. Nothing in this module
carries a default that would be unsafe if it survived into production.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the backend."""

    model_config = SettingsConfigDict(
        env_prefix="PORTFOLIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./data/portfolio.db"
    allowed_origin: str = "http://localhost:5173"
    session_cookie_secure: bool = True

    # Credentials are never plain `str`. `SecretStr` keeps the value out of reprs,
    # tracebacks and model dumps, which is what stops an exchange key from reaching the
    # logs by accident; `logging.py` is the second line of defence, not the first:
    #
    # exchange_api_secret: SecretStr | None = None  # noqa: ERA001


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed from the environment exactly once."""
    return Settings()
