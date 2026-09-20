"""Building an Alembic `Config` in code, so nothing has to guess where the ini lives.

`backend/alembic.ini` exists for the developer CLI only. The application, and the tests,
run migrations against a URL they already hold, from a working directory that is not
necessarily `backend/`. Both go through here instead, and the URL travels on
`Config.attributes` rather than through `sqlalchemy.url` -- there is exactly one source of
truth for the database URL, and it is `Settings`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parent / "migrations"

# The key `env.py` reads the URL from. A `Config` built by the CLI does not carry it, and
# `env.py` falls back to `Settings` in that case.
DATABASE_URL_ATTRIBUTE: Final = "database_url"


def build_alembic_config(database_url: str) -> Config:
    """Return a `Config` pointing at the packaged migrations and the given URL."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Alembic 1.16 renamed `version_path_separator`; setting the new key keeps the
    # programmatic config free of the deprecation warning the CLI config also avoids.
    config.set_main_option("path_separator", "os")
    config.attributes[DATABASE_URL_ATTRIBUTE] = database_url
    return config


def upgrade_to_head(database_url: str) -> None:
    """Migrate a database up to the newest revision.

    Synchronous on purpose. Alembic's async `env.py` calls `asyncio.run`, which raises if
    a loop is already running in the same thread, so a caller inside the event loop must
    hop through a worker thread -- `anyio.to_thread.run_sync` -- to get here.
    """
    command.upgrade(build_alembic_config(database_url), "head")


def downgrade_to_base(database_url: str) -> None:
    """Migrate a database all the way back down, leaving no application tables."""
    command.downgrade(build_alembic_config(database_url), "base")
