"""Criteria 6 and 7 of #104, over HTTP: `GET /api/accounting/reconciliation` (spec 025).

The whole stack runs -- middleware, router, the reconciliation service, the real recompute
trigger, SQLite. The history is fills planted through the application's own insert; the
wallet readings are rows as the balance sync leaves them; the venue readings are written by
`ExchangeBalanceRepository.replace` or, in the end-to-end tests, by the real exchange sync
through `POST /api/exchanges/sync` against a simulated venue.

## What is pinned

* **The shape**, key for key, against the spec's own example document -- whose BTC row is
  reproduced figure for figure.
* **Every quantity is a JSON string**, asserted on the raw response text: `response.json()`
  cannot tell `"0.5"` from `0.5` once a test compares it with a `Decimal`.
* **`401` without a session**, and the public-path allowlist exactly as it was.
* **No snapshot is `computed_at: null` with an empty `assets`** -- "not computed", never
  "every balance is unaccounted for" -- while `exchanges` and `wallets` are still answered.
* **The operation is in the OpenAPI document** under `readReconciliation`, with every
  quantity declared a string.
* **No request reaches a venue** (criterion 7): the simulated venue counts its calls, and a
  `GET` adds none.
* **A failed balance read shows here and nowhere else**: the sync's own response still says
  success, and this endpoint names the venue, the kind, when it was last read, and that its
  reading is no longer compared.
* **R9: a reading is compared only while it is current.** A venue whose last read failed,
  whose fill sync is not `ok`, or whose reading is more than a day old is listed with its
  `not_compared_reason` and adds nothing; a wallet read more than a day ago is counted
  `stale` and adds nothing. `last_recompute` is served as the positions endpoint serves it.

## Time

The endpoint measures a reading's age against the real clock, so every instant planted here
is taken from the real clock too: a reading "six minutes old" is six minutes before the test
ran. A test written against fixed dates would pass on the day it was written and fail the
next, when its readings turned a day old.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.api.middleware import PUBLIC_API_PATHS
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import ExchangeKey
from portfolio.main import run_accounting_recompute
from portfolio.providers.exchanges.errors import (
    ExchangeAuthError,
    ExchangeUnavailableError,
)
from portfolio.repositories.exchange_sync_runs import ExchangeSyncErrorKind
from portfolio.services.accounting import RecomputeReason
from tests.accounting_harness import plant_unconvertible_fill
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WSH, KASPA_TESTNET_V0
from tests.api.test_accounting import application, owner_id, plant, recompute
from tests.auth.conftest import BASE_URL, JSON_HEADERS
from tests.balance_harness import insert_wallet
from tests.exchange_sync_harness import SimulatedVenue, always_balances, held, make_fill
from tests.services.test_reconciliation_service import (
    buy,
    fail_balances,
    history_fills,
    plant_reading,
    set_sync_status,
    store_balances,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastapi import FastAPI

RECONCILIATION: Final = "/api/accounting/reconciliation"
POSITIONS: Final = "/api/accounting/positions"

TOP_LEVEL_FIELDS: Final = {
    "computed_at",
    "tolerance_pct",
    "assets",
    "max_reading_age_hours",
    "last_recompute",
    "exchanges",
    "wallets",
}
ASSET_FIELDS: Final = {
    "asset",
    "history_quantity",
    "wallet_quantity",
    "exchange_quantity",
    "held_quantity",
    "difference",
    "status",
}
EXCHANGE_FIELDS: Final = {
    "exchange_key",
    "balances_read_at",
    "balances_error",
    "not_compared_reason",
}
WALLETS_FIELDS: Final = {"compared", "stale", "unread", "oldest_observed_at"}
LAST_RECOMPUTE_FIELDS: Final = {"at", "outcome", "error"}

#: Every property of an asset row that carries a quantity. Each must be a JSON string.
QUANTITY_FIELDS: Final = (
    "history_quantity",
    "wallet_quantity",
    "exchange_quantity",
    "held_quantity",
    "difference",
)
EIGHTEEN_PLACES: Final = re.compile(r"-?\d+\.\d{18}")

#: The nine kinds a balance read can fail with, written out rather than read off the enum.
ERROR_KINDS: Final = [
    "auth",
    "conflict",
    "insufficient_scope",
    "internal",
    "invalid_request",
    "rate_limited",
    "retention_window",
    "schema",
    "unavailable",
]
#: R9's four reasons a venue's reading is not compared, in the order they are tested.
NOT_COMPARED_REASONS: Final = ["read_failed", "never_read", "sync_failed", "out_of_date"]

#: Distinctive, so their absence means something. Synthetic.
MARKED_ASSET: Final = "ZZMARKED"
MARKED_AMOUNT: Final = "424242.424242424242"


class Readings:
    """The instants a test plants its readings at, minutes before the real clock's now."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.bitget = self.now - timedelta(minutes=6)
        self.bingx = self.now - timedelta(minutes=6, seconds=30)
        self.oldest = self.now - timedelta(minutes=20)
        self.newer = self.now - timedelta(minutes=15)


def wire(moment: datetime) -> str:
    """A whole-second instant as the API writes it: ISO 8601 in UTC, ending in `Z`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------
# Planting, and reading the answer
# --------------------------------------------------------------------------------------


async def reconciliation(client: AsyncClient) -> tuple[dict[str, Any], str]:
    """The parsed body and the raw text it was parsed from."""
    response = await client.get(RECONCILIATION)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    body: dict[str, Any] = response.json()
    return body, response.text


def by_asset(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["asset"]: entry for entry in body["assets"]}


def by_key(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["exchange_key"]: entry for entry in body["exchanges"]}


async def account_id(app: FastAPI, key: ExchangeKey) -> int:
    async with app.state.db_sessionmaker() as session:
        found = await session.scalar(
            text("SELECT id FROM exchange_accounts WHERE exchange_key = :key"), {"key": str(key)}
        )
    assert found is not None, f"no {key} account was planted"
    return int(found)


async def plant_synced(app: FastAPI, fills: dict[ExchangeKey, list[Any]]) -> None:
    """The owner's accounts and fills, each account `ok` as a successful fill sync leaves it.

    Balances are only ever read after a successful fill sync, and R9 compares a venue's
    reading only while its fill sync is `ok`.
    """
    await plant(app, fills)
    for key in fills:
        await set_sync_status(app.state.db_sessionmaker, await account_id(app, key), "ok")


async def add_wallet(app: FastAPI, chain: ChainKey, address: str) -> int:
    user_id = await owner_id(app)
    async with app.state.db_sessionmaker() as session:
        return await insert_wallet(session, user_id=user_id, chain_key=chain, address=address)


async def plant_the_specs_example(app: FastAPI, readings: Readings) -> None:
    """The spec's BTC row, and the rest of the service test's scenario around it.

    History 0.5 BTC, 2 ETH, 1000 KAS (DOGE sold out). Wallets 0.4 + 0.3 BTC and 600 KAS.
    Bitget 0.2 BTC, 0.5 ETH, 395 KAS, 5000 USDT; BingX 0.1 BTC, 3 SOL, 250 USDT. Every
    reading is minutes old.
    """
    await plant_synced(app, {ExchangeKey.BITGET: history_fills(), ExchangeKey.BINGX: []})
    factory = app.state.db_sessionmaker
    first = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    second = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    kaspa = await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(factory, wallet_id=first, confirmed=40_000_000, observed_at=readings.newer)
    await plant_reading(
        factory, wallet_id=second, confirmed=30_000_000, observed_at=readings.oldest
    )
    await plant_reading(
        factory, wallet_id=kaspa, confirmed=60_000_000_000, observed_at=readings.newer
    )
    await store_balances(
        factory,
        await account_id(app, ExchangeKey.BITGET),
        (held("BTC", "0.2"), held("ETH", "0.5"), held("KAS", "395"), held("USDT", "5000")),
        readings.bitget,
    )
    await store_balances(
        factory,
        await account_id(app, ExchangeKey.BINGX),
        (held("BTC", "0.1"), held("SOL", "3"), held("USDT", "250")),
        readings.bingx,
    )
    await recompute(app)


async def stored_computed_at(app: FastAPI) -> datetime:
    async with app.state.db_sessionmaker() as session:
        found = await session.scalar(text("SELECT computed_at FROM accounting_snapshots"))
    return datetime.fromisoformat(str(found)).replace(tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Authentication, the allowlist, the schema
# --------------------------------------------------------------------------------------


async def test_the_reconciliation_requires_a_session(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct `401` without a cookie, as the problem document every refusal is.

    The balances are planted first, so a body that leaked them would have something to leak.
    """
    del api_environment
    async with application(monkeypatch) as (app, _client):
        await plant_the_specs_example(app, Readings())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(RECONCILIATION)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "assets" not in response.json()
    assert "BTC" not in response.text
    assert "exchange_quantity" not in response.text


def test_the_public_allowlist_is_unchanged() -> None:
    """The endpoint is protected by being absent from the allowlist, which did not grow."""
    assert frozenset({"/api/health", "/api/auth/login"}) == PUBLIC_API_PATHS
    assert RECONCILIATION not in PUBLIC_API_PATHS
    assert not any("reconciliation" in path for path in PUBLIC_API_PATHS)


def test_the_operation_is_in_the_schema_under_its_operation_id(app: FastAPI) -> None:
    schema = app.openapi()
    operation = schema["paths"][RECONCILIATION]

    assert set(operation) == {"get"}, "one operation, a read"
    assert operation["get"]["operationId"] == "readReconciliation"
    assert operation["get"].get("parameters", []) == [], "it takes no parameters"
    assert "requestBody" not in operation["get"]
    answer = operation["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert answer == {"$ref": "#/components/schemas/ReconciliationResponse"}
    assert "accounting" in operation["get"]["tags"]


def test_the_schema_declares_the_specs_shape_and_every_quantity_a_string(app: FastAPI) -> None:
    """What the generated TypeScript client is built from: a quantity typed `number` there
    compiles perfectly and is already inexact by the time any code runs."""
    schemas = app.openapi()["components"]["schemas"]
    top = schemas["ReconciliationResponse"]
    row = schemas["AssetReconciliationResponse"]
    exchange = schemas["ExchangeBalancesResponse"]
    wallets = schemas["WalletsReadResponse"]

    assert set(top["properties"]) == TOP_LEVEL_FIELDS
    assert set(top["required"]) == TOP_LEVEL_FIELDS
    assert top["properties"]["tolerance_pct"]["type"] == "string"
    assert top["properties"]["max_reading_age_hours"]["type"] == "integer"
    assert {option.get("type") for option in top["properties"]["computed_at"]["anyOf"]} == {
        "string",
        "null",
    }
    assert {"$ref": "#/components/schemas/LastRecomputeResponse"} in top["properties"][
        "last_recompute"
    ]["anyOf"]
    assert set(row["properties"]) == ASSET_FIELDS
    assert set(row["required"]) == ASSET_FIELDS
    for field in QUANTITY_FIELDS:
        assert row["properties"][field]["type"] == "string", field
    assert row["properties"]["asset"]["type"] == "string"
    assert schemas["ReconciliationStatus"]["enum"] == ["match", "history_short", "history_over"]
    assert row["properties"]["status"] == {"$ref": "#/components/schemas/ReconciliationStatus"}
    assert set(exchange["properties"]) == EXCHANGE_FIELDS
    assert set(exchange["required"]) == EXCHANGE_FIELDS
    assert sorted(schemas["ExchangeSyncErrorKind"]["enum"]) == ERROR_KINDS
    assert {"$ref": "#/components/schemas/ExchangeSyncErrorKind"} in exchange["properties"][
        "balances_error"
    ]["anyOf"]
    assert schemas["NotComparedReason"]["enum"] == NOT_COMPARED_REASONS
    assert {"$ref": "#/components/schemas/NotComparedReason"} in exchange["properties"][
        "not_compared_reason"
    ]["anyOf"]
    assert set(wallets["properties"]) == WALLETS_FIELDS
    assert set(wallets["required"]) == WALLETS_FIELDS
    for count in ("compared", "stale", "unread"):
        assert wallets["properties"][count]["type"] == "integer", count


# --------------------------------------------------------------------------------------
# The shape and the figures
# --------------------------------------------------------------------------------------


async def test_the_specs_example_is_served_figure_for_figure(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 025's own document: 0.5 BTC in the history, 0.7 in wallets, 0.3 on exchanges."""
    del api_environment
    readings = Readings()
    async with application(monkeypatch) as (app, client):
        await plant_the_specs_example(app, readings)
        body, _raw = await reconciliation(client)
        computed_at = await stored_computed_at(app)

    assert set(body) == TOP_LEVEL_FIELDS
    assert body["tolerance_pct"] == "1"
    assert body["max_reading_age_hours"] == 24
    assert datetime.fromisoformat(body["computed_at"]) == computed_at
    assert body["computed_at"].endswith("Z")
    assert [entry["asset"] for entry in body["assets"]] == ["BTC", "ETH", "KAS", "SOL"]
    assert by_asset(body)["BTC"] == {
        "asset": "BTC",
        "history_quantity": "0.500000000000000000",
        "wallet_quantity": "0.700000000000000000",
        "exchange_quantity": "0.300000000000000000",
        "held_quantity": "1.000000000000000000",
        "difference": "0.500000000000000000",
        "status": "history_short",
    }
    assert body["exchanges"] == [
        {
            "exchange_key": "bingx",
            "balances_read_at": wire(readings.bingx),
            "balances_error": None,
            "not_compared_reason": None,
        },
        {
            "exchange_key": "bitget",
            "balances_read_at": wire(readings.bitget),
            "balances_error": None,
            "not_compared_reason": None,
        },
    ]
    assert body["wallets"] == {
        "compared": 3,
        "stale": 0,
        "unread": 0,
        "oldest_observed_at": wire(readings.oldest),
    }


async def test_every_status_and_a_negative_difference_are_served(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ETH: 2 in the history, 0.5 held, a difference of -1.5 with its sign. KAS: 1000 against
    995, half a percent, a match that still shows its -5. SOL: held with no history."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_specs_example(app, Readings())
        body, _raw = await reconciliation(client)

    rows = by_asset(body)
    assert rows["ETH"] == {
        "asset": "ETH",
        "history_quantity": "2.000000000000000000",
        "wallet_quantity": "0.000000000000000000",
        "exchange_quantity": "0.500000000000000000",
        "held_quantity": "0.500000000000000000",
        "difference": "-1.500000000000000000",
        "status": "history_over",
    }
    assert rows["KAS"] == {
        "asset": "KAS",
        "history_quantity": "1000.000000000000000000",
        "wallet_quantity": "600.000000000000000000",
        "exchange_quantity": "395.000000000000000000",
        "held_quantity": "995.000000000000000000",
        "difference": "-5.000000000000000000",
        "status": "match",
    }
    assert rows["SOL"]["status"] == "history_short"
    assert rows["SOL"]["history_quantity"] == "0.000000000000000000"
    assert "USDT" not in rows, "a cash asset is never a row"
    assert "DOGE" not in rows, "nothing in the history and nothing held is not a row"


async def test_every_quantity_is_a_json_string_at_eighteen_places(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on the parsed types and on the raw text, where the quotation marks are."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_specs_example(app, Readings())
        body, raw = await reconciliation(client)

    assert len(body["assets"]) == 4
    for entry in body["assets"]:
        assert set(entry) == ASSET_FIELDS
        for field in QUANTITY_FIELDS:
            value = entry[field]
            assert isinstance(value, str), f"{entry['asset']}.{field} is {type(value).__name__}"
            assert EIGHTEEN_PLACES.fullmatch(value), f"{entry['asset']}.{field} = {value}"
    assert isinstance(body["tolerance_pct"], str)
    for field in (*QUANTITY_FIELDS, "tolerance_pct"):
        assert f'"{field}":"' in raw, field
        assert not re.search(rf'"{field}":\s*[-0-9]', raw), f"{field} is served as a JSON number"
    numbers = re.findall(r'"([a-z_]+)":\s*(-?\d[\d.eE+-]*)', raw)
    assert sorted(name for name, _value in numbers) == [
        "compared",
        "max_reading_age_hours",
        "stale",
        "unread",
    ], "the only JSON numbers in the document are three wallet counts and the age limit"
    assert Decimal(by_asset(body)["BTC"]["held_quantity"]) == Decimal(1)


async def test_an_exact_quantity_past_a_doubles_precision_is_served_digit_for_digit(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """38 digits at each venue, 39 in their sum, none lost on the way to the wire."""
    del api_environment
    amount = "60000000000000000000.000000000000000001"
    readings = Readings()
    async with application(monkeypatch) as (app, client):
        await plant_synced(app, {ExchangeKey.BITGET: [], ExchangeKey.BINGX: []})
        for key, read_at in (
            (ExchangeKey.BITGET, readings.bitget),
            (ExchangeKey.BINGX, readings.bingx),
        ):
            await store_balances(
                app.state.db_sessionmaker,
                await account_id(app, key),
                (held("KAS", amount),),
                read_at,
            )
        body, _raw = await reconciliation(client)

    (row,) = body["assets"]
    assert row["exchange_quantity"] == "120000000000000000000.000000000000000002"
    assert row["held_quantity"] == "120000000000000000000.000000000000000002"
    assert row["difference"] == "120000000000000000000.000000000000000002"
    assert row["status"] == "history_short"


# --------------------------------------------------------------------------------------
# No snapshot, nothing at all, and the last recompute
# --------------------------------------------------------------------------------------


async def test_no_snapshot_is_a_200_with_a_null_timestamp_and_no_assets(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Not computed", never "every balance is unaccounted for".

    The balances are all there. Without a snapshot to compare them with, the answer is an
    empty `assets` and a `null` `computed_at` -- and the sources are still described.
    """
    del api_environment
    readings = Readings()
    async with application(monkeypatch) as (app, client):
        await plant_the_specs_example(app, readings)
        async with app.state.db_sessionmaker() as session:
            await session.execute(text("DELETE FROM accounting_snapshots"))
            await session.commit()
        body, raw = await reconciliation(client)

    assert body["computed_at"] is None
    assert '"computed_at":null' in raw
    assert body["assets"] == []
    assert body["tolerance_pct"] == "1"
    assert body["max_reading_age_hours"] == 24
    assert [entry["exchange_key"] for entry in body["exchanges"]] == ["bingx", "bitget"]
    assert by_key(body)["bitget"]["balances_read_at"] == wire(readings.bitget)
    assert [entry["not_compared_reason"] for entry in body["exchanges"]] == [None, None]
    assert body["wallets"] == {
        "compared": 3,
        "stale": 0,
        "unread": 0,
        "oldest_observed_at": wire(readings.oldest),
    }
    assert "history_short" not in raw


async def test_an_owner_with_nothing_yet_gets_an_empty_comparison(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No venue, no wallet, no fill: the startup recompute's empty snapshot, and nothing held."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        body, _raw = await reconciliation(client)
        computed_at = await stored_computed_at(app)

    assert datetime.fromisoformat(body.pop("computed_at")) == computed_at
    last = body.pop("last_recompute")
    assert set(last) == LAST_RECOMPUTE_FIELDS
    assert last["error"] is None
    assert body == {
        "tolerance_pct": "1",
        "max_reading_age_hours": 24,
        "assets": [],
        "exchanges": [],
        "wallets": {"compared": 0, "stale": 0, "unread": 0, "oldest_observed_at": None},
    }


async def test_the_last_recompute_is_what_the_positions_endpoint_serves(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One status, kept in one place, served under one name by both endpoints."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_specs_example(app, Readings())
        body, _raw = await reconciliation(client)
        positions = (await client.get(POSITIONS)).json()

    assert set(body["last_recompute"]) == LAST_RECOMPUTE_FIELDS
    assert body["last_recompute"] == positions["last_recompute"]
    assert body["last_recompute"]["outcome"] == "written"
    assert body["last_recompute"]["error"] is None


async def test_a_failed_recompute_is_served_beside_the_comparison_it_made_stale(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history could not be recomputed, so the snapshot is older than the balances.

    An asset bought since would show as held with no history. The endpoint says so --
    `outcome: failed` and the exception's class name, never its message -- and still serves
    the comparison of the snapshot it has, with that snapshot's own `computed_at`: what to
    show the owner is the dashboard's decision, and it needs both to make it.
    """
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_specs_example(app, Readings())
        before, _raw = await reconciliation(client)
        async with app.state.db_sessionmaker() as session:
            await plant_unconvertible_fill(
                session,
                await account_id(app, ExchangeKey.BITGET),
                trade_id="tid-UNCV-same-asset",
                shape="same_asset",
            )
        failed = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
        after, raw = await reconciliation(client)

    assert failed.error == "UnconvertibleFillError"
    assert after["last_recompute"]["outcome"] == "failed"
    assert after["last_recompute"]["error"] == "UnconvertibleFillError"
    assert "tid-UNCV" not in raw
    assert after["computed_at"] == before["computed_at"], "the snapshot is the one before"
    assert after["assets"] == before["assets"]


# --------------------------------------------------------------------------------------
# Sources that are missing
# --------------------------------------------------------------------------------------


async def test_an_unread_wallet_is_counted_and_adds_nothing(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    readings = Readings()
    async with application(monkeypatch) as (app, client):
        await plant_synced(app, {ExchangeKey.BITGET: [buy(1001, 0, "BTC", "0.5", "30000")]})
        read = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
        await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
        await plant_reading(
            app.state.db_sessionmaker,
            wallet_id=read,
            confirmed=50_000_000,
            observed_at=readings.newer,
        )
        await recompute(app)
        body, _raw = await reconciliation(client)

    assert body["wallets"] == {
        "compared": 1,
        "stale": 0,
        "unread": 1,
        "oldest_observed_at": wire(readings.newer),
    }
    (btc,) = body["assets"]
    assert btc["wallet_quantity"] == "0.500000000000000000"
    assert btc["status"] == "match"


async def test_a_wallet_read_more_than_a_day_ago_is_counted_stale_and_adds_nothing(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9: 9 BTC in a reading two days old is not 9 BTC held. It is a wallet whose chain has
    stopped answering, and its coins may have moved since."""
    del api_environment
    readings = Readings()
    async with application(monkeypatch) as (app, client):
        await plant_synced(app, {ExchangeKey.BITGET: [buy(1001, 0, "BTC", "0.5", "30000")]})
        current = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
        stale = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
        factory = app.state.db_sessionmaker
        await plant_reading(
            factory, wallet_id=current, confirmed=50_000_000, observed_at=readings.newer
        )
        await plant_reading(
            factory,
            wallet_id=stale,
            confirmed=900_000_000,
            observed_at=readings.now - timedelta(days=2),
        )
        await recompute(app)
        body, _raw = await reconciliation(client)

    assert body["wallets"] == {
        "compared": 1,
        "stale": 1,
        "unread": 0,
        "oldest_observed_at": wire(readings.newer),
    }
    (btc,) = body["assets"]
    assert btc["wallet_quantity"] == "0.500000000000000000"
    assert btc["status"] == "match"


async def test_a_failed_read_is_named_with_its_kind_and_its_reading_is_left_out(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bitget's last attempt failed and BingX's never succeeded: each says which it is, and
    Bitget's stored 0.2 BTC is no longer part of what is held (R9)."""
    del api_environment
    kinds = {
        ExchangeKey.BITGET: ExchangeSyncErrorKind.UNAVAILABLE,
        ExchangeKey.BINGX: ExchangeSyncErrorKind.INSUFFICIENT_SCOPE,
    }
    readings = Readings()
    async with application(monkeypatch) as (app, client):
        await plant_synced(app, {ExchangeKey.BITGET: history_fills(), ExchangeKey.BINGX: []})
        factory = app.state.db_sessionmaker
        await store_balances(
            factory,
            await account_id(app, ExchangeKey.BITGET),
            (held("BTC", "0.2"),),
            readings.bitget,
        )
        for key, kind in kinds.items():
            await fail_balances(factory, await account_id(app, key), kind)
        await recompute(app)
        body, _raw = await reconciliation(client)

    assert body["exchanges"] == [
        {
            "exchange_key": "bingx",
            "balances_read_at": None,
            "balances_error": "insufficient_scope",
            "not_compared_reason": "read_failed",
        },
        {
            "exchange_key": "bitget",
            "balances_read_at": wire(readings.bitget),
            "balances_error": "unavailable",
            "not_compared_reason": "read_failed",
        },
    ]
    btc = by_asset(body)["BTC"]
    assert btc["exchange_quantity"] == "0.000000000000000000"
    assert btc["status"] == "history_over", "left out, the venue can only hide a finding"


async def test_each_reason_a_venue_is_not_compared_is_served(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BingX read 25 hours ago is `out_of_date`; Bitget, read minutes ago, whose fill sync
    has since failed is `sync_failed`. Neither has an error on its balances, and neither's
    rows are summed."""
    del api_environment
    readings = Readings()
    long_ago = readings.now - timedelta(hours=25)
    async with application(monkeypatch) as (app, client):
        await plant_synced(app, {ExchangeKey.BITGET: history_fills(), ExchangeKey.BINGX: []})
        factory = app.state.db_sessionmaker
        bitget = await account_id(app, ExchangeKey.BITGET)
        await store_balances(factory, bitget, (held("BTC", "0.2"),), readings.bitget)
        await store_balances(
            factory, await account_id(app, ExchangeKey.BINGX), (held("BTC", "0.1"),), long_ago
        )
        await recompute(app)
        compared, _raw = await reconciliation(client)
        await set_sync_status(factory, bitget, "error")
        body, _raw = await reconciliation(client)

    assert by_key(compared)["bitget"]["not_compared_reason"] is None
    assert by_asset(compared)["BTC"]["exchange_quantity"] == "0.200000000000000000"
    assert body["exchanges"] == [
        {
            "exchange_key": "bingx",
            "balances_read_at": wire(long_ago),
            "balances_error": None,
            "not_compared_reason": "out_of_date",
        },
        {
            "exchange_key": "bitget",
            "balances_read_at": wire(readings.bitget),
            "balances_error": None,
            "not_compared_reason": "sync_failed",
        },
    ]
    assert by_asset(body)["BTC"]["exchange_quantity"] == "0.000000000000000000"


# --------------------------------------------------------------------------------------
# End to end with the real sync, and criterion 7: no request reaches a venue
# --------------------------------------------------------------------------------------


def a_venue(now: datetime) -> SimulatedVenue:
    """Four buys of 0.5 BTC, each with a 0.0005 BTC fee: 1.998 BTC in the history."""
    return SimulatedVenue(
        [make_fill(5001 + n, now - timedelta(minutes=10 - n), quantity="0.5") for n in range(4)],
        balances=(held("BTC", "1.998"), held("KAS", "250"), held("USDT", "100")),
    )


async def test_a_manual_sync_reads_the_balances_and_the_endpoint_compares_them(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POST /api/exchanges/sync`, then one `GET`: the fills are the history, the venue's
    balances are the held side, and the read is stamped."""
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    venue = a_venue(now)
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        before, _raw = await reconciliation(client)
        response = await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"
        body, _raw = await reconciliation(client)

    assert before["assets"] == []
    assert before["exchanges"] == [], "an account row exists only once a sync has run"
    assert venue.balance_calls == 1
    rows = by_asset(body)
    assert rows["BTC"] == {
        "asset": "BTC",
        "history_quantity": "1.998000000000000000",
        "wallet_quantity": "0.000000000000000000",
        "exchange_quantity": "1.998000000000000000",
        "held_quantity": "1.998000000000000000",
        "difference": "0.000000000000000000",
        "status": "match",
    }
    assert rows["KAS"]["status"] == "history_short"
    assert rows["KAS"]["difference"] == "250.000000000000000000"
    assert set(rows) == {"BTC", "KAS"}
    (source,) = body["exchanges"]
    assert source["exchange_key"] == "bitget"
    assert source["balances_error"] is None
    assert source["not_compared_reason"] is None
    assert datetime.fromisoformat(source["balances_read_at"]) >= now
    assert body["last_recompute"]["outcome"] == "written"


async def test_a_failed_balance_read_shows_here_while_the_sync_reports_success(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criteria 3 and 4 end to end, through the two endpoints the owner actually uses.

    The first manual sync reads the balances. The second finds the venue refusing the key
    for the balance read alone: the sync's answer is still a success with no error on the
    account, and this endpoint says `auth` beside the reading it still has -- and no longer
    counts that reading as held (R9). A third manual sync, the refusal gone, asks again -- a
    manual sync retries a refused key -- and the venue is compared again.
    """
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    venue = a_venue(now)
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
        good, _raw = await reconciliation(client)

        venue.balance_fault = always_balances(ExchangeAuthError(status=401))
        venue.balances = [held("BTC", "9")]
        failed = await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
        exchanges = (await client.get("/api/exchanges")).json()
        after_failure, _raw = await reconciliation(client)

        venue.balance_fault = None
        retried = await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
        after_retry, _raw = await reconciliation(client)

    assert by_asset(good)["BTC"]["exchange_quantity"] == "1.998000000000000000"
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "success"
    assert (failed.json()["accounts_succeeded"], failed.json()["accounts_failed"]) == (1, 0)
    assert "auth" not in failed.text, "the sync's answer says nothing of the balance read"
    assert "auth_failed" not in repr(exchanges), "the account is not auth_failed: its fills read"
    (source,) = after_failure["exchanges"]
    assert source["balances_error"] == "auth"
    assert source["not_compared_reason"] == "read_failed"
    assert source["balances_read_at"] == good["exchanges"][0]["balances_read_at"], (
        "the last good reading is kept, and says how old it is"
    )
    btc = by_asset(after_failure)["BTC"]
    assert btc["exchange_quantity"] == "0.000000000000000000", "a kept reading is not compared"
    assert btc["status"] == "history_over"
    assert retried.json()["status"] == "success"
    assert venue.balance_calls == 3, "a manual sync asks again after a refused key"
    (cleared,) = after_retry["exchanges"]
    assert cleared["balances_error"] is None
    assert cleared["not_compared_reason"] is None
    assert by_asset(after_retry)["BTC"]["exchange_quantity"] == "9.000000000000000000"


async def test_reading_the_reconciliation_never_calls_a_venue(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 7: the endpoint serves what the sync stored. Both venues are configured,
    one of them failing, and five requests later neither has been asked for anything."""
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    venues = {
        ExchangeKey.BITGET: a_venue(now),
        ExchangeKey.BINGX: SimulatedVenue(
            exchange_key=ExchangeKey.BINGX,
            balances=(held(MARKED_ASSET, MARKED_AMOUNT),),
            balance_fault=always_balances(ExchangeUnavailableError(status=503)),
        ),
    }
    async with application(monkeypatch, venues) as (_app, client):
        for _ in range(5):
            body, _raw = await reconciliation(client)

    assert [venue.balance_calls for venue in venues.values()] == [0, 0]
    assert [venue.calls for venue in venues.values()] == [[], []]
    assert [venue.symbol_calls for venue in venues.values()] == [0, 0]
    assert body["assets"] == []
    assert body["exchanges"] == [], "no sync ran, so no account row was ever created"
