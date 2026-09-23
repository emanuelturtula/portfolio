"""`refresh-prices`: the entry point that exists so the call budget can be measured.

The spec is explicit that #9 delivers no scheduler -- #10 owns that -- and that this
command is what stands in for one until then: *run it by hand, count the requests in the
log, and the number in `docs/providers.md` stops being arithmetic and becomes an
observation.*

## The assertion that matters is the exit code

**Exit code 1 when anything is unavailable; 0 only when every pair refreshed.** A command
that succeeded at three pairs out of four has not succeeded, and a scheduler reading only
the exit code would record a good run -- with the missing pair surfacing days later as a
portfolio total that has been quietly short the whole time. That is criterion 3's failure
arriving through the one interface that has no room for a flag beside the number.

The pairs that *did* work are still printed in that case, because the operator needs to
know which vendor answered.

## Why the client is replaced and the database is not

`run_price_refresh` builds its own `httpx.AsyncClient` with no arguments, which in a test
would reach four real vendors. `cli.build_http_client` is therefore replaced with one over
a scripted transport -- the only substitution in this module. Everything else is the real
thing: the real settings out of the environment, the real engine, the real migrations, the
real service, and a real file on disk read back afterwards through a second connection.

Prices are printed by this command, deliberately: they are public market data, and nothing
here names a wallet, an address or a quantity. The assertions below check that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import text

from portfolio import cli
from portfolio.config import get_settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.providers.prices.base import BTC, EUR, KAS, SUPPORTED_PAIRS, USD
from portfolio.providers.prices.coinbase import COINBASE
from portfolio.providers.prices.kraken import KRAKEN
from portfolio.services.prices import PriceUnavailable
from tests.providers.prices.harness import (
    KASPA_PRIMARY_URL,
    KRAKEN_PRICES,
    PriceFake,
    Reply,
    ScriptedVendor,
    coinbase_body,
    kraken_echo,
    price_client,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

REFRESH: Final[list[str]] = ["refresh-prices"]

#: The Coinbase figure, deliberately different from Kraken's for the same pair, so the
#: `via <source>` line is checked against a number only one of them could have supplied.
COINBASE_BTC_USD: Final = "85999.90"


@pytest.fixture
def migrated_cli_database(cli_database: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The CLI's own database, migrated to head, with the Kaspa node pointed somewhere safe.

    `run_price_refresh` deliberately does **not** migrate -- unlike `create-user`, which is
    the command that bootstraps an instance. A refresh against an unmigrated database is an
    operator error rather than a thing to silently repair, and migrating here would mean a
    scheduled job could rewrite the schema of a running deployment.

    The environment variable is the interesting half. Three of the four sources take their
    base URL from a module constant and the scripted transport routes on the host parsed out
    of those, but the **Kaspa price source reads `PORTFOLIO_KASPA_API_URL`** -- the same
    variable the chain provider reads, because it is the same server. Left at its shipped
    default this command reaches for a real host that the fake does not route, which is how
    this fixture found out that the setting is genuinely consulted rather than merely
    accepted.
    """
    monkeypatch.setenv("PORTFOLIO_KASPA_API_URL", KASPA_PRIMARY_URL)
    monkeypatch.setenv("PORTFOLIO_KASPA_API_FALLBACK_URL", "")
    get_settings.cache_clear()
    cli_database.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(f"sqlite+aiosqlite:///{cli_database.as_posix()}")
    return cli_database


def with_vendors(monkeypatch: pytest.MonkeyPatch, fake: PriceFake) -> None:
    """Replace the client the command builds with one over the scripted transport.

    Patched on `cli` rather than on `providers.http`, because `cli` imported the name at
    module load and rebinding the origin would leave this caller holding the original --
    the failure that makes a monkeypatch look applied and do nothing.
    """
    monkeypatch.setattr(cli, "build_http_client", lambda: price_client(fake))


def stored_prices(sync_engine: Engine) -> dict[tuple[str, str], tuple[str, str]]:
    """Every row in `prices`, keyed by pair, as the raw `TEXT` amount and its source.

    Read through a second, unconfigured connection and without the ORM: what this asserts
    is what is on disk after the command exited, which is the only thing an operator
    running it by hand will ever have.
    """
    with sync_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT a.symbol, p.quote_currency, p.amount, p.source "
                "FROM prices p JOIN assets a ON a.id = p.asset_id"
            )
        ).all()
    return {(row[0], row[1]): (row[2], row[3]) for row in rows}


# --------------------------------------------------------------------------------------
# The healthy refresh
# --------------------------------------------------------------------------------------


def test_a_complete_refresh_prints_every_pair_and_exits_zero(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One line per pair on stdout, nothing on stderr, and exit code 0.

    The `as of` line is asserted separately from the price lines because it is the one an
    operator uses to tell a refresh that ran from one that did nothing: without it, a
    command that printed four prices from the previous run would look identical.
    """
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=kraken_echo())))
    with_vendors(monkeypatch, fake)

    exit_code = cli.main(REFRESH)

    captured = capsys.readouterr()
    lines = captured.out.splitlines()

    assert exit_code == 0
    assert captured.err == "", "a complete refresh has nothing to report as a failure"
    assert lines[0].startswith("as of ")
    assert len(lines) == 1 + len(SUPPORTED_PAIRS)
    assert f"{BTC}/{USD} {KRAKEN_PRICES['XXBTZUSD']} via {KRAKEN}" in lines
    assert f"{KAS}/{EUR} {KRAKEN_PRICES['KASEUR']} via {KRAKEN}" in lines


def test_a_complete_refresh_writes_every_pair_to_the_database(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What the command printed and what it stored are the same four prices.

    Read back through a second connection after the process's own engine was disposed, so
    this is an assertion about the file rather than about the session that wrote it -- and
    the stored text is the fixed-point form `NumericText` writes, digits intact.
    """
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=kraken_echo())))
    with_vendors(monkeypatch, fake)

    assert cli.main(REFRESH) == 0
    capsys.readouterr()

    stored = stored_prices(sync_engine)

    assert set(stored) == SUPPORTED_PAIRS
    assert {source for _amount, source in stored.values()} == {KRAKEN}
    assert stored[(KAS, USD)][0] == "0.042286450000"


def test_the_command_costs_one_request_which_is_the_whole_point_of_it(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The measurement this command exists to make possible, made here as well.

    `docs/providers.md` says one request per refresh and multiplies it out to 720 a month.
    That figure is arithmetic on a measurement taken by hand on one day; this is the same
    claim checked against the code on every CI run, through the entry point an operator
    would use rather than through the loop underneath it.
    """
    del migrated_cli_database, sync_engine
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=kraken_echo())))
    with_vendors(monkeypatch, fake)

    assert cli.main(REFRESH) == 0
    capsys.readouterr()

    assert len(fake.requests) == 1
    assert sum(fake.counts.values()) == 1


# --------------------------------------------------------------------------------------
# The incomplete refresh, which is the reason the exit code exists
# --------------------------------------------------------------------------------------


def test_an_incomplete_refresh_exits_one_and_still_reports_what_worked(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Kraken down, Coinbase answering two pairs: two prices, two reasons, exit 1.

    The exit code is the assertion that carries this module. A scheduler -- #10's, or cron
    in the meantime -- reads it and nothing else, so a command that returned 0 here would
    record a successful run for a refresh that is short two pairs, and the portfolio total
    would be quietly incomplete until somebody looked at a dashboard and did the arithmetic
    by hand.

    The successful pairs still go to stdout, because "which vendor answered" is the thing
    an operator needs on the day the primary is down.
    """
    del migrated_cli_database
    fake = PriceFake(
        kraken=ScriptedVendor(Reply(status=503)),
        coinbase=ScriptedVendor(
            Reply(
                renderer=lambda request: coinbase_body(
                    base=BTC,
                    currency=request.url.path.removesuffix("/spot")[-3:],
                    amount=COINBASE_BTC_USD,
                )
            )
        ),
    )
    with_vendors(monkeypatch, fake)

    exit_code = cli.main(REFRESH)

    captured = capsys.readouterr()

    assert exit_code == 1
    assert f"{BTC}/{USD} {COINBASE_BTC_USD} via {COINBASE}" in captured.out
    assert f"{KAS}/{USD} unavailable: {PriceUnavailable.EVERY_SOURCE_FAILED.value}" in captured.err
    assert f"{KAS}/{EUR} unavailable: {PriceUnavailable.EVERY_SOURCE_FAILED.value}" in captured.err
    assert "2 of 4 pair(s) were not refreshed." in captured.err


def test_an_entirely_failed_refresh_exits_one_and_writes_nothing(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every vendor down: four reasons, no rows, exit 1.

    The empty table is the half that matters. A command that wrote zeros for the pairs it
    could not price would produce a portfolio worth nothing, complete and believed -- which
    is the exact sentence criterion 3 is quoted for in the spec.
    """
    del migrated_cli_database
    fake = PriceFake(
        kraken=ScriptedVendor(Reply(status=503)),
        coinbase=ScriptedVendor(Reply(status=503)),
        kaspa=ScriptedVendor(Reply(status=503)),
    )
    with_vendors(monkeypatch, fake)

    exit_code = cli.main(REFRESH)

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out.splitlines()[0].startswith("as of ")
    assert len(captured.out.splitlines()) == 1, "nothing was refreshed, so nothing is listed"
    assert "4 of 4 pair(s) were not refreshed." in captured.err
    assert stored_prices(sync_engine) == {}


def test_the_reason_printed_is_the_enum_value_an_operator_can_search_for(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`every_source_failed`, not `PriceUnavailable.EVERY_SOURCE_FAILED`.

    The wire value is what appears in `docs/`, in a log, and eventually in an API response,
    so it is the string somebody will paste into a search box. A `StrEnum` formats as its
    value, which is the whole reason the spec chose one -- and it is a property of the type
    rather than of this command, so it is worth an assertion that would notice the type
    changing.
    """
    del migrated_cli_database, sync_engine
    fake = PriceFake(
        kraken=ScriptedVendor(Reply(status=503)),
        coinbase=ScriptedVendor(Reply(status=503)),
        kaspa=ScriptedVendor(Reply(status=503)),
    )
    with_vendors(monkeypatch, fake)

    cli.main(REFRESH)

    captured = capsys.readouterr()

    assert "every_source_failed" in captured.err
    assert "PriceUnavailable." not in captured.err


def test_the_command_takes_no_options_and_names_no_credential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`refresh-prices` has no flags, so there is nothing to pass a key through.

    The same rule `create-user` follows for a password, applied to the one credential this
    change introduces: a command-line argument lands in the shell history and in `ps`
    output. The key is read from the environment into a `SecretStr` and nowhere else.
    """
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["refresh-prices", "--api-key", "anything"])

    rendered = capsys.readouterr().err
    assert "unrecognized arguments" in rendered or "invalid choice" in rendered

    parsed = parser.parse_args(REFRESH)
    assert parsed.handler is cli.refresh_prices
    assert not any(name.endswith("key") for name in vars(parsed))
