"""`backfill-prices` (spec 037): every daily close Kraken still serves, now, by hand.

The same harness as `test_refresh_prices.py` and for its reasons: the command is called in
process, `cli.build_http_client` is replaced with one over a scripted Kraken -- the only
substitution -- and everything else is real: the settings out of the environment, the
engine, the migrations, the service, and the file read back through a second connection.

## The exit code, again

**1 when any pair failed, 0 only when every pair was answered** -- including a pair answered
with no committed close, which is a true answer rather than a failure. A cron line or an
operator reading only the code must not record a good run for a backfill that is short a
pair.

## No price is printed

Per pair: the number of days and the first and last of them. 720 lines of prices would bury
the answer, and the table is where they are; the assertions below check no close appears.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import event, text

from portfolio import cli
from portfolio.config import get_settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import create_database_engine
from portfolio.providers.prices.base import BTC, KAS, USD
from portfolio.providers.prices.kraken import KRAKEN
from tests.providers.prices.harness import (
    KRAKEN_HOST,
    PriceFake,
    Reply,
    ScriptedVendor,
    price_client,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import httpx
    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine

BACKFILL: Final[list[str]] = ["backfill-prices"]

#: 2024-10-18, 19 and 20 at 00:00 UTC, then the candle still trading on the 21st.
COMMITTED: Final = (1729209600, 1729296000, 1729382400)
TODAY: Final = 1729468800

#: Closes no other line in the output could contain by accident.
CLOSES: Final = ("67123.45678", "67234.56789", "67345.67891")
MOVING_CLOSE: Final = "67999.99999"

#: A backfilled line: `SYMBOL/CURRENCY N day(s): first to last`, anchored end to end, for
#: the reason `test_refresh_prices.REFRESHED_LINE` is: a stray log line must not satisfy it.
BACKFILLED_LINE: Final = re.compile(
    r"^[A-Z]+/[A-Z]{3} \d+ day\(s\): (\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}|no committed close)$"
)

#: A failed line on stderr: `SYMBOL/CURRENCY failed: ErrorClass`.
FAILED_LINE: Final = re.compile(r"^[A-Z]+/[A-Z]{3} failed: \w+$")


def ohlc_body(code: str, candles: Sequence[tuple[int, str]], *, last: int) -> str:
    """Kraken's OHLC envelope, written as text: the close at index 4 and as a string."""
    entries = ",".join(
        f'[{opened},"1.1","2.2","0.5","{close}","1.7","10.00000000",42]'
        for opened, close in candles
    )
    return f'{{"error":[],"result":{{"{code}":[{entries}],"last":{last}}}}}'


def three_days_and_today(request: httpx.Request) -> str:
    """A Kraken answering the pair the request named, with three committed days and today."""
    code = request.url.params.get("pair", "")
    candles = [*zip(COMMITTED, CLOSES, strict=True), (TODAY, MOVING_CLOSE)]
    return ohlc_body(code, candles, last=COMMITTED[-1])


def only_today(request: httpx.Request) -> str:
    """A pair listed today: one entry, still trading, and nothing committed."""
    code = request.url.params.get("pair", "")
    return ohlc_body(code, [(TODAY, MOVING_CLOSE)], last=COMMITTED[-1])


@pytest.fixture
def migrated_cli_database(cli_database: Path) -> Path:
    """The CLI's own database, migrated to head. The command itself does not migrate.

    Nothing else to point anywhere: the backfill's one source is Kraken, whose base URL is a
    module constant the scripted transport routes on.
    """
    cli_database.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(f"sqlite+aiosqlite:///{cli_database.as_posix()}")
    return cli_database


def with_kraken(monkeypatch: pytest.MonkeyPatch, fake: PriceFake) -> list[httpx.AsyncClient]:
    """Replace the client the command builds, and keep every one built so it can be checked.

    Patched on `cli`, where the name was imported, for the reason `test_refresh_prices`
    gives.
    """
    built: list[httpx.AsyncClient] = []

    def build() -> httpx.AsyncClient:
        client = price_client(fake)
        built.append(client)
        return client

    monkeypatch.setattr(cli, "build_http_client", build)
    return built


def matching(pattern: re.Pattern[str], stream: str) -> list[str]:
    return [line for line in stream.splitlines() if pattern.match(line)]


def stored_history(sync_engine: Engine) -> list[tuple[str, str, str, str, str, str]]:
    """Every `price_history` row as raw text, through a second, unconfigured connection."""
    with sync_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT a.symbol, h.quote_currency, h.day, h.amount, h.basis, h.source "
                "FROM price_history h JOIN assets a ON a.id = h.asset_id "
                "ORDER BY a.symbol, h.day"
            )
        ).all()
    return [(row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows]


# --------------------------------------------------------------------------------------
# The complete backfill
# --------------------------------------------------------------------------------------


def test_a_complete_backfill_prints_each_pairs_span_and_exits_zero(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One line per pair, the committed days only, and no price anywhere in the output."""
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=three_days_and_today)))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert matching(BACKFILLED_LINE, captured.out) == [
        f"{BTC}/{USD} 3 day(s): 2024-10-18 to 2024-10-20",
        f"{KAS}/{USD} 3 day(s): 2024-10-18 to 2024-10-20",
    ]
    assert matching(FAILED_LINE, captured.err) == []
    assert "not backfilled" not in captured.err
    for close in (*CLOSES, MOVING_CLOSE):
        assert close not in captured.out + captured.err, "no price is printed"
    assert fake.counts[KRAKEN_HOST] == len((BTC, KAS)), "one request per pair"


def test_a_complete_backfill_writes_every_committed_close_to_the_database(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On disk after the command exited: three closes per pair, none for the moving day."""
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=three_days_and_today)))
    with_kraken(monkeypatch, fake)

    assert cli.main(BACKFILL) == 0
    capsys.readouterr()

    rows = stored_history(sync_engine)
    days = ["2024-10-18", "2024-10-19", "2024-10-20"]
    assert [(row[0], row[2]) for row in rows] == [(BTC, day) for day in days] + [
        (KAS, day) for day in days
    ]
    assert {(row[1], row[4], row[5]) for row in rows} == {(USD, "close", KRAKEN)}
    assert [row[3] for row in rows if row[0] == BTC] == [f"{close}0000000" for close in CLOSES]


def test_running_it_twice_leaves_the_same_rows(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Idempotent through the entry point an operator uses, not only through the service."""
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=three_days_and_today)))
    with_kraken(monkeypatch, fake)

    assert cli.main(BACKFILL) == 0
    first = stored_history(sync_engine)
    assert cli.main(BACKFILL) == 0
    capsys.readouterr()

    assert stored_history(sync_engine) == first


def test_a_pair_with_no_committed_close_says_so_and_is_not_a_failure(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero days is a true answer: printed as such, exit 0, and nothing stored."""
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=only_today)))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert matching(BACKFILLED_LINE, captured.out) == [
        f"{BTC}/{USD} 0 day(s): no committed close",
        f"{KAS}/{USD} 0 day(s): no committed close",
    ]
    assert matching(FAILED_LINE, captured.err) == []
    assert stored_history(sync_engine) == []


# --------------------------------------------------------------------------------------
# The incomplete backfill
# --------------------------------------------------------------------------------------


def test_a_failing_pair_is_a_stderr_line_and_exit_one_and_the_other_is_still_stored(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BTC answers, KAS meets a 503 through every retry: one line each side, and exit 1.

    The pairs are asked in sorted order, so the script's first reply is BTC's and the 503
    -- which repeats -- is every attempt at KAS. The failed line carries the error's class
    name, never the vendor's text and never a host.
    """
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=three_days_and_today), Reply(status=503)))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert matching(BACKFILLED_LINE, captured.out) == [
        f"{BTC}/{USD} 3 day(s): 2024-10-18 to 2024-10-20"
    ]
    assert matching(FAILED_LINE, captured.err) == [f"{KAS}/{USD} failed: ProviderUnavailableError"]
    assert "1 of 2 pair(s) were not backfilled." in captured.err
    assert KRAKEN_HOST not in captured.err
    assert {row[0] for row in stored_history(sync_engine)} == {BTC}


def test_every_pair_failing_is_exit_one_and_stores_nothing(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(status=429)))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert matching(BACKFILLED_LINE, captured.out) == []
    assert matching(FAILED_LINE, captured.err) == [
        f"{BTC}/{USD} failed: ProviderRateLimitedError",
        f"{KAS}/{USD} failed: ProviderRateLimitedError",
    ]
    assert "2 of 2 pair(s) were not backfilled." in captured.err
    assert stored_history(sync_engine) == []


# --------------------------------------------------------------------------------------
# Lifetimes: the client and the engine are this command's, and both are closed
# --------------------------------------------------------------------------------------


def recording_engines(monkeypatch: pytest.MonkeyPatch) -> list[list[object]]:
    """Wrap the engine factory `cli` uses, recording each engine's `dispose()`.

    `engine_disposed` is SQLAlchemy's own event for exactly that call, so it fires whether or
    not the command got as far as opening a connection -- which the `close` event on the
    pool would not, for a run that failed before its first query.
    """
    disposals: list[list[object]] = []

    def create(url: str) -> AsyncEngine:
        engine = create_database_engine(url)
        disposed: list[object] = []
        event.listen(engine.sync_engine, "engine_disposed", disposed.append)
        disposals.append(disposed)
        return engine

    monkeypatch.setattr(cli, "create_database_engine", create)
    return disposals


def test_the_client_and_the_engine_are_closed_after_a_backfill(
    migrated_cli_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One client built and closed, one engine built and disposed, by the command itself."""
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=three_days_and_today)))
    built = with_kraken(monkeypatch, fake)
    engines = recording_engines(monkeypatch)

    assert cli.main(BACKFILL) == 0
    capsys.readouterr()

    assert len(built) == 1
    assert built[0].is_closed is True
    assert [len(disposed) for disposed in engines] == [1], "one engine, disposed once"


def test_the_client_and_the_engine_are_closed_when_the_backfill_raises(
    migrated_cli_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `finally`: an unexpected exception still releases the pool and the file handle."""
    del migrated_cli_database
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=three_days_and_today)))
    built = with_kraken(monkeypatch, fake)
    engines = recording_engines(monkeypatch)

    class Exploding:
        async def backfill(self) -> object:
            message = "a bug, not a vendor"
            raise RuntimeError(message)

    monkeypatch.setattr(cli, "build_price_backfill_service", lambda *_a, **_k: Exploding())

    with pytest.raises(RuntimeError, match="a bug, not a vendor"):
        cli.main(BACKFILL)

    assert built[0].is_closed is True
    assert [len(disposed) for disposed in engines] == [1], "disposed on the way out regardless"
    assert fake.requests == []


def test_the_command_takes_no_options_and_is_registered_under_its_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`backfill-prices` with no flags, dispatching to `cli.backfill_prices`."""
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["backfill-prices", "--since", "2024-01-01"])

    assert "unrecognized arguments" in capsys.readouterr().err
    parsed = parser.parse_args(BACKFILL)
    assert parsed.handler is cli.backfill_prices


def test_the_command_reads_the_database_the_settings_name(
    migrated_cli_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run_price_backfill` is handed the cached settings, so it writes the configured file."""
    fake = PriceFake(kraken=ScriptedVendor(Reply(renderer=three_days_and_today)))
    with_kraken(monkeypatch, fake)
    urls: list[str] = []

    def create(url: str) -> AsyncEngine:
        urls.append(url)
        return create_database_engine(url)

    monkeypatch.setattr(cli, "create_database_engine", create)

    assert cli.main(BACKFILL) == 0
    capsys.readouterr()

    assert urls == [get_settings().database_url]
    assert migrated_cli_database.as_posix() in urls[0]
