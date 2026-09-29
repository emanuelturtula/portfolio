"""Criteria 3 to 7 and 9 of #19, over HTTP: `GET /api/accounting/positions`.

The whole stack runs -- middleware, router, the accounting service, the price cache, the
trigger the lifespan owns, SQLite. Fills are planted through the application's own insert
(`tests/accounting_harness.py`) and the snapshot is taken with the real trigger,
`run_accounting_recompute`, so what the endpoint serves is what production would store. One
test drives the real exchange sync through `POST /api/exchanges/sync` with a simulated
venue, which is criterion 3 end to end: the manual sync's response already reflects the new
snapshot.

Every expected figure is worked out by hand beside the literal. The positions are built so
that each figure is a round number: the spec's own BTC example (1.5 BTC at an average of
35000, priced at 60000) is reproduced exactly.

## Money on the wire

Every amount and quantity is a JSON **string** at eighteen places, and the percentage at
four. They are compared as `Decimal`s -- the spelling is `MoneyStr`'s, tested in
`test_money_schema.py` -- and the quotation marks are asserted against the raw response text,
because `response.json()` cannot tell `"0.1"` from `0.1` once a test compares it to a
`Decimal`.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.api.middleware import PUBLIC_API_PATHS
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.main import create_app, run_accounting_recompute
from portfolio.services.accounting import AccountingStatus, RecomputeReason
from tests.accounting_harness import (
    at,
    plant_account,
    plant_fills,
    plant_price,
    plant_unconvertible_fill,
)
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in
from tests.exchange_sync_harness import SimulatedVenue, make_fill

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence
    from pathlib import Path

    import pytest
    from fastapi import FastAPI

    from portfolio.providers.exchanges.base import NormalizedFill

POSITIONS: Final = "/api/accounting/positions"
BOUND: Final = 5

TOP_LEVEL_FIELDS: Final = {
    "method",
    "quote_currency",
    "computed_at",
    "event_count",
    "last_recompute",
    "positions",
    "totals",
    "unallocated_costs",
    "warnings",
}
POSITION_FIELDS: Final = {
    "asset",
    "quantity",
    "unknown_basis_quantity",
    "average_cost",
    "total_invested",
    "realized_pnl",
    "unmatched_proceeds",
    "flags",
    "price",
    "market_value",
    "market_value_unavailable_reason",
    "unrealized_pnl",
    "unrealized_return_pct",
}
PRICE_FIELDS: Final = {"amount", "as_of", "stale", "source"}
TOTALS_FIELDS: Final = {
    "total_invested",
    "market_value",
    "unrealized_pnl",
    "unrealized_return_pct",
    "realized_pnl",
    "excluded",
}
WARNING_FIELDS: Final = {"kind", "occurred_at", "source", "asset", "quantity", "charged_to"}
LAST_RECOMPUTE_FIELDS: Final = {"at", "outcome", "error"}

#: Every property of this response that carries an amount, a quantity or a percentage.
MONEY_FIELDS: Final = frozenset(
    {
        "quantity",
        "unknown_basis_quantity",
        "average_cost",
        "total_invested",
        "realized_pnl",
        "unmatched_proceeds",
        "amount",
        "market_value",
        "unrealized_pnl",
        "unrealized_return_pct",
        "unallocated_costs",
    }
)

#: Distinctive trade ids, so finding one in a response is a leak rather than a coincidence.
WARNING_TRADE_ID: Final = "tid-W4RN-sold-unheld"
UNCONVERTIBLE_TRADE_ID: Final = "tid-UNCV-same-asset"


# --------------------------------------------------------------------------------------
# The application, the owner's fills, and the trigger
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def application(
    monkeypatch: pytest.MonkeyPatch,
    providers: Mapping[ExchangeKey, SimulatedVenue] | None = None,
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """The real application, signed in, its startup recompute already settled."""
    frozen = MappingProxyType(dict(providers or {}))
    monkeypatch.setattr("portfolio.main.exchange_providers", lambda client, **_: frozen)
    app = create_app()
    async with app.router.lifespan_context(app):
        await settled(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            yield app, client


async def until(condition: Callable[[], bool]) -> None:
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


async def settled(app: FastAPI) -> None:
    """Wait for the lifespan's startup recompute to finish, so no test races it."""
    task: asyncio.Task[Any] = app.state.accounting_startup_task
    await asyncio.wait_for(until(task.done), timeout=BOUND)


async def owner_id(app: FastAPI) -> int:
    async with app.state.db_sessionmaker() as session:
        found = await session.scalar(text("SELECT id FROM users WHERE username = 'owner'"))
    return int(found)


async def plant(app: FastAPI, fills: Mapping[ExchangeKey, Sequence[NormalizedFill]]) -> None:
    """The owner's accounts at each venue, and their fills, through the application's insert."""
    user_id = await owner_id(app)
    async with app.state.db_sessionmaker() as session:
        for exchange_key, venue_fills in fills.items():
            existing = await session.scalar(
                text(
                    "SELECT id FROM exchange_accounts WHERE user_id = :user AND exchange_key = :key"
                ),
                {"user": user_id, "key": str(exchange_key)},
            )
            account = (
                int(existing)
                if existing is not None
                else await plant_account(session, user_id, exchange_key)
            )
            await plant_fills(session, account, venue_fills)


async def price(app: FastAPI, symbol: str, amount: str, *, age: timedelta) -> datetime:
    """A USD price row `age` old against the real clock the endpoint reads. Returns its `as_of`."""
    as_of = (datetime.now(UTC) - age).replace(microsecond=0)
    async with app.state.db_sessionmaker() as session:
        await plant_price(session, symbol=symbol, amount=Decimal(amount), as_of=as_of)
    return as_of


async def recompute(app: FastAPI) -> AccountingStatus:
    status = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
    assert status.error is None, status
    return status


async def positions(client: AsyncClient) -> tuple[dict[str, Any], str]:
    """The parsed body and the raw text it was parsed from."""
    response = await client.get(POSITIONS)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body, response.text


def by_asset(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["asset"]: entry for entry in body["positions"]}


def dec(value: object) -> Decimal:
    assert isinstance(value, str), f"{value!r} is not a JSON string"
    return Decimal(value)


def spec_btc() -> list[NormalizedFill]:
    """Spec 021's example: 2 BTC for 70000, then 0.5 sold for 25000.

    Average 35000; the sale realizes 25000 - 0.5 x 35000 = 7500; 1.5 BTC left at a basis
    of 52500.
    """
    return [
        make_fill(
            1001,
            at(0),
            quantity="2",
            price="35000",
            quote_quantity="70000",
            fee_amount="0",
            fee_asset=None,
        ),
        make_fill(
            1002,
            at(10),
            side=FillSide.SELL,
            quantity="0.5",
            price="50000",
            quote_quantity="25000",
            fee_amount="0",
            fee_asset=None,
        ),
    ]


def kas_with_unknown_basis() -> list[NormalizedFill]:
    """1000 KAS bought for 100, then 500 more for 1 ETH nobody ever bought.

    KAS: 1000 known at a basis of 100, 500 of unknown cost, flagged. ETH: a shortfall of 1,
    nothing held, the history flagged incomplete, and a `negative_inventory` warning.
    """
    return [
        make_fill(
            2001,
            at(20),
            symbol="KASUSDT",
            base_asset="KAS",
            quantity="1000",
            price="0.1",
            quote_quantity="100",
            fee_amount="0",
            fee_asset=None,
        ),
        make_fill(
            2002,
            at(30),
            symbol="ETHKAS",
            base_asset="ETH",
            quote_asset="KAS",
            side=FillSide.SELL,
            quantity="1",
            price="500",
            quote_quantity="500",
            fee_amount="0",
            fee_asset=None,
        ),
    ]


def held_eth() -> list[NormalizedFill]:
    """2 ETH bought for 5000 and held: an asset no chain prices."""
    return [
        make_fill(
            3001,
            at(40),
            symbol="ETHUSDT",
            base_asset="ETH",
            quantity="2",
            price="2500",
            quote_quantity="5000",
            fee_amount="0",
            fee_asset=None,
        )
    ]


# --------------------------------------------------------------------------------------
# Criterion 9: authentication
# --------------------------------------------------------------------------------------


async def test_positions_require_a_session(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct `401` without a cookie, as the problem document every refusal is."""
    del api_environment
    async with application(monkeypatch) as (app, _client):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(POSITIONS)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "positions" not in response.json()


def test_the_public_allowlist_is_unchanged() -> None:
    """Criterion 9: the endpoint is protected by being absent from the allowlist."""
    assert frozenset({"/api/health", "/api/auth/login"}) == PUBLIC_API_PATHS
    assert POSITIONS not in PUBLIC_API_PATHS


def test_the_operation_is_in_the_schema_under_its_operation_id(app: FastAPI) -> None:
    operation = app.openapi()["paths"][POSITIONS]

    assert set(operation) == {"get"}
    assert operation["get"]["operationId"] == "readAccountingPositions"


# --------------------------------------------------------------------------------------
# No snapshot yet
# --------------------------------------------------------------------------------------


async def test_no_snapshot_is_a_200_with_a_null_timestamp_and_zeros(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first recompute has not written anything: every list empty, every total zero."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        async with app.state.db_sessionmaker() as session:
            await session.execute(text("DELETE FROM accounting_snapshots"))
            await session.commit()
        body, _raw = await positions(client)
        startup: AccountingStatus = app.state.accounting_status

    last = body.pop("last_recompute")
    assert set(last) == LAST_RECOMPUTE_FIELDS
    assert (last["outcome"], last["error"]) == (str(startup.outcome), None)
    assert datetime.fromisoformat(last["at"]) == startup.at
    totals = body.pop("totals")
    assert {
        key: dec(totals[key])
        for key in ("total_invested", "market_value", "unrealized_pnl", "realized_pnl")
    } == dict.fromkeys(
        ("total_invested", "market_value", "unrealized_pnl", "realized_pnl"), Decimal(0)
    )
    assert (totals["unrealized_return_pct"], totals["excluded"]) == (None, [])
    assert dec(body.pop("unallocated_costs")) == 0
    assert body == {
        "method": "weighted_average",
        "quote_currency": "USD",
        "computed_at": None,
        "event_count": 0,
        "positions": [],
        "warnings": [],
    }


# --------------------------------------------------------------------------------------
# Criteria 4 and 5: the figures, and their wire form
# --------------------------------------------------------------------------------------


async def test_the_specs_example_is_served_figure_for_figure(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 4: quantity, average cost, total invested, price, value, P&L and return.

    1.5 BTC at 35000 (C = 52500), priced at 60000: worth 90000, unrealized 37500, a return
    of 37500 x 100 / 52500 = 71.4286%. Realized 7500 sits beside it, never mixed in.
    """
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: spec_btc()})
        as_of = await price(app, "BTC", "60000", age=timedelta(minutes=5))
        status = await recompute(app)
        body, _raw = await positions(client)

    assert body["method"] == "weighted_average"
    assert body["quote_currency"] == "USD"
    assert body["event_count"] == 2
    assert datetime.fromisoformat(body["computed_at"]) <= status.at
    btc = by_asset(body)["BTC"]
    assert set(btc) == POSITION_FIELDS
    assert dec(btc["quantity"]) == Decimal("1.5")
    assert dec(btc["unknown_basis_quantity"]) == 0
    assert dec(btc["average_cost"]) == Decimal(35000)
    assert dec(btc["total_invested"]) == Decimal(52500)
    assert dec(btc["realized_pnl"]) == Decimal(7500)
    assert dec(btc["unmatched_proceeds"]) == 0
    assert btc["flags"] == []
    assert set(btc["price"]) == PRICE_FIELDS
    assert dec(btc["price"]["amount"]) == Decimal(60000)
    assert btc["price"]["stale"] is False
    assert btc["price"]["source"] == "coinbase"
    assert datetime.fromisoformat(btc["price"]["as_of"]) == as_of
    assert dec(btc["market_value"]) == Decimal(90000)
    assert btc["market_value_unavailable_reason"] is None
    assert dec(btc["unrealized_pnl"]) == Decimal(37500)
    assert dec(btc["unrealized_return_pct"]) == Decimal("71.4286")
    totals = body["totals"]
    assert set(totals) == TOTALS_FIELDS
    assert dec(totals["total_invested"]) == Decimal(52500)
    assert dec(totals["market_value"]) == Decimal(90000)
    assert dec(totals["unrealized_pnl"]) == Decimal(37500), "realized is not mixed in"
    assert dec(totals["unrealized_return_pct"]) == Decimal("71.4286")
    assert dec(totals["realized_pnl"]) == Decimal(7500)
    assert totals["excluded"] == []
    assert body["warnings"] == []


def money_values(node: object, found: list[tuple[str, object]]) -> list[tuple[str, object]]:
    """Every `(field, value)` under a money-named key, however deep."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in MONEY_FIELDS:
                found.append((key, value))
            money_values(value, found)
    elif isinstance(node, list):
        for item in node:
            money_values(item, found)
    return found


async def test_every_money_field_is_a_json_string_on_the_wire(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5, against the bytes: no money-named field is ever a bare JSON number."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: spec_btc() + kas_with_unknown_basis() + held_eth()})
        await price(app, "BTC", "60000", age=timedelta(minutes=5))
        await price(app, "KAS", "0.12", age=timedelta(minutes=5))
        await recompute(app)
        body, raw = await positions(client)

    found = money_values(body, [])
    assert {field for field, _value in found} == MONEY_FIELDS, "every money field was seen"
    assert [(field, value) for field, value in found if not isinstance(value, str | None)] == []
    for field in MONEY_FIELDS:
        assert not re.search(rf'"{field}"\s*:\s*[-0-9]', raw), field
    # The places, which is what makes a string a fixed-point amount and not a float spelling.
    btc = by_asset(body)["BTC"]
    assert len(btc["quantity"].split(".")[1]) == 18
    assert len(btc["unrealized_return_pct"].split(".")[1]) == 4


def test_every_money_field_is_declared_a_string_in_the_schema(app: FastAPI) -> None:
    """Criterion 5, against the OpenAPI document the generated client is built from."""
    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]
    reachable = {
        "PositionsResponse",
        "AccountingPositionResponse",
        "AccountingTotalsResponse",
        "AccountingWarningResponse",
        "PriceResponse",
    }
    assert reachable <= set(schemas)

    def declared(definition: dict[str, Any]) -> set[str]:
        if "anyOf" in definition:
            return {str(option.get("type")) for option in definition["anyOf"]}
        return {str(definition.get("type"))}

    offences = [
        f"{name}.{field}: {sorted(declared(definition))}"
        for name in reachable
        for field, definition in schemas[name]["properties"].items()
        if field in MONEY_FIELDS and not declared(definition) <= {"string", "null"}
    ]
    seen = {
        field
        for name in reachable
        for field in schemas[name]["properties"]
        if field in MONEY_FIELDS
    }

    assert offences == []
    assert seen == MONEY_FIELDS


# --------------------------------------------------------------------------------------
# Criterion 6: unknown basis is flagged, not zeroed
# --------------------------------------------------------------------------------------


async def test_an_unknown_basis_asset_is_flagged_valued_whole_and_excluded_from_the_totals(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KAS: 1000 known at 100 and 500 of unknown cost, priced at 0.12.

    Market value counts every unit: 1500 x 0.12 = 180. The P&L counts the known part only:
    1000 x 0.12 - 100 = 20, a return of 20%. The totals leave KAS out and say why; BTC alone
    is in them.
    """
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: spec_btc() + kas_with_unknown_basis()})
        await price(app, "BTC", "60000", age=timedelta(minutes=5))
        await price(app, "KAS", "0.12", age=timedelta(minutes=5))
        await recompute(app)
        body, _raw = await positions(client)

    kas = by_asset(body)["KAS"]
    assert kas["flags"] == ["unknown_basis"]
    assert dec(kas["unknown_basis_quantity"]) == Decimal(500)
    assert dec(kas["quantity"]) == Decimal(1500)
    assert dec(kas["total_invested"]) == Decimal(100)
    assert dec(kas["average_cost"]) == Decimal("0.1")
    assert dec(kas["market_value"]) == Decimal(180)
    assert dec(kas["unrealized_pnl"]) == Decimal(20)
    assert dec(kas["unrealized_return_pct"]) == Decimal(20)
    totals = body["totals"]
    assert totals["excluded"] == [{"asset": "KAS", "reason": "unknown_basis"}]
    assert dec(totals["total_invested"]) == Decimal(52500)
    assert dec(totals["market_value"]) == Decimal(90000)
    eth = by_asset(body)["ETH"]
    assert eth["flags"] == ["history_incomplete"]
    assert dec(eth["quantity"]) == 0


# --------------------------------------------------------------------------------------
# Criterion 7: a missing price is a null with a reason; a stale one is used and flagged
# --------------------------------------------------------------------------------------


async def test_a_chain_asset_never_priced_and_an_asset_no_chain_prices_have_different_reasons(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BTC is a chain's asset with no price row yet: `never_fetched`. ETH is nobody's:
    `unsupported_pair`, decided without a lookup. Both are null, never a zero.
    """
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: spec_btc() + held_eth()})
        await recompute(app)
        body, _raw = await positions(client)

    btc, eth = by_asset(body)["BTC"], by_asset(body)["ETH"]
    assert (btc["price"], btc["market_value"], btc["unrealized_pnl"]) == (None, None, None)
    assert btc["market_value_unavailable_reason"] == "never_fetched"
    assert btc["unrealized_return_pct"] is None
    assert (eth["price"], eth["market_value"]) == (None, None)
    assert eth["market_value_unavailable_reason"] == "unsupported_pair"
    assert dec(eth["total_invested"]) == Decimal(5000), "the cost is known without a price"
    assert body["totals"]["excluded"] == [
        {"asset": "BTC", "reason": "unpriced"},
        {"asset": "ETH", "reason": "unpriced"},
    ]
    assert dec(body["totals"]["realized_pnl"]) == Decimal(7500), "realized needs no price"


async def test_a_stale_price_is_used_as_it_is_and_flagged(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three hours old: the value is computed with it, and `stale` says so (#20 shows the age)."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: spec_btc()})
        as_of = await price(app, "BTC", "60000", age=timedelta(hours=3))
        await recompute(app)
        body, _raw = await positions(client)

    btc = by_asset(body)["BTC"]
    assert btc["price"]["stale"] is True
    assert datetime.fromisoformat(btc["price"]["as_of"]) == as_of
    assert dec(btc["market_value"]) == Decimal(90000)
    assert btc["market_value_unavailable_reason"] is None
    assert body["totals"]["excluded"] == []


async def test_a_value_past_the_range_is_a_null_with_its_reason_never_a_500(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 021, R6: 1E19 BTC bought for 1 USDT, priced at 100, is worth 1E21.

    Every input is legal -- a 20-digit quantity is what `NormalizedFill` accepts, and 100 is
    a price -- but the product has 22 digits before the point. The endpoint answers 200, with
    the value and the P&L null and the reason named, and leaves BTC out of the totals.
    """
    del api_environment
    absurd_quantity = "10000000000000000000"
    async with application(monkeypatch) as (app, client):
        await plant(
            app,
            {
                ExchangeKey.BITGET: [
                    make_fill(
                        6001,
                        at(0),
                        quantity=absurd_quantity,
                        price="1",
                        quote_quantity="1",
                        fee_amount="0",
                        fee_asset=None,
                    )
                ]
            },
        )
        await price(app, "BTC", "100", age=timedelta(minutes=5))
        await recompute(app)
        response = await client.get(POSITIONS)

    assert response.status_code == 200, response.text
    btc = by_asset(response.json())["BTC"]
    assert dec(btc["quantity"]) == Decimal(absurd_quantity)
    assert (btc["market_value"], btc["unrealized_pnl"], btc["unrealized_return_pct"]) == (
        None,
        None,
        None,
    )
    assert btc["market_value_unavailable_reason"] == "value_out_of_range"
    assert dec(btc["price"]["amount"]) == Decimal(100), "the price itself is still served"
    assert response.json()["totals"]["excluded"] == [{"asset": "BTC", "reason": "unpriced"}]


# --------------------------------------------------------------------------------------
# Warnings, and what never crosses the wire
# --------------------------------------------------------------------------------------


async def test_warnings_are_served_without_a_trade_id(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The venue and the moment identify the fill for the owner; the id stays in the table."""
    del api_environment
    unheld_sale = make_fill(
        4001,
        at(50),
        side=FillSide.SELL,
        quantity="0.25",
        price="40000",
        quote_quantity="10000",
        fee_amount="0",
        fee_asset=None,
    )
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BINGX: [_with_trade_id(unheld_sale, WARNING_TRADE_ID)]})
        await recompute(app)
        body, raw = await positions(client)

    (warning,) = body["warnings"]
    assert set(warning) == WARNING_FIELDS
    assert warning["kind"] == "negative_inventory"
    assert (warning["source"], warning["asset"], warning["charged_to"]) == ("bingx", "BTC", None)
    assert dec(warning["quantity"]) == Decimal("0.25")
    assert datetime.fromisoformat(warning["occurred_at"]) == at(50)
    assert WARNING_TRADE_ID not in raw
    assert "external_id" not in raw


def _with_trade_id(fill: NormalizedFill, trade_id: str) -> NormalizedFill:
    return replace(fill, external_trade_id=trade_id)


async def test_a_failed_recompute_is_reported_and_the_previous_snapshot_is_still_served(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`last_recompute` names the class, never the message or the id; the old figures stand."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: spec_btc()})
        await recompute(app)
        before, _raw = await positions(client)
        user_id = await owner_id(app)
        async with app.state.db_sessionmaker() as session:
            account = await session.scalar(
                text("SELECT id FROM exchange_accounts WHERE user_id = :user"), {"user": user_id}
            )
            await plant_unconvertible_fill(
                session, int(account), trade_id=UNCONVERTIBLE_TRADE_ID, shape="same_asset"
            )
        failed = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
        after, raw = await positions(client)

    assert failed.error == "UnconvertibleFillError"
    assert after["last_recompute"]["outcome"] == "failed"
    assert after["last_recompute"]["error"] == "UnconvertibleFillError"
    assert UNCONVERTIBLE_TRADE_ID not in raw
    del before["last_recompute"], after["last_recompute"]
    assert after == before


# --------------------------------------------------------------------------------------
# Criterion 3, end to end: a manual sync that stores fills is reflected when it returns
# --------------------------------------------------------------------------------------


async def test_a_manual_sync_that_stores_fills_is_reflected_by_the_time_it_returns(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real sync against a simulated venue: once `POST` returns, the positions are there."""
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    venue = SimulatedVenue(
        [make_fill(5001 + n, now - timedelta(minutes=10 - n), quantity="0.5") for n in range(4)]
    )
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        empty, _raw = await positions(client)
        response = await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["fills_inserted"] == 4
        body, _raw = await positions(client)
        again = await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
        assert again.json()["fills_inserted"] == 0
        unchanged, _raw = await positions(client)

    assert (empty["event_count"], empty["positions"]) == (0, [])
    assert body["event_count"] == 4
    assert body["last_recompute"]["outcome"] == "written"
    btc = by_asset(body)["BTC"]
    # Four buys of 0.5 BTC for 30000, each with a 0.0005 BTC fee: 4 x 0.4995 = 1.998 held.
    assert dec(btc["quantity"]) == Decimal("1.998")
    assert dec(btc["total_invested"]) == Decimal(120000)
    assert unchanged == body, "a sync that stored nothing did not recompute"


def test_the_expected_money_fields_are_the_ones_this_module_names() -> None:
    """The control for the walks above: the set is not empty and names the spec's fields."""
    assert {"market_value", "unrealized_return_pct", "total_invested"} <= MONEY_FIELDS
    assert json.dumps(sorted(MONEY_FIELDS))
