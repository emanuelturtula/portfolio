"""`GET /api/portfolio/summary` over HTTP, the whole stack.

Middleware, router, service, domain and SQLite all run. The wallet readings are planted as the
balance sync leaves them, and the prices as the refresh stores them.

The scenario every figure below is worked out from:

* **Held**: a Bitcoin wallet with 0.4 BTC and a Kaspa wallet with 6000 KAS.
* **Prices**: BTC 60000 and KAS 0.1, minutes old.

So the BTC is worth 24000 and the KAS 600: a total of 24600, of which BTC is 97.5610 % and
KAS 2.4390 %.

Every instant is taken from the real clock: the endpoint measures ages against it.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.api.middleware import PUBLIC_API_PATHS
from portfolio.domain.chains import ChainKey
from portfolio.main import create_app
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
)
from tests.auth.conftest import BASE_URL, sign_in
from tests.balance_harness import insert_wallet, sqlite_timestamp
from tests.price_harness import plant_price

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

SUMMARY: Final = "/api/portfolio/summary"

TOP_LEVEL_FIELDS: Final = {"total_value", "holdings", "missing"}
HOLDING_FIELDS: Final = {"asset", "quantity", "price", "value", "share_pct"}


@asynccontextmanager
async def application() -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """The real application, its lifespan run, signed in as the owner."""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            yield app, client


def _ago(*, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    return (datetime.now(UTC) - delta).replace(microsecond=0)


def dec(value: object) -> Decimal:
    assert isinstance(value, str), f"{value!r} is not a JSON string"
    return Decimal(value)


async def add_wallet(app: FastAPI, chain: ChainKey, address: str) -> int:
    async with app.state.db_sessionmaker() as session:
        owner = await session.scalar(text("SELECT id FROM users WHERE username = 'owner'"))
        return await insert_wallet(session, user_id=int(owner), chain_key=chain, address=address)


async def plant_reading(app: FastAPI, *, wallet_id: int, confirmed: int, at: datetime) -> None:
    """One `balance_snapshots` row under a finished run of its own, as the sync leaves it."""
    async with app.state.db_sessionmaker() as session:
        run_id = await session.scalar(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
                "wallets_total, wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', 'success', :at, :at, 1, 1, 1, 0) RETURNING id"
            ),
            {"at": sqlite_timestamp(at)},
        )
        await session.execute(
            text(
                "INSERT INTO balance_snapshots "
                "(wallet_id, sync_run_id, confirmed, pending, decimals, observed_at) "
                "VALUES (:wallet, :run, :confirmed, NULL, 8, :at)"
            ),
            {
                "wallet": wallet_id,
                "run": run_id,
                "confirmed": confirmed,
                "at": sqlite_timestamp(at),
            },
        )
        await session.commit()


async def price(app: FastAPI, symbol: str, amount: str, *, age: timedelta) -> None:
    """A USD price row `age` old against the real clock the endpoint reads."""
    async with app.state.db_sessionmaker() as session:
        await plant_price(session, symbol=symbol, amount=Decimal(amount), as_of=_ago() - age)


async def plant_the_scenario(app: FastAPI, *, kas_price: bool = True) -> None:
    bitcoin = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    kaspa = await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(app, wallet_id=bitcoin, confirmed=40_000_000, at=_ago(minutes=5))
    await plant_reading(app, wallet_id=kaspa, confirmed=600_000_000_000, at=_ago(minutes=5))
    await price(app, "BTC", "60000", age=timedelta(minutes=3))
    if kas_price:
        await price(app, "KAS", "0.1", age=timedelta(minutes=3))


async def summary(client: AsyncClient) -> tuple[dict[str, Any], str]:
    response = await client.get(SUMMARY)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body, response.text


# --------------------------------------------------------------------------------------
# Authentication and the allowlist
# --------------------------------------------------------------------------------------


async def test_the_summary_requires_a_session(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, _client):
        await plant_the_scenario(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(SUMMARY)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "holdings" not in response.text


def test_the_public_allowlist_is_unchanged() -> None:
    assert frozenset({"/api/health", "/api/auth/login"}) == PUBLIC_API_PATHS
    assert not any("portfolio" in path for path in PUBLIC_API_PATHS)


# --------------------------------------------------------------------------------------
# The figures
# --------------------------------------------------------------------------------------


async def test_the_figures_of_the_scenario(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        body, _ = await summary(client)

    assert set(body) == TOP_LEVEL_FIELDS
    assert dec(body["total_value"]) == Decimal(24600)
    assert body["missing"] == []

    assert [set(holding) for holding in body["holdings"]] == [HOLDING_FIELDS] * 2
    btc, kas = body["holdings"]
    assert btc["asset"] == "BTC"
    assert dec(btc["quantity"]) == Decimal("0.4")
    assert dec(btc["price"]) == Decimal(60000)
    assert dec(btc["value"]) == Decimal(24000)
    assert dec(btc["share_pct"]) == Decimal("97.5610")
    assert kas["asset"] == "KAS"
    assert dec(kas["quantity"]) == Decimal(6000)
    assert dec(kas["value"]) == Decimal(600)
    assert dec(kas["share_pct"]) == Decimal("2.4390")


async def test_every_amount_is_a_json_string(api_environment: Path) -> None:
    """Asserted on the raw text: a parsed body cannot tell `"0.5"` from `0.5`."""
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        _, raw = await summary(client)

    for field in ("total_value", "quantity", "price", "value", "share_pct"):
        assert re.search(rf'"{field}":"-?\d', raw), field
        assert not re.search(rf'"{field}":-?\d', raw), field


async def test_nothing_held(api_environment: Path) -> None:
    del api_environment
    async with application() as (_app, client):
        body, _ = await summary(client)

    assert dec(body["total_value"]) == 0
    assert body["holdings"] == []
    assert body["missing"] == []


async def test_an_empty_wallet_is_not_a_holding(api_environment: Path) -> None:
    """A reading of zero is read, current and holds nothing: no row, nothing missing."""
    del api_environment
    async with application() as (app, client):
        wallet = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
        await plant_reading(app, wallet_id=wallet, confirmed=0, at=_ago(minutes=5))
        body, _ = await summary(client)

    assert body["holdings"] == []
    assert body["missing"] == []


# --------------------------------------------------------------------------------------
# What a figure is missing
# --------------------------------------------------------------------------------------


async def test_an_unpriced_holding_is_named_and_adds_nothing(api_environment: Path) -> None:
    """Never a zero: KAS keeps its row, with no price, no value and no share."""
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app, kas_price=False)
        body, _ = await summary(client)

    assert dec(body["total_value"]) == Decimal(24000)
    assert body["missing"] == [{"kind": "unpriced", "subject": "KAS"}]
    kas = body["holdings"][-1]
    assert kas["asset"] == "KAS"
    assert dec(kas["quantity"]) == Decimal(6000)
    assert (kas["price"], kas["value"], kas["share_pct"]) == (None, None, None)


async def test_out_of_date_sources_are_counted_and_named(api_environment: Path) -> None:
    """A wallet read two days ago, an unread wallet and an old price."""
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app, kas_price=False)
        old = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
        await plant_reading(app, wallet_id=old, confirmed=10_000_000, at=_ago(days=2))
        await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V1_KEY)
        await price(app, "KAS", "0.1", age=timedelta(hours=2))
        body, _ = await summary(client)

    # Sorted by kind, then subject: the order is stable, not a ranking.
    assert body["missing"] == [
        {"kind": "stale_price", "subject": "KAS"},
        {"kind": "wallet_stale", "subject": "bitcoin"},
        {"kind": "wallet_unread", "subject": "kaspa"},
    ]
    # The two-day-old 0.1 BTC still counts: it is the last thing known.
    assert dec(body["holdings"][0]["quantity"]) == Decimal("0.5")
