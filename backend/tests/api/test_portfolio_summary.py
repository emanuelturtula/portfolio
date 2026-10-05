"""`GET /api/portfolio/summary` over HTTP, the whole stack (#154).

Middleware, router, service, domain and SQLite all run. The fills are planted through the
application's own insert, the wallet readings as the balance sync leaves them, the venue
readings through the write the exchange sync makes, and the prices as the refresh stores them.

The scenario every figure below is worked out from:

* **Fills on Bitget**: 0.5 BTC bought for 30000 USDT with its fee in BTC; 10000 KAS bought
  for 1000 with a 1 USDT fee; 2000 KAS sold for 300 with a 0.3 USDT fee. Invested is
  30000 + 1000 + 1 - 300 + 0.3 = 30701.3.
* **Held**: a Bitcoin wallet with 0.4 BTC and a Kaspa wallet with 6000 KAS; Bitget holds
  0.0995 BTC, 2000 KAS, 5000 USDT and 0.5 of an asset nothing tracks.
* **Prices**: BTC 60000 and KAS 0.1, minutes old.

So 0.4995 BTC is worth 29970 and 8000 KAS 800: a total of 30770, a P/L of 68.7, which is
0.2238 % of what was invested. The USDT is cash and is in none of it.

Every instant is taken from the real clock, for the reason `test_reconciliation.py` gives:
the endpoint measures ages against it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient

from portfolio.api.middleware import PUBLIC_API_PATHS
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import ExchangeKey, FillSide
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
)
from tests.api.test_accounting import application, dec, plant, price
from tests.api.test_reconciliation import account_id, add_wallet, plant_synced
from tests.auth.conftest import BASE_URL
from tests.exchange_sync_harness import held, make_fill
from tests.services.test_reconciliation_service import plant_reading, store_balances

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastapi import FastAPI

    from portfolio.providers.exchanges.base import NormalizedFill

SUMMARY: Final = "/api/portfolio/summary"

TOP_LEVEL_FIELDS: Final = {
    "total_value",
    "invested",
    "pnl",
    "pnl_pct",
    "holdings",
    "missing",
    "untracked",
}
HOLDING_FIELDS: Final = {"asset", "quantity", "price", "value", "share_pct"}

#: Synthetic, and distinctive, so finding it in a refused response is a leak.
UNTRACKED: Final = "ZZDUST"


def fills() -> list[NormalizedFill]:
    return [
        make_fill(5001, _ago(days=30), quantity="0.5", price="60000", quote_quantity="30000"),
        make_fill(
            5002,
            _ago(days=20),
            symbol="KASUSDT",
            base_asset="KAS",
            quantity="10000",
            price="0.1",
            quote_quantity="1000",
            fee_amount="1",
            fee_asset="USDT",
        ),
        make_fill(
            5003,
            _ago(days=10),
            symbol="KASUSDT",
            base_asset="KAS",
            side=FillSide.SELL,
            quantity="2000",
            price="0.15",
            quote_quantity="300",
            fee_amount="0.3",
            fee_asset="USDT",
        ),
    ]


def _ago(*, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    return (datetime.now(UTC) - delta).replace(microsecond=0)


async def plant_the_scenario(app: FastAPI, *, kas_price: bool = True) -> None:
    await plant_synced(app, {ExchangeKey.BITGET: fills()})
    factory = app.state.db_sessionmaker
    bitcoin = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    kaspa = await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(
        factory, wallet_id=bitcoin, confirmed=40_000_000, observed_at=_ago(minutes=5)
    )
    await plant_reading(
        factory, wallet_id=kaspa, confirmed=600_000_000_000, observed_at=_ago(minutes=5)
    )
    await store_balances(
        factory,
        await account_id(app, ExchangeKey.BITGET),
        (held("BTC", "0.0995"), held("KAS", "2000"), held("USDT", "5000"), held(UNTRACKED, "0.5")),
        _ago(minutes=6),
    )
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


async def test_the_summary_requires_a_session(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, _client):
        await plant_the_scenario(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(SUMMARY)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "holdings" not in response.text
    assert UNTRACKED not in response.text


def test_the_public_allowlist_is_unchanged() -> None:
    assert frozenset({"/api/health", "/api/auth/login"}) == PUBLIC_API_PATHS
    assert not any("portfolio" in path for path in PUBLIC_API_PATHS)


# --------------------------------------------------------------------------------------
# The figures
# --------------------------------------------------------------------------------------


async def test_the_figures_of_the_scenario(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_scenario(app)
        body, _ = await summary(client)

    assert set(body) == TOP_LEVEL_FIELDS
    assert dec(body["total_value"]) == Decimal(30770)
    assert dec(body["invested"]) == Decimal("30701.3")
    assert dec(body["pnl"]) == Decimal("68.7")
    assert dec(body["pnl_pct"]) == Decimal("0.2238")
    assert body["missing"] == []
    assert body["untracked"] == [UNTRACKED]

    assert [set(holding) for holding in body["holdings"]] == [HOLDING_FIELDS] * 2
    btc, kas = body["holdings"]
    assert btc["asset"] == "BTC"
    assert dec(btc["quantity"]) == Decimal("0.4995")
    assert dec(btc["price"]) == Decimal(60000)
    assert dec(btc["value"]) == Decimal(29970)
    assert dec(btc["share_pct"]) == Decimal("97.4001")
    assert kas["asset"] == "KAS"
    assert dec(kas["quantity"]) == Decimal(8000)
    assert dec(kas["value"]) == Decimal(800)
    assert dec(kas["share_pct"]) == Decimal("2.5999")


async def test_every_amount_is_a_json_string(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on the raw text: a parsed body cannot tell `"0.5"` from `0.5`."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_scenario(app)
        _, raw = await summary(client)

    for field in ("total_value", "invested", "pnl", "pnl_pct", "quantity", "price", "value"):
        assert re.search(rf'"{field}":"-?\d', raw), field
        assert not re.search(rf'"{field}":-?\d', raw), field


async def test_nothing_held_and_nothing_traded(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (_app, client):
        body, _ = await summary(client)

    assert dec(body["total_value"]) == 0
    assert dec(body["invested"]) == 0
    assert dec(body["pnl"]) == 0
    assert body["pnl_pct"] is None
    assert body["holdings"] == []
    assert body["missing"] == []
    assert body["untracked"] == []


# --------------------------------------------------------------------------------------
# What a figure is missing
# --------------------------------------------------------------------------------------


async def test_an_unpriced_holding_is_named_and_adds_nothing(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a zero: KAS keeps its row, with no price, no value and no share."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_scenario(app, kas_price=False)
        body, _ = await summary(client)

    assert dec(body["total_value"]) == Decimal(29970)
    assert body["missing"] == [{"kind": "unpriced", "subject": "KAS"}]
    kas = body["holdings"][-1]
    assert kas["asset"] == "KAS"
    assert dec(kas["quantity"]) == Decimal(8000)
    assert (kas["price"], kas["value"], kas["share_pct"]) == (None, None, None)


async def test_out_of_date_sources_are_counted_and_named(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wallet read two days ago, an unread wallet, a venue never read and an old price."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_scenario(app, kas_price=False)
        await plant(app, {ExchangeKey.BINGX: []})
        factory = app.state.db_sessionmaker
        old = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
        await plant_reading(factory, wallet_id=old, confirmed=10_000_000, observed_at=_ago(days=2))
        await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V1_KEY)
        await price(app, "KAS", "0.1", age=timedelta(hours=2))
        body, _ = await summary(client)

    # Sorted by kind, then subject: the order is stable, not a ranking.
    assert body["missing"] == [
        {"kind": "exchange_unread", "subject": "bingx"},
        {"kind": "stale_price", "subject": "KAS"},
        {"kind": "wallet_stale", "subject": "bitcoin"},
        {"kind": "wallet_unread", "subject": "kaspa"},
    ]
    # The two-day-old 0.1 BTC still counts: it is the last thing known.
    assert dec(body["holdings"][0]["quantity"]) == Decimal("0.5995")


async def test_a_fill_not_quoted_in_cash_is_named(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_scenario(app)
        await plant(
            app,
            {
                ExchangeKey.BINGX: [
                    make_fill(
                        5101,
                        _ago(days=5),
                        symbol="KASBTC",
                        base_asset="KAS",
                        quote_asset="BTC",
                        quantity="100",
                        price="0.0000001",
                        quote_quantity="0.00001",
                    )
                ]
            },
        )
        body, _ = await summary(client)

    assert dec(body["invested"]) == Decimal("30701.3")
    assert {"kind": "fill_not_in_cash", "subject": "BTC"} in body["missing"]
