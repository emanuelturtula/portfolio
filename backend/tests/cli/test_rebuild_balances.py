"""`rebuild-balances` (spec 038): every active wallet's past daily balances, now, by hand.

Called in process, as the other commands' suites call theirs. Two things are replaced: the
chain registry, through `stub_chain_providers`, with stubs that read a history, and
`cli.build_http_client`, with the offline client, so nothing can reach an index. Everything
else is real: the settings, the engine, the service, the repository and the file, read back
through a second connection.

**1 when any wallet was not rebuilt, 0 only when every one was**, for the reason
`refresh-prices` gives: a cron line or an operator reading only the code must not record a
good run for a rebuild that left a wallet's past unproven. **No address and no amount is
printed**: one line per wallet, by id and chain.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import text

from portfolio import cli
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.domain.chains import ChainKey
from portfolio.providers.base import AddressHistory, HistoryIncomplete, TxEffect
from portfolio.providers.errors import ProviderUnavailableError
from tests.balance_harness import (
    DEFAULT_BITCOIN_ADDRESS,
    DEFAULT_KASPA_ADDRESS,
    StubChainProvider,
    stub_chain_providers,
)
from tests.offline_http import offline_http_client

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from sqlalchemy import Engine

REBUILD: Final[list[str]] = ["rebuild-balances"]

#: Amounts no other part of the output could contain by accident.
RECEIVED: Final = 987_654_321

#: A rebuilt line on stdout, anchored end to end so a stray log line cannot satisfy it.
REBUILT_LINE: Final = re.compile(
    r"^wallet \d+ \([a-z]+\) rebuilt: (\d+ day\(s\) from \d{4}-\d{2}-\d{2}|no transaction)$"
)


class HistoryStub(StubChainProvider):
    """A stub that reads a history: `RECEIVED`, `days_ago` days back, unless told otherwise."""

    def __init__(
        self,
        chain_key: ChainKey,
        *,
        days_ago: int | None = 2,
        incomplete: HistoryIncomplete | None = None,
        history_raises: Exception | None = None,
    ) -> None:
        super().__init__(chain_key)
        self.days_ago = days_ago
        self.incomplete = incomplete
        self.history_raises = history_raises

    async def address_history(self, address: str) -> AddressHistory:
        if self.history_raises is not None:
            raise self.history_raises
        if self.days_ago is None:
            return AddressHistory(address, 0, 8, (), self.incomplete)
        received = datetime.now(UTC) - timedelta(days=self.days_ago)
        return AddressHistory(
            address=address,
            balance=RECEIVED,
            decimals=8,
            effects=(TxEffect(occurred_at=received, delta=RECEIVED),),
            incomplete=self.incomplete,
        )


@pytest.fixture
def migrated_cli_database(cli_database: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The CLI's database migrated to head, and the command's client the offline one."""
    cli_database.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(f"sqlite+aiosqlite:///{cli_database.as_posix()}")
    built: list[httpx.AsyncClient] = []

    def build() -> httpx.AsyncClient:
        client = offline_http_client()
        built.append(client)
        return client

    monkeypatch.setattr(cli, "build_http_client", build)
    return cli_database


def plant(sync_engine: Engine, *wallets: tuple[ChainKey, str]) -> list[int]:
    """One owner and these active wallets, in order. Returns their ids."""
    with sync_engine.begin() as connection:
        user_id: int = connection.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES ('owner', 'not-a-hash', '2026-01-01 00:00:00.000000') RETURNING id"
            )
        ).scalar_one()
        return [
            connection.execute(
                text(
                    "INSERT INTO wallets (user_id, chain_key, address_canonical, "
                    "address_display, created_at, updated_at) VALUES (:user_id, :chain, "
                    ":address, :address, '2026-01-01 00:00:00.000000', "
                    "'2026-01-01 00:00:00.000000') RETURNING id"
                ),
                {"user_id": user_id, "chain": chain.value, "address": address},
            ).scalar_one()
            for chain, address in wallets
        ]


def stored(sync_engine: Engine) -> list[tuple[int, str, int]]:
    with sync_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT wallet_id, day, confirmed FROM reconstructed_balances "
                "ORDER BY wallet_id, day"
            )
        ).all()
    return [(row[0], row[1], row[2]) for row in rows]


def test_a_complete_rebuild_prints_each_wallets_span_and_exits_zero(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One line per wallet, rows on disk, and no address or amount anywhere in the output."""
    del migrated_cli_database
    bitcoin, kaspa = plant(
        sync_engine,
        (ChainKey.BITCOIN, DEFAULT_BITCOIN_ADDRESS),
        (ChainKey.KASPA, DEFAULT_KASPA_ADDRESS),
    )
    stub_chain_providers(
        monkeypatch,
        {
            ChainKey.BITCOIN: HistoryStub(ChainKey.BITCOIN),
            ChainKey.KASPA: HistoryStub(ChainKey.KASPA, days_ago=None),
        },
    )
    first = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()

    exit_code = cli.main(REBUILD)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.splitlines() == [
        f"wallet {bitcoin} (bitcoin) rebuilt: 3 day(s) from {first}",
        f"wallet {kaspa} (kaspa) rebuilt: no transaction",
    ]
    assert all(REBUILT_LINE.match(line) for line in captured.out.splitlines())
    assert captured.err == ""
    for secret in (DEFAULT_BITCOIN_ADDRESS, DEFAULT_KASPA_ADDRESS, str(RECEIVED)):
        assert secret not in captured.out + captured.err
    rows = stored(sync_engine)
    assert [(row[0], row[2]) for row in rows] == [(bitcoin, RECEIVED)] * 3
    assert rows[0][1] == first


def test_a_wallet_not_rebuilt_is_named_with_its_reason_and_exits_one(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Incomplete, failed and unsupported each on stderr, then a count; nothing stored."""
    del migrated_cli_database
    unproven, failing = plant(
        sync_engine,
        (ChainKey.BITCOIN, DEFAULT_BITCOIN_ADDRESS),
        (ChainKey.KASPA, DEFAULT_KASPA_ADDRESS),
    )
    stub_chain_providers(
        monkeypatch,
        {
            ChainKey.BITCOIN: HistoryStub(
                ChainKey.BITCOIN, incomplete=HistoryIncomplete.MOVED_DURING_READ
            ),
            ChainKey.KASPA: HistoryStub(
                ChainKey.KASPA, history_raises=ProviderUnavailableError("down")
            ),
        },
    )

    exit_code = cli.main(REBUILD)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"wallet {unproven} (bitcoin) incomplete: moved_during_read",
        f"wallet {failing} (kaspa) failed: ProviderUnavailableError",
        "2 of 2 wallet(s) were not rebuilt.",
    ]
    assert stored(sync_engine) == []


def test_a_chain_that_cannot_read_a_history_is_unsupported(
    migrated_cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del migrated_cli_database
    (wallet,) = plant(sync_engine, (ChainKey.BITCOIN, DEFAULT_BITCOIN_ADDRESS))
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN)})

    assert cli.main(REBUILD) == 1

    assert capsys.readouterr().err.splitlines() == [
        f"wallet {wallet} (bitcoin) unsupported",
        "1 of 1 wallet(s) were not rebuilt.",
    ]


def test_no_active_wallet_is_said_and_is_not_a_failure(
    migrated_cli_database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del migrated_cli_database

    assert cli.main(REBUILD) == 0

    assert capsys.readouterr().out.splitlines() == ["No active wallet to rebuild."]
