"""`backfill-prices` (specs 037 and 038): every daily close Kraken still serves, now, by hand,
and BTC/USD from Coinbase Exchange for the days before Kraken's.

The same harness as `test_refresh_prices.py` and for its reasons: the command is called in
process, `cli.build_http_client` is replaced with one over a scripted Kraken and a scripted
Coinbase Exchange -- the only substitution -- and everything else is real: the settings out
of the environment, the engine, the migrations, the service, and the file read back through
a second connection.

## The exit code, again

**1 when any pair failed, 0 only when every pair was answered** -- including a pair answered
with no committed close, which is a true answer rather than a failure. A cron line or an
operator reading only the code must not record a good run for a backfill that is short a
pair.

## No price is printed

Per pair and source: the number of days and the first and last of them. 720 lines of prices
would bury the answer, and the table is where they are; the assertions below check no close
appears.
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
from portfolio.providers.prices.coinbase import COINBASE
from portfolio.providers.prices.kraken import KRAKEN
from tests.providers.prices.harness import (
    COINBASE_EXCHANGE_HOST,
    KRAKEN_HOST,
    PriceFake,
    Reply,
    ScriptedVendor,
    coinbase_candles_echo,
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

#: Coinbase Exchange's two candles in the range before Kraken's first day, 2024-10-18: its
#: first day, 2015-07-20, and the day before Kraken's. JSON **numbers**, as the vendor sends.
COINBASE_CLOSES: Final = {1437350400: "277.89123", 1729123200: "67011.11111"}

#: 2015-07-20 to 2024-10-17 in windows of 300 days: twelve requests, as measured.
COINBASE_WINDOWS: Final = 12

#: A backfilled line: `SYMBOL/CURRENCY N day(s) via source: first to last`, anchored end to
#: end, for the reason `test_refresh_prices.REFRESHED_LINE` is: a stray log line must not
#: satisfy it.
BACKFILLED_LINE: Final = re.compile(
    r"^[A-Z]+/[A-Z]{3} \d+ day\(s\) via [a-z]+: "
    r"(\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}|no committed close)$"
)

#: A failed line on stderr: `SYMBOL/CURRENCY via source failed: ErrorClass`.
FAILED_LINE: Final = re.compile(r"^[A-Z]+/[A-Z]{3} via [a-z]+ failed: \w+$")

#: What the complete run prints, in order: per pair, Kraken's line before Coinbase's.
COMPLETE_LINES: Final = [
    f"{BTC}/{USD} 3 day(s) via {KRAKEN}: 2024-10-18 to 2024-10-20",
    f"{BTC}/{USD} 2 day(s) via {COINBASE}: 2015-07-20 to 2024-10-17",
    f"{KAS}/{USD} 3 day(s) via {KRAKEN}: 2024-10-18 to 2024-10-20",
]


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


def kas_only_today(request: httpx.Request) -> str:
    """BTC with three committed days, and KAS listed today with nothing committed."""
    if request.url.params.get("pair") == "KASUSD":
        return only_today(request)
    return three_days_and_today(request)


def answering(*kraken: Reply, coinbase: Reply | None = None) -> PriceFake:
    """Kraken scripted with `kraken`; Coinbase Exchange answering its two candles by window."""
    exchange = (
        coinbase if coinbase is not None else Reply(renderer=coinbase_candles_echo(COINBASE_CLOSES))
    )
    return PriceFake(kraken=ScriptedVendor(*kraken), coinbase_exchange=ScriptedVendor(exchange))


@pytest.fixture
def migrated_cli_database(cli_database: Path) -> Path:
    """The CLI's own database, migrated to head. The command itself does not migrate.

    Nothing else to point anywhere: the backfill's sources are Kraken and Coinbase Exchange,
    whose base URLs are module constants the scripted transport routes on.
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
    """One line per pair and source, the committed days only, and no price in the output.

    BTC/USD has two: Kraken's three days, then Coinbase's two before them -- asked in twelve
    windows from 2015-07-20 to the day before Kraken's first close.
    """
    del migrated_cli_database
    fake = answering(Reply(renderer=three_days_and_today))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert matching(BACKFILLED_LINE, captured.out) == COMPLETE_LINES
    assert matching(FAILED_LINE, captured.err) == []
    assert "backfilled" not in captured.err
    for close in (*CLOSES, MOVING_CLOSE, *COINBASE_CLOSES.values()):
        assert close not in captured.out + captured.err, "no price is printed"
    assert fake.counts[KRAKEN_HOST] == len((BTC, KAS)), "one request per pair"
    assert fake.counts[COINBASE_EXCHANGE_HOST] == COINBASE_WINDOWS


def test_a_complete_backfill_writes_every_committed_close_to_the_database(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On disk after the command exited: three closes per pair, none for the moving day, and
    Coinbase's two BTC closes before them -- the exact digits of its JSON numbers."""
    del migrated_cli_database
    fake = answering(Reply(renderer=three_days_and_today))
    with_kraken(monkeypatch, fake)

    assert cli.main(BACKFILL) == 0
    capsys.readouterr()

    rows = stored_history(sync_engine)
    days = ["2024-10-18", "2024-10-19", "2024-10-20"]
    assert [(row[0], row[2], row[5]) for row in rows] == [
        (BTC, "2015-07-20", COINBASE),
        (BTC, "2024-10-17", COINBASE),
        *((BTC, day, KRAKEN) for day in days),
        *((KAS, day, KRAKEN) for day in days),
    ]
    assert {(row[1], row[4]) for row in rows} == {(USD, "close")}
    assert [row[3] for row in rows if row[0] == BTC] == [
        "277.891230000000",
        "67011.111110000000",
        *(f"{close}0000000" for close in CLOSES),
    ]


def test_running_it_twice_leaves_the_same_rows(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Idempotent through the entry point an operator uses, not only through the service.

    And the second run asks Coinbase nothing: BTC/USD is filled back to 2015-07-20, so the
    Coinbase line is gone from the output.
    """
    del migrated_cli_database
    fake = answering(Reply(renderer=three_days_and_today))
    with_kraken(monkeypatch, fake)

    assert cli.main(BACKFILL) == 0
    first = stored_history(sync_engine)
    capsys.readouterr()
    assert cli.main(BACKFILL) == 0
    second = capsys.readouterr()

    assert stored_history(sync_engine) == first
    assert fake.counts[COINBASE_EXCHANGE_HOST] == COINBASE_WINDOWS, "none on the second run"
    assert matching(BACKFILLED_LINE, second.out) == [
        line for line in COMPLETE_LINES if COINBASE not in line
    ]


def test_a_pair_with_no_committed_close_says_so_and_is_not_a_failure(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero days is a true answer: printed as such, exit 0, and nothing stored for KAS."""
    del migrated_cli_database
    fake = answering(Reply(renderer=kas_only_today))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert matching(BACKFILLED_LINE, captured.out) == [
        *COMPLETE_LINES[:2],
        f"{KAS}/{USD} 0 day(s) via {KRAKEN}: no committed close",
    ]
    assert matching(FAILED_LINE, captured.err) == []
    assert {row[0] for row in stored_history(sync_engine)} == {BTC}


def test_no_close_stored_for_btc_is_a_coinbase_failure_without_a_request(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Kraken committed nothing for either pair: Coinbase has no close to extend back from.

    `NoRecentClose`, on stderr, exit 1 -- the BTC history is short and the operator can see
    why -- and no request to Coinbase. The count is of pairs, not of lines.
    """
    del migrated_cli_database
    fake = answering(Reply(renderer=only_today))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert matching(BACKFILLED_LINE, captured.out) == [
        f"{BTC}/{USD} 0 day(s) via {KRAKEN}: no committed close",
        f"{KAS}/{USD} 0 day(s) via {KRAKEN}: no committed close",
    ]
    assert matching(FAILED_LINE, captured.err) == [
        f"{BTC}/{USD} via {COINBASE} failed: NoRecentClose"
    ]
    assert "1 of 2 pair(s) were not fully backfilled." in captured.err
    assert fake.counts[COINBASE_EXCHANGE_HOST] == 0
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
    fake = answering(Reply(renderer=three_days_and_today), Reply(status=503))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert matching(BACKFILLED_LINE, captured.out) == COMPLETE_LINES[:2]
    assert matching(FAILED_LINE, captured.err) == [
        f"{KAS}/{USD} via {KRAKEN} failed: ProviderUnavailableError"
    ]
    assert "1 of 2 pair(s) were not fully backfilled." in captured.err
    assert KRAKEN_HOST not in captured.err
    assert {row[0] for row in stored_history(sync_engine)} == {BTC}


def test_coinbase_failing_is_its_own_line_and_kraken_is_stored_regardless(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Coinbase Exchange 503s through every retry: one stderr line naming it, exit 1, and
    Kraken's closes on disk. Its host is in no stderr line; the transport's own log lines,
    on stdout, carry the host and label by design."""
    del migrated_cli_database
    fake = answering(Reply(renderer=three_days_and_today), coinbase=Reply(status=503))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert matching(BACKFILLED_LINE, captured.out) == [
        line for line in COMPLETE_LINES if COINBASE not in line
    ]
    assert matching(FAILED_LINE, captured.err) == [
        f"{BTC}/{USD} via {COINBASE} failed: ProviderUnavailableError"
    ]
    assert "1 of 2 pair(s) were not fully backfilled." in captured.err
    assert COINBASE_EXCHANGE_HOST not in captured.err
    assert {row[5] for row in stored_history(sync_engine)} == {KRAKEN}


def test_every_pair_failing_is_exit_one_and_stores_nothing(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Kraken throttled for both pairs on a fresh install: Coinbase has nothing to extend
    back from, so BTC carries two lines and the count is still two pairs."""
    del migrated_cli_database
    fake = answering(Reply(status=429))
    with_kraken(monkeypatch, fake)

    exit_code = cli.main(BACKFILL)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert matching(BACKFILLED_LINE, captured.out) == []
    assert matching(FAILED_LINE, captured.err) == [
        f"{BTC}/{USD} via {KRAKEN} failed: ProviderRateLimitedError",
        f"{BTC}/{USD} via {COINBASE} failed: NoRecentClose",
        f"{KAS}/{USD} via {KRAKEN} failed: ProviderRateLimitedError",
    ]
    assert "2 of 2 pair(s) were not fully backfilled." in captured.err
    assert fake.counts[COINBASE_EXCHANGE_HOST] == 0
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
    """One client built and closed -- shared by both sources -- and one engine built and
    disposed, by the command itself."""
    del migrated_cli_database
    fake = answering(Reply(renderer=three_days_and_today))
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
    fake = answering(Reply(renderer=three_days_and_today))
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
    fake = answering(Reply(renderer=three_days_and_today))
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
