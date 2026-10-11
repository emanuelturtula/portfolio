"""`GET /api/portfolio/changes` over HTTP, the whole stack (spec 041).

The scenario every figure below is worked out from:

* **Held**: 0.4 BTC and 6000 KAS, both read eight days ago and five minutes ago.
* **Now**: BTC 60000 and KAS 0.1, so 24000 + 600 = **24600**.
* **24 hours ago**: BTC closed at 50000 and KAS at 0.1, so 20600: a change of **+4000**,
  19.4175 % of it.
* **7 days ago**: BTC closed at 75000 and KAS at 0.1, so 30600: a change of **-6000**,
  -19.6078 % of it.

Every instant is taken from the real clock: the endpoint measures back from it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.domain.chains import ChainKey
from portfolio.repositories.price_hourly import PriceHourlyRepository
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WSH, KASPA_TESTNET_V0
from tests.api.test_portfolio_summary import (
    _ago,
    add_wallet,
    application,
    dec,
    plant_reading,
    price,
)
from tests.auth.conftest import BASE_URL

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

CHANGES: Final = "/api/portfolio/changes"
CHANGE_FIELDS: Final = {"period", "since", "value_then", "change", "change_pct", "unavailable"}


async def plant_close(app: FastAPI, symbol: str, amount: str, *, ago: timedelta) -> None:
    """The hourly close that prices the instant `ago` before now: the hour ending just before."""
    instant = datetime.now(UTC) - ago
    hour = instant.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    async with app.state.db_sessionmaker() as session:
        asset_id = await session.scalar(
            text("SELECT id FROM assets WHERE symbol = :symbol"), {"symbol": symbol}
        )
        await PriceHourlyRepository(session).record_new(
            asset_id=int(asset_id),
            quote_currency="USD",
            closes=[(hour, Decimal(amount))],
            source="kraken",
            recorded_at=datetime.now(UTC),
        )
        await session.commit()


async def plant_the_scenario(app: FastAPI, *, kas_week_close: bool = True) -> None:
    bitcoin = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    kaspa = await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0)
    for wallet_id, confirmed in ((bitcoin, 40_000_000), (kaspa, 600_000_000_000)):
        await plant_reading(app, wallet_id=wallet_id, confirmed=confirmed, at=_ago(days=8))
        await plant_reading(app, wallet_id=wallet_id, confirmed=confirmed, at=_ago(minutes=5))
    await price(app, "BTC", "60000", age=timedelta(minutes=3))
    await price(app, "KAS", "0.1", age=timedelta(minutes=3))
    day, week = timedelta(hours=24), timedelta(days=7)
    await plant_close(app, "BTC", "50000", ago=day)
    await plant_close(app, "KAS", "0.1", ago=day)
    await plant_close(app, "BTC", "75000", ago=week)
    if kas_week_close:
        await plant_close(app, "KAS", "0.1", ago=week)


async def changes(client: AsyncClient) -> tuple[dict[str, Any], str]:
    response = await client.get(CHANGES)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body, response.text


async def test_the_changes_require_a_session(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, _client):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(CHANGES)

    assert response.status_code == 401
    assert '"change"' not in response.text


async def test_the_change_over_a_day_and_a_week(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        body, raw = await changes(client)

    assert set(body) == {"as_of", "value", "changes"}
    assert dec(body["value"]) == Decimal(24600)
    day, week = body["changes"]
    assert set(day) == CHANGE_FIELDS
    assert (day["period"], week["period"]) == ("24h", "7d")
    assert dec(day["value_then"]) == Decimal(20600)
    assert dec(day["change"]) == Decimal(4000)
    assert dec(day["change_pct"]) == Decimal("19.4175")
    assert day["unavailable"] is None
    assert dec(week["change"]) == Decimal(-6000)
    assert dec(week["change_pct"]) == Decimal("-19.6078")
    as_of = datetime.fromisoformat(body["as_of"])
    assert datetime.fromisoformat(day["since"]) == as_of - timedelta(hours=24)
    assert datetime.fromisoformat(week["since"]) == as_of - timedelta(days=7)
    assert not re.search(r'"(value|value_then|change|change_pct)":-?\d', raw)


async def test_a_missing_price_then_is_unavailable_and_never_zero(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app, kas_week_close=False)
        body, raw = await changes(client)

    day, week = body["changes"]
    assert day["unavailable"] is None
    assert week["unavailable"] == "no_price_then"
    assert week["change"] is None
    assert week["change_pct"] is None
    assert '"change":null' in raw


async def test_an_unread_wallet_now_makes_every_change_unavailable(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
        body, _ = await changes(client)

    assert body["value"] is None
    assert [change["unavailable"] for change in body["changes"]] == [
        "no_reading_then",
        "no_reading_then",
    ]
