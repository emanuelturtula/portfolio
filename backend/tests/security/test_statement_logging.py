"""#106: at DEBUG, the database driver writes no row's values to any log line.

`aiosqlite` logs every call it makes on a connection at DEBUG, and for a statement that call
is `functools.partial(cursor.execute, sql, parameters)` -- so the record is the SQL text and
the tuple of values. `hide_parameters=True` on the engine does not reach it, because the
driver logs underneath SQLAlchemy through a logger of its own. With `PORTFOLIO_LOG_LEVEL=DEBUG`
every INSERT and UPDATE the application wrote went to stdout, wallet addresses included.

At INFO, the production default, nothing leaked, which is why nothing saw it: the one secrets
test that already ran at DEBUG searched for credentials, and credentials are never written to
the database. So this module runs at DEBUG and searches for what *is* written.

## What is driven, and what is searched

The **real application** through its real lifespan, twice over:

* a wallet registered and then renamed over HTTP, which is an INSERT and an UPDATE on
  `wallets` through the router, the service, the repository and the application's own engine.
  The values are a testnet address (rule 3) and two labels built from short cycles, so no
  ordinary output contains them;
* a start from an empty database and a sign-in, which writes the owner's Argon2 password hash
  and a session's token hash. Nobody named those rows when #106 was filed; lifting the floor
  while fixing it showed both on stdout.

Searched are the bytes the production JSON pipeline wrote to stdout and every standard-library
record, rendered in full by a root handler of the test's own. Both, because the root handler's
`%(message)s` format decides what of a record reaches stdout, and a value a future format
would print is caught by the second today.

## Why each absence means something

* **The positive companion.** A DEBUG line is written through the same pipeline at the same
  moment and has to reach both captures, so neither can pass by being empty or by running
  above DEBUG.
* **The rows are read back.** The address and both labels really went through the engine.
* **The control.** The floor is lifted, the same writes are driven, and both labels have to
  appear -- on an `aiosqlite` line, on stdout and in a record. A label is a value no rule of
  #23's value redaction touches, so it, not the address, is what proves the floor is what
  stops the line: since spec 030 the address is redacted on stdout even with the floor
  lifted, which the control asserts too. If a future release stops logging statements or
  renames its logger, that test goes red and says the floor is guarding a route nothing uses,
  rather than this module passing against nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.api.session_cookie import SECURE_SESSION_COOKIE_NAME
from portfolio.logging import (
    SILENCED_VENDOR_LOGGERS,
    STATEMENT_LOGGING_LIBRARIES,
    VENDOR_LOG_FLOOR,
)
from portfolio.main import create_app
from tests.address_vectors import BIP173_TESTNET_P2WPKH
from tests.auth.conftest import BASE_URL, JSON_HEADERS, OWNER_PHRASE, sign_in
from tests.security.conftest import EveryRecord, assert_absent, assert_carried_something

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from tests.security.conftest import ProductionLoggingInstaller

WALLETS: Final = "/api/wallets"
DRIVER: Final = "aiosqlite"

#: Built at run time from short cycles, so no literal here looks like anything to a scanner.
LABEL_SENTINEL: Final = "lbl-" + "Rk5" * 6
RENAMED_SENTINEL: Final = "lbl-" + "Tz8" * 6

#: Written through the application's pipeline at DEBUG, so a capture that holds it was live.
MARKER: Final = "statement_logging_probe"


# --------------------------------------------------------------------------------------
# The floor
# --------------------------------------------------------------------------------------


def test_the_silenced_logger_lists_are_pinned_against_a_literal() -> None:
    """Removing `aiosqlite` has to fail here rather than quietly reopening the leak.

    Pinned against a literal for the reason `URL_LOGGING_LIBRARIES` is: a guard that derives
    its expectation from the thing it guards shrinks along with it and cannot fail. The
    joined list is pinned too, because that is the one `configure_logging` actually walks.
    """
    assert STATEMENT_LOGGING_LIBRARIES == (DRIVER,)
    assert SILENCED_VENDOR_LOGGERS == ("httpx", "httpcore", "aiosqlite")
    assert VENDOR_LOG_FLOOR == logging.WARNING


@pytest.mark.parametrize("log_level", ["DEBUG", "INFO", "WARNING"])
def test_the_driver_floor_is_absolute_and_not_relative_to_the_application_level(
    log_level: str,
    production_logging: ProductionLoggingInstaller,
) -> None:
    """DEBUG is the level that matters: the leak did not exist at the production default.

    Asserted on `.level` as well as `getEffectiveLevel()`, because the first says the floor
    is on the logger itself, which is what survives the `logging.basicConfig(force=True)`
    that `configure_logging` calls. A filter on the root handler would pass the second and
    be removed by the next call.
    """
    production_logging(log_level)

    driver = logging.getLogger(DRIVER)
    assert driver.level == VENDOR_LOG_FLOOR
    assert driver.getEffectiveLevel() == VENDOR_LOG_FLOOR
    assert logging.getLogger().level == logging.getLevelNamesMapping()[log_level]


# --------------------------------------------------------------------------------------
# The bytes
# --------------------------------------------------------------------------------------


async def write_a_wallet(client: AsyncClient) -> None:
    """An INSERT and an UPDATE on `wallets`, through the router down to the driver."""
    created = await client.post(
        WALLETS,
        json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WPKH, "label": LABEL_SENTINEL},
        headers=JSON_HEADERS,
    )
    assert created.status_code == 201, created.text
    renamed = await client.patch(
        f"{WALLETS}/{created.json()['id']}",
        json={"label": RENAMED_SENTINEL},
        headers=JSON_HEADERS,
    )
    assert renamed.status_code == 200, renamed.text


async def captured_writes(
    client: AsyncClient,
    capsys: pytest.CaptureFixture[str],
) -> tuple[str, str]:
    """Drive the writes and a DEBUG probe; answer stdout and every stdlib record, rendered.

    Whatever the test did before this call -- building the application, signing in -- is
    read off and discarded first, so what is searched is what the writes produced.
    """
    capsys.readouterr()
    records = EveryRecord()
    root = logging.getLogger()
    root.addHandler(records)
    try:
        await write_a_wallet(client)
        structlog.get_logger(__name__).debug(MARKER)
    finally:
        root.removeHandler(records)
    return capsys.readouterr().out, "\n".join(records.rendered)


async def stored_values(sessionmaker: async_sessionmaker[AsyncSession]) -> list[tuple[str, str]]:
    async with sessionmaker() as session:
        rows = await session.execute(text("SELECT address_canonical, label FROM wallets"))
        return [(row.address_canonical, row.label) for row in rows]


async def test_a_wallet_written_at_debug_reaches_no_log_line(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The claim: at DEBUG, neither the address nor either label reaches any log."""
    production_logging("DEBUG")

    written, records = await captured_writes(signed_in_api_client, capsys)

    # The positive companions: DEBUG was on, both captures were live, the rows were written.
    assert_carried_something(written, marker=MARKER)
    assert MARKER in records, "the stdlib capture never saw the probe"
    assert await stored_values(api_sessionmaker) == [(BIP173_TESTNET_P2WPKH, RENAMED_SENTINEL)]
    # The claim.
    for where, searched in {"stdout": written, "a log record": records}.items():
        assert_absent(searched, BIP173_TESTNET_P2WPKH)
        assert LABEL_SENTINEL not in searched, f"the created label reached {where}"
        assert RENAMED_SENTINEL not in searched, f"the renamed label reached {where}"


async def test_the_owner_bootstrap_and_a_sign_in_at_debug_put_no_credential_on_any_line(
    api_environment: Path,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rows #106 did not name, and the worst ones: a password hash and a session.

    Measured while fixing it: with the floor lifted, the lifespan's owner bootstrap put its
    `INSERT INTO users` on stdout with the Argon2 hash of the owner's password, and every
    sign-in put its `INSERT INTO sessions` there with the session's token hash. The hash is
    what an offline guessing attack starts from. So the application is started at DEBUG from
    an empty database, which is the one start that bootstraps an owner, and signed in to.
    """
    del api_environment
    app = create_app()
    production_logging("DEBUG")
    records = EveryRecord()
    root = logging.getLogger()
    root.addHandler(records)
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client,
        ):
            await sign_in(client)
            cookie = client.cookies[SECURE_SESSION_COOKIE_NAME]
            structlog.get_logger(__name__).debug(MARKER)
            async with app.state.db_sessionmaker() as session:
                password_hash = await session.scalar(text("SELECT password_hash FROM users"))
                token_hash = await session.scalar(text("SELECT token_hash FROM sessions"))
    finally:
        root.removeHandler(records)
    written = capsys.readouterr().out
    rendered = "\n".join(records.rendered)

    # The positive companions: DEBUG was on and both captures were live from before the
    # lifespan started; the owner and the session really were written.
    assert_carried_something(written, marker=MARKER)
    assert MARKER in rendered, "the stdlib capture never saw the probe"
    assert isinstance(password_hash, str)
    assert password_hash.startswith("$argon2")
    assert isinstance(token_hash, str)
    assert token_hash
    # The claim.
    for where, searched in {"stdout": written, "a log record": rendered}.items():
        assert "$argon2" not in searched, f"a password hash reached {where}"
        assert password_hash not in searched, f"the owner's password hash reached {where}"
        assert token_hash not in searched, f"a session's token hash reached {where}"
        assert cookie not in searched, f"a session cookie reached {where}"
        assert OWNER_PHRASE not in searched, f"the bootstrap password reached {where}"


async def test_the_statement_reaches_the_log_the_moment_the_floor_is_lifted(
    signed_in_api_client: AsyncClient,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The control, and the reason the test above is not a test of nothing.

    The floor is lifted to DEBUG, the same writes are driven, and both labels must be
    **present** on a line the driver wrote, on stdout and in a record. A label is what no value
    rule redacts, so its presence is the floor's absence and nothing else -- the address was
    that witness until #23, and is now redacted on stdout by the value rule even with the floor
    lifted, which this asserts as well. A record seen by the test's own handler is not
    asserted redacted: a standard-library record reaches a handler beside the root's as the
    library wrote it (spec 030, R10).

    `production_logging`'s teardown restores the floor through `tests/logging_harness.py`,
    and `test_logging_harness.py` proves that it does; the `finally` here is so the restore
    does not depend on which of them runs first.
    """
    production_logging("DEBUG")
    driver = logging.getLogger(DRIVER)
    try:
        driver.setLevel(logging.DEBUG)
        written, records = await captured_writes(signed_in_api_client, capsys)
    finally:
        driver.setLevel(VENDOR_LOG_FLOOR)

    for where, searched in {"stdout": written, "a log record": records}.items():
        statements = [line for line in searched.splitlines() if LABEL_SENTINEL in line]
        assert statements, (
            f"with the floor lifted no statement reached {where}: {DRIVER} no longer logs its "
            "statements, so the floor guards a route nothing uses and the reason for it needs "
            "revisiting"
        )
        assert any("executing" in line for line in statements), f"not the driver's line in {where}"
        assert RENAMED_SENTINEL in searched
    assert any(f'"logger": "{DRIVER}"' in line for line in written.splitlines())
    assert any(line.startswith(DRIVER) for line in records.splitlines())
    assert_absent(written, BIP173_TESTNET_P2WPKH)
