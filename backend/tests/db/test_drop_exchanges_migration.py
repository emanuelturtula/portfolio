"""Migration `0012_drop_exchanges_accounting`: what goes, what stays, and the way back (spec 036).

Synchronous, like `test_migrations.py`, for the reason that module gives: Alembic's async
`env.py` calls `asyncio.run`. The schema is read back through a second, unconfigured engine,
so what is asserted is what is on disk.

## What is pinned

* **The upgrade over data.** At `0011_extended_keys` an owner has a wallet, a balance snapshot,
  a price, an exchange account with a fill, and a manual adjustment. After the upgrade the
  eleven tables and the two triggers are gone, and the owner, the wallet, the snapshot and the
  price are exactly as they were: nothing that stays is rebuilt or touched.
* **The downgrade recreates the schema, empty.** Every dropped table and both triggers come
  back, with nothing in them, and the triggers still refuse to change a fill. That emptiness is
  the point the operations guide makes: rolling back past `0012` is a restore, not a downgrade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.alembic_config import build_alembic_config
from portfolio.db.migrations.versions import v0012_drop_exchanges_accounting as drop
from tests.address_vectors import BIP173_TESTNET_P2WPKH

if TYPE_CHECKING:
    from sqlalchemy import Engine

REVISION: Final = "0012_drop_exchanges_accounting"
PARENT: Final = "0011_extended_keys"
AT: Final = "2026-10-01 10:00:00.000000"

DROPPED: Final = frozenset(
    {
        "exchange_accounts",
        "exchange_fills",
        "exchange_sync_windows",
        "exchange_sync_runs",
        "exchange_sync_run_accounts",
        "accounting_snapshots",
        "accounting_positions",
        "accounting_lots",
        "accounting_warnings",
        "manual_adjustments",
        "exchange_balances",
    }
)
TRIGGERS: Final = frozenset({"exchange_fills_no_update", "exchange_fills_no_delete"})

#: The rows that must come through the upgrade untouched, read whole.
KEPT_QUERIES: Final = {
    "users": "SELECT * FROM users ORDER BY id",
    "wallets": "SELECT * FROM wallets ORDER BY id",
    "sync_runs": "SELECT * FROM sync_runs ORDER BY id",
    "balance_snapshots": "SELECT * FROM balance_snapshots ORDER BY id",
    "prices": "SELECT * FROM prices ORDER BY id",
    "assets": "SELECT * FROM assets ORDER BY id",
}


def tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def triggers(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        return {str(row[0]) for row in rows}


def kept_rows(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    with engine.connect() as connection:
        return {
            table: [tuple(row) for row in connection.execute(text(sql))]
            for table, sql in KEPT_QUERIES.items()
        }


def plant_everything(engine: Engine) -> None:
    """One row in each kind of table, at the parent revision."""
    with engine.begin() as connection:
        user_id: int = connection.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES ('owner', 'not-a-hash', :at) RETURNING id"
            ),
            {"at": AT},
        ).scalar_one()
        wallet_id: int = connection.execute(
            text(
                "INSERT INTO wallets (user_id, chain_key, address_canonical, address_display, "
                "created_at, updated_at) VALUES (:user, 'bitcoin', :address, :address, :at, :at) "
                "RETURNING id"
            ),
            {"user": user_id, "address": BIP173_TESTNET_P2WPKH, "at": AT},
        ).scalar_one()
        run_id: int = connection.execute(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
                "wallets_total, wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', 'success', :at, :at, 1, 1, 1, 0) RETURNING id"
            ),
            {"at": AT},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO balance_snapshots "
                "(wallet_id, sync_run_id, confirmed, pending, decimals, observed_at) "
                "VALUES (:wallet, :run, 40000000, NULL, 8, :at)"
            ),
            {"wallet": wallet_id, "run": run_id, "at": AT},
        )
        connection.execute(
            text(
                "INSERT INTO prices (asset_id, quote_currency, amount, source, as_of, fetched_at) "
                "VALUES ((SELECT id FROM assets WHERE symbol = 'BTC'), 'USD', "
                "'60000.000000000000', 'coinbase', :at, :at)"
            ),
            {"at": AT},
        )
        account_id: int = connection.execute(
            text(
                "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
                "VALUES (:user, 'bitget', :at) RETURNING id"
            ),
            {"user": user_id, "at": AT},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, symbol, "
                "base_asset, quote_asset, side, quantity, price, quote_quantity, "
                "quote_quantity_derived, fee_amount, executed_at, raw_payload, ingested_at) "
                "VALUES (:account, '1001', 'BTCUSDT', 'BTC', 'USDT', 'buy', '1.0', '2.0', "
                "'2.0', 0, '0', :at, '{}', :at)"
            ),
            {"account": account_id, "at": AT},
        )
        connection.execute(
            text(
                "INSERT INTO manual_adjustments (user_id, asset, quantity, unit_cost, "
                "occurred_at, note, created_at, updated_at) "
                "VALUES (:user, 'BTC', '0.1', NULL, :at, 'opening balance', :at, :at)"
            ),
            {"user": user_id, "at": AT},
        )


def test_the_revision_names_what_it_drops() -> None:
    assert frozenset(drop.DROPPED_TABLES) == DROPPED
    assert len(drop.DROPPED_TABLES) == len(DROPPED)
    assert frozenset(drop.DROPPED_TRIGGERS) == TRIGGERS
    assert drop.revision == REVISION
    assert drop.down_revision == PARENT


def test_the_upgrade_drops_the_tables_and_triggers_and_keeps_everything_else(
    database_url: str, sync_engine: Engine
) -> None:
    config = build_alembic_config(database_url)
    command.upgrade(config, PARENT)
    plant_everything(sync_engine)
    assert tables(sync_engine) >= DROPPED
    assert triggers(sync_engine) == TRIGGERS
    before = kept_rows(sync_engine)

    command.upgrade(config, REVISION)

    assert tables(sync_engine).isdisjoint(DROPPED)
    assert triggers(sync_engine) == set()
    assert kept_rows(sync_engine) == before
    assert all(before.values())


def test_the_downgrade_recreates_every_table_empty_with_its_triggers(
    database_url: str, sync_engine: Engine
) -> None:
    config = build_alembic_config(database_url)
    command.upgrade(config, PARENT)
    plant_everything(sync_engine)
    command.upgrade(config, REVISION)

    command.downgrade(config, PARENT)

    assert tables(sync_engine) >= DROPPED
    assert triggers(sync_engine) == TRIGGERS
    with sync_engine.connect() as connection:
        for table in sorted(DROPPED):
            # One of eleven literal names above, never input.
            count = connection.scalar(text(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
            assert count == 0, table
        assert connection.scalar(text("SELECT COUNT(*) FROM wallets")) == 1


def test_the_recreated_triggers_still_refuse_to_change_a_fill(
    database_url: str, sync_engine: Engine
) -> None:
    """The downgrade runs `0007`'s own upgrade, so the append-only rule comes back with it."""
    config = build_alembic_config(database_url)
    command.upgrade(config, REVISION)
    command.downgrade(config, PARENT)
    plant_everything(sync_engine)

    with pytest.raises(IntegrityError), sync_engine.begin() as connection:
        connection.execute(text("UPDATE exchange_fills SET quantity = '2.0'"))
    with pytest.raises(IntegrityError), sync_engine.begin() as connection:
        connection.execute(text("DELETE FROM exchange_fills"))
