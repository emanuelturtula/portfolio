"""The two paths through `env.py` that the ordinary migration tests never take.

Every other test in this package hands the URL to Alembic on `Config.attributes`, and
every one of them runs online. So `resolve_database_url`'s fallback to `Settings` and
`run_migrations_offline` were both executed by nothing. Rename `Settings.database_url`
and update every caller but that one and CI stays green while `uv run alembic upgrade
head` -- the documented developer workflow -- dies with `AttributeError`.

`env.py` cannot be imported to be tested directly: its module body runs a migration. Both
paths are therefore driven through Alembic itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from portfolio.config import get_settings
from portfolio.db.alembic_config import DATABASE_URL_ATTRIBUTE, MIGRATIONS_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

APPLICATION_TABLES = frozenset({"users", "sessions", "assets", "wallets"})
"""Every table the application owns, compared **exactly** rather than with `>=`.

Under `>=` a table nobody added here satisfied every assertion below, so the list could
fall silently behind the schema. `alembic_version` is Alembic's own bookkeeping and is
added at each comparison rather than listed as though the application owned it.
"""

STAMP_TABLE = "alembic_version"

# `backend/alembic.ini`, resolved from the package rather than from the test's working
# directory, because pytest's rootdir is not necessarily `backend/`.
ALEMBIC_INI = MIGRATIONS_DIR.parents[3] / "alembic.ini"


@pytest.fixture
def settings_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the process-wide settings at a temporary file, and put them back.

    `get_settings` is `lru_cache`d and `Settings` also reads a `.env` file, so the cache
    is cleared on both sides. Without the second clear the temporary URL would stay
    cached for every test that runs afterwards in the session.
    """
    database_path = tmp_path / "from_settings.db"
    monkeypatch.setenv(
        "PORTFOLIO_DATABASE_URL",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    get_settings.cache_clear()
    try:
        yield database_path
    finally:
        get_settings.cache_clear()


def table_names_in(database_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def cli_shaped_config(script_location: Path) -> Config:
    """A `Config` carrying no URL, exactly as the Alembic CLI builds one."""
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("path_separator", "os")
    return config


def test_env_falls_back_to_settings_when_the_config_carries_no_url(
    settings_database: Path,
) -> None:
    """The developer CLI supplies no URL, so `Settings` has to be the fallback."""
    config = cli_shaped_config(MIGRATIONS_DIR)
    assert DATABASE_URL_ATTRIBUTE not in config.attributes

    command.upgrade(config, "head")

    assert settings_database.is_file()
    assert table_names_in(settings_database) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_config_attribute_wins_over_settings(
    settings_database: Path,
    database_url: str,
    database_path: Path,
) -> None:
    """The fallback must not shadow an explicit URL, or the tests migrate the wrong file."""
    config = cli_shaped_config(MIGRATIONS_DIR)
    config.attributes[DATABASE_URL_ATTRIBUTE] = database_url

    command.upgrade(config, "head")

    assert table_names_in(database_path) == APPLICATION_TABLES | {STAMP_TABLE}
    assert not settings_database.exists()


def test_env_offline_mode_emits_sql_and_writes_nothing(
    database_url: str,
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`alembic upgrade --sql` has to produce a script, not silently produce nothing."""
    command.upgrade(build_offline_config(database_url), "head", sql=True)

    emitted = capsys.readouterr().out

    assert "CREATE TABLE users" in emitted
    assert "CREATE TABLE sessions" in emitted
    assert "CREATE TABLE assets" in emitted
    assert "CREATE INDEX ix_sessions_user_id" in emitted
    # The seed migration renders too: `bulk_insert` is silently a no-op offline unless
    # the statements are emitted individually.
    assert "INSERT INTO assets" in emitted
    assert emitted.count("INSERT INTO assets") >= 1
    # A script that does not stamp the revision leaves the operator's database unmanaged.
    assert "alembic_version" in emitted
    assert not database_path.exists()


def test_env_offline_mode_renders_the_check_constraint(
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A generated script that drops a constraint is worse than no script."""
    command.upgrade(build_offline_config(database_url), "head", sql=True)

    emitted = capsys.readouterr().out

    assert "ck_assets_kind" in emitted
    assert "fk_sessions_user_id_users" in emitted


def test_the_offline_script_carries_the_safety_preamble_first(
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The artifact an operator reads is the SQL, so the warning has to be in the SQL.

    A generated script containing a batch rebuild deletes every referencing row if it is
    applied under enforcement. The instructions must therefore sit above the first
    statement, not in a docstring nobody applying the script will read.

    SQLite declares DDL non-transactional, so Alembic emits no `BEGIN` of its own here:
    the anchor is the first statement in the script rather than a `BEGIN` line.
    """
    command.upgrade(build_offline_config(database_url), "head", sql=True)

    emitted = capsys.readouterr().out

    assert "PRAGMA foreign_keys=OFF" in emitted
    assert "PRAGMA foreign_key_check" in emitted
    assert "ROLLBACK" in emitted
    # Every preamble line is a comment, and all of them precede the first statement.
    first_statement = emitted.index("CREATE TABLE alembic_version")
    preamble = emitted[:first_statement]
    assert preamble.index("-- Apply this script") < first_statement
    assert "PRAGMA foreign_keys=OFF" in preamble
    assert "PRAGMA foreign_key_check" in preamble
    assert all(line.startswith("--") for line in preamble.splitlines() if line.strip()), (
        "the preamble must be comments only, or the script will not parse"
    )


def test_the_offline_preamble_does_not_emit_the_pragma_as_a_statement(
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An emitted `PRAGMA foreign_keys=OFF` would look protective and do nothing.

    Alembic wraps the script in a transaction and the pragma is a documented no-op
    inside one, so it has to stay an instruction to the operator.
    """
    command.upgrade(build_offline_config(database_url), "head", sql=True)

    emitted = capsys.readouterr().out

    executable = [
        line
        for line in emitted.splitlines()
        if "PRAGMA" in line and not line.strip().startswith("--")
    ]
    assert executable == []


def test_the_developer_ini_drives_a_real_migration(
    database_url: str,
    sync_engine: Engine,
    restored_logging: None,
) -> None:
    """`uv run alembic upgrade head` is documented, so the ini has to actually resolve.

    This is the only test that hands Alembic a config file, which is also the only way
    `env.py`'s `fileConfig` branch ever runs.
    """
    assert ALEMBIC_INI.is_file()
    config = Config(str(ALEMBIC_INI))
    config.attributes[DATABASE_URL_ATTRIBUTE] = database_url

    command.upgrade(config, "head")

    assert set(inspect(sync_engine).get_table_names()) == APPLICATION_TABLES | {STAMP_TABLE}


def test_the_ini_carries_no_database_url() -> None:
    """Two copies of the URL means one of them is wrong, and it is the production one."""
    config = Config(str(ALEMBIC_INI))

    assert config.get_main_option("sqlalchemy.url") is None


def build_offline_config(database_url: str) -> Config:
    """The same config the online tests use; offline is selected by `sql=True`."""
    config = cli_shaped_config(MIGRATIONS_DIR)
    config.attributes[DATABASE_URL_ATTRIBUTE] = database_url
    return config
