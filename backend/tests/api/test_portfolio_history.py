"""The value-history endpoints over HTTP, the whole stack (spec 037).

Middleware, router, service, domain and SQLite all run. The readings and the prices are
planted against the real clock, which the endpoints read for "today".
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.domain.chains import ChainKey
from portfolio.main import create_app
from portfolio.repositories.price_history import CLOSE, OBSERVED, PriceHistoryRepository
from tests.address_vectors import BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V0
from tests.auth.conftest import BASE_URL, sign_in
from tests.balance_harness import insert_user, insert_wallet, sqlite_timestamp

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

HISTORY: Final = "/api/portfolio/history"
POINT_FIELDS: Final = {"day", "value", "assets"}
WALLET_POINT_FIELDS: Final = {"day", "quantity", "value"}


def wallet_history(wallet_id: int) -> str:
    return f"/api/wallets/{wallet_id}/value-history"


@asynccontextmanager
async def application() -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """The real application, its lifespan run, signed in as the owner."""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            yield app, client


def today() -> date:
    return datetime.now(UTC).date()


def dec(value: object) -> Decimal:
    assert isinstance(value, str), f"{value!r} is not a JSON string"
    return Decimal(value)


async def add_wallet(
    app: FastAPI, chain: ChainKey, address: str, *, user_id: int | None = None
) -> int:
    async with app.state.db_sessionmaker() as session:
        if user_id is None:
            owner = await session.scalar(text("SELECT id FROM users WHERE username = 'owner'"))
            user_id = int(owner)
        return await insert_wallet(session, user_id=user_id, chain_key=chain, address=address)


async def plant_reading(app: FastAPI, wallet_id: int, confirmed: int, at: datetime) -> None:
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


async def plant_price(app: FastAPI, symbol: str, day: date, amount: str, basis: str) -> None:
    async with app.state.db_sessionmaker() as session:
        asset_id = await session.scalar(
            text("SELECT id FROM assets WHERE symbol = :symbol"), {"symbol": symbol}
        )
        await PriceHistoryRepository(session).record(
            asset_id=int(asset_id),
            quote_currency="USD",
            day=day,
            amount=Decimal(amount),
            basis=basis,
            source="kraken",
            recorded_at=datetime.now(UTC),
        )
        await session.commit()


async def plant_two_days(app: FastAPI) -> int:
    """0.4 BTC read yesterday and today; yesterday closed at 60000, today seen at 61000."""
    yesterday = today() - timedelta(days=1)
    btc = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    noon = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    await plant_reading(app, btc, 40_000_000, noon - timedelta(days=1))
    await plant_reading(app, btc, 40_000_000, datetime.now(UTC) - timedelta(seconds=5))
    await plant_price(app, "BTC", yesterday, "60000", CLOSE)
    await plant_price(app, "BTC", today(), "61000", OBSERVED)
    return btc


async def get_json(client: AsyncClient, path: str) -> tuple[dict[str, Any], str]:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body, response.text


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


async def test_both_endpoints_require_a_session(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, _client):
        btc = await plant_two_days(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            responses = [await anonymous.get(HISTORY), await anonymous.get(wallet_history(btc))]

    for response in responses:
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")
        assert "points" not in response.text


# --------------------------------------------------------------------------------------
# The portfolio
# --------------------------------------------------------------------------------------


async def test_the_default_range_is_ninety_days_ending_today(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_two_days(app)
        body, _ = await get_json(client, HISTORY)

    assert set(body) == {"range", "assets", "points"}
    assert body["range"] == "90d"
    assert body["assets"] == ["BTC"]
    points = body["points"]
    assert len(points) == 90
    assert all(set(point) == POINT_FIELDS for point in points)
    assert points[-1]["day"] == today().isoformat()
    assert dec(points[-1]["value"]) == Decimal(24400)
    assert dec(points[-2]["value"]) == Decimal(24000)
    assert dec(points[-1]["assets"]["BTC"]) == Decimal(24400)
    # Before the first reading the value is unknown, and says so with a null, never a zero.
    assert points[0]["value"] is None


async def test_each_asset_has_its_own_value_beside_the_total(api_environment: Path) -> None:
    """Spec 041, R7: KAS read today only is `null` yesterday, while BTC and the total are not."""
    del api_environment
    async with application() as (app, client):
        await plant_two_days(app)
        kas = await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0)
        await plant_reading(app, kas, 1_000_000_000_000, datetime.now(UTC) - timedelta(seconds=5))
        await plant_price(app, "KAS", today(), "0.05", OBSERVED)
        body, raw = await get_json(client, f"{HISTORY}?range=30d")

    assert body["assets"] == ["BTC", "KAS"]
    yesterday, now = body["points"][-2:]
    assert yesterday["assets"]["KAS"] is None
    assert dec(yesterday["assets"]["BTC"]) == Decimal(24000)
    assert dec(yesterday["value"]) == Decimal(24000)
    assert dec(now["assets"]["KAS"]) == Decimal(500)
    assert dec(now["value"]) == Decimal(24900)
    assert not re.search(r'"(BTC|KAS)":-?\d', raw)


async def test_all_starts_on_the_first_reading(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_two_days(app)
        body, _ = await get_json(client, f"{HISTORY}?range=all")

    assert body["range"] == "all"
    assert [point["day"] for point in body["points"]] == [
        (today() - timedelta(days=1)).isoformat(),
        today().isoformat(),
    ]


async def test_every_amount_is_a_json_string_and_a_gap_is_null(api_environment: Path) -> None:
    """Asserted on the raw text: a parsed body cannot tell `"0.5"` from `0.5`."""
    del api_environment
    async with application() as (app, client):
        await plant_two_days(app)
        _, raw = await get_json(client, f"{HISTORY}?range=30d")

    assert re.search(r'"value":"\d', raw)
    assert re.search(r'"value":null', raw)
    assert not re.search(r'"value":-?\d', raw)


async def test_a_range_that_is_not_one_of_the_four_is_refused(api_environment: Path) -> None:
    del api_environment
    async with application() as (_app, client):
        response = await client.get(f"{HISTORY}?range=7d")

    assert response.status_code == 422


# --------------------------------------------------------------------------------------
# One wallet
# --------------------------------------------------------------------------------------


async def test_one_wallets_quantity_and_value(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        btc = await plant_two_days(app)
        body, raw = await get_json(client, f"{wallet_history(btc)}?range=1y")

    assert (body["wallet_id"], body["asset"], body["range"]) == (btc, "BTC", "1y")
    points = body["points"]
    assert len(points) == 365
    assert all(set(point) == WALLET_POINT_FIELDS for point in points)
    assert dec(points[-1]["quantity"]) == Decimal("0.4")
    assert dec(points[-1]["value"]) == Decimal(24400)
    assert points[0] == {"day": points[0]["day"], "quantity": None, "value": None}
    assert not re.search(r'"quantity":-?\d', raw)


async def test_a_wallet_of_another_owner_is_not_found(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        async with app.state.db_sessionmaker() as session:
            stranger = await insert_user(session, "stranger")
        theirs = await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0, user_id=stranger)
        responses = [
            await client.get(wallet_history(theirs)),
            await client.get(wallet_history(999_999)),
        ]

    for response in responses:
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
