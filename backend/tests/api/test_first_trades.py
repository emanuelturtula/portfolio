"""Criteria 1, 2 and 4 of #111, over HTTP: `GET /api/accounting/first-trades` (spec 027).

The whole stack runs -- middleware, router, the accounting service, SQLite. Fills are planted
through the application's own insert (`tests/accounting_harness.py`), on the owner's accounts
and on a stranger's, and the adjustment in the adjustments test is recorded through the
application's own `POST`.

## What is pinned

* **The shape**, byte for byte against the spec's own example document: one object with one
  key, `assets`, each entry an `asset` and a `first_trade_at`, and nothing else.
* **`first_trade_at` is an aware ISO 8601 instant** in UTC, the fill's own time to the
  millisecond.
* **`401` without a session**, and the public-path allowlist exactly as it was.
* **The rule end to end**: base, quote and non-zero fee each count, a zero fee does not, the
  cash assets are absent, a sale counts like a buy, the earliest fill across two venues wins,
  and the list is sorted by asset. The rule itself is `first_trades_of`'s, tested clause by
  clause in `tests/services/test_first_trades.py`; here it is shown to reach the wire.
* **Another owner's fills are not read**, whatever the query string says.
* **No fills is `{"assets": []}`**, and a manual adjustment adds nothing to it.
* **It reads the stored fills and not the snapshot**: it answers before any recompute, and
  while the last recompute is failing on a row it cannot convert.
* **The operation is in the OpenAPI document** under `readFirstTrades`, with both models.
* **No trade id and nothing of a venue's payload crosses it.**

## Time

The instants are fixed dates in the past, the spec's own among them: this endpoint measures
nothing against a clock. The adjustment's date only has to be earlier than now.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.api.middleware import PUBLIC_API_PATHS
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.main import run_accounting_recompute
from portfolio.services.accounting import RecomputeOutcome, RecomputeReason
from tests.accounting_harness import (
    INGESTED_AT,
    fixed,
    plant_account,
    plant_fills,
    plant_owner,
    plant_unconvertible_fill,
)
from tests.adjustments_harness import ADJUSTMENTS_PATH, POSITIONS_PATH, body, iso
from tests.api.test_accounting import application, owner_id, plant, recompute
from tests.auth.conftest import BASE_URL, JSON_HEADERS
from tests.balance_harness import sqlite_timestamp
from tests.exchange_sync_harness import make_fill

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastapi import FastAPI

    from portfolio.providers.exchanges.base import NormalizedFill

FIRST_TRADES: Final = "/api/accounting/first-trades"

#: The spec's own instant: `"first_trade_at": "2025-03-01T10:00:37Z"`.
SPEC_INSTANT: Final = datetime(2025, 3, 1, 10, 0, 37, tzinfo=UTC)
SPEC_INSTANT_ON_THE_WIRE: Final = "2025-03-01T10:00:37Z"

TOP_LEVEL_FIELDS: Final = {"assets"}
ENTRY_FIELDS: Final = {"asset", "first_trade_at"}

#: Distinctive, so finding one in a response is a leak rather than a coincidence. Synthetic.
MARKED_TRADE_ID: Final = "tid-F1RST-trade-marker"
MARKED_PAYLOAD: Final = '{"marker":"payload-sentinel-Q7Q7Q7Q7"}'
STRANGER_ASSET: Final = "ZZOTHER"


def minute(offset: int) -> datetime:
    """The spec's instant plus `offset` minutes."""
    return SPEC_INSTANT + timedelta(minutes=offset)


def trade(
    trade_id: int,
    when: datetime,
    *,
    base: str = "BTC",
    quote: str = "USDT",
    side: FillSide = FillSide.BUY,
    fee: str = "0",
    fee_asset: str | None = None,
) -> NormalizedFill:
    """One valid fill of `base` against `quote`: round amounts, and the fee as given."""
    return make_fill(
        trade_id,
        when,
        symbol=f"{base}{quote}",
        base_asset=base,
        quote_asset=quote,
        side=side,
        quantity="2",
        price="10",
        quote_quantity="20",
        fee_amount=fee,
        fee_asset=fee_asset,
    )


async def first_trades(client: AsyncClient, path: str = FIRST_TRADES) -> dict[str, Any]:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    answer: dict[str, Any] = response.json()
    return answer


def instants(answer: dict[str, Any]) -> list[tuple[str, datetime]]:
    """The entries as `(asset, instant)`, each instant parsed and required to be aware."""
    parsed: list[tuple[str, datetime]] = []
    for entry in answer["assets"]:
        assert set(entry) == ENTRY_FIELDS, entry
        assert isinstance(entry["first_trade_at"], str), entry
        instant = datetime.fromisoformat(entry["first_trade_at"])
        assert instant.utcoffset() == timedelta(0), f"{entry['first_trade_at']!r} is not UTC"
        parsed.append((entry["asset"], instant))
    return parsed


# --------------------------------------------------------------------------------------
# Criterion 1: authentication
# --------------------------------------------------------------------------------------


async def test_first_trades_require_a_session(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct `401` without a cookie, as the problem document every refusal is.

    Fills are planted first, so that a `401` which leaked anything would have it to leak.
    """
    del api_environment
    async with application(monkeypatch) as (app, _client):
        await plant(app, {ExchangeKey.BITGET: [trade(1001, SPEC_INSTANT)]})
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(FIRST_TRADES)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "assets" not in response.json()
    assert "BTC" not in response.text
    assert "first_trade_at" not in response.text


def test_the_public_allowlist_is_unchanged() -> None:
    """The endpoint is protected by being absent from the allowlist, which did not grow."""
    assert frozenset({"/api/health", "/api/auth/login"}) == PUBLIC_API_PATHS
    assert FIRST_TRADES not in PUBLIC_API_PATHS
    assert not any("first-trades" in path for path in PUBLIC_API_PATHS)


# --------------------------------------------------------------------------------------
# Criterion 4: the OpenAPI document
# --------------------------------------------------------------------------------------


def test_the_operation_is_in_the_schema_under_its_operation_id(app: FastAPI) -> None:
    operation = app.openapi()["paths"][FIRST_TRADES]

    assert set(operation) == {"get"}, "one operation, a read"
    assert operation["get"]["operationId"] == "readFirstTrades"
    assert operation["get"].get("parameters", []) == [], "it takes no parameters"
    assert "requestBody" not in operation["get"]
    answer = operation["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert answer == {"$ref": "#/components/schemas/FirstTradesResponse"}
    assert "accounting" in operation["get"]["tags"]


def test_the_schema_declares_the_specs_shape(app: FastAPI) -> None:
    """What the generated TypeScript client is built from: both fields required, the
    instant a `date-time` string, and no third field."""
    schemas = app.openapi()["components"]["schemas"]
    top = schemas["FirstTradesResponse"]
    entry = schemas["FirstTradeResponse"]

    assert set(top["properties"]) == TOP_LEVEL_FIELDS
    assert set(top["required"]) == TOP_LEVEL_FIELDS
    assert top["properties"]["assets"]["type"] == "array"
    assert top["properties"]["assets"]["items"] == {
        "$ref": "#/components/schemas/FirstTradeResponse"
    }
    assert set(entry["properties"]) == ENTRY_FIELDS
    assert set(entry["required"]) == ENTRY_FIELDS
    assert entry["properties"]["asset"]["type"] == "string"
    assert entry["properties"]["first_trade_at"]["type"] == "string"
    assert entry["properties"]["first_trade_at"]["format"] == "date-time"


# --------------------------------------------------------------------------------------
# Criterion 2: no fills
# --------------------------------------------------------------------------------------


async def test_an_owner_with_no_fills_gets_an_empty_list(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (_app, client):
        response = await client.get(FIRST_TRADES)

    assert response.status_code == 200, response.text
    assert response.json() == {"assets": []}


async def test_an_owner_with_an_account_and_no_fills_gets_an_empty_list(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: [], ExchangeKey.BINGX: []})
        answer = await first_trades(client)

    assert answer == {"assets": []}


# --------------------------------------------------------------------------------------
# Criterion 1: the shape, and the rule on the wire
# --------------------------------------------------------------------------------------


async def test_the_response_is_the_specs_example_document(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One BTC buy at the spec's instant, paid in USDT with no fee: the spec's own body."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: [trade(1001, SPEC_INSTANT)]})
        answer = await first_trades(client)

    assert answer == {"assets": [{"asset": "BTC", "first_trade_at": SPEC_INSTANT_ON_THE_WIRE}]}
    assert set(answer) == TOP_LEVEL_FIELDS


def two_venues() -> dict[ExchangeKey, list[NormalizedFill]]:
    """A history that uses every clause of the rule, over two venues.

    | Venue | When | Fill | Fee | Begins |
    |---|---|---|---|---|
    | Bitget | +20 min | buy BTC for USDT | none | nothing: BTC was the quote at +5 |
    | Bitget | +5 min | sell ETH for BTC | 0.01 BGB | BTC, as the quote |
    | Bitget | +1 min | buy 1000SATS for USDT | 0 KAS | 1000SATS; KAS does not begin |
    | Bitget | +2 min | buy SOL for USDC | 3 USDT | SOL |
    | BingX | +9 min | buy BTC for USDT | none | nothing: later than its quote role |
    | BingX | +3 min | buy ETH for USDT | -0.1 BGB, a rebate | ETH, and BGB by the rebate |
    | BingX | +8 min | sell ZEC for USDT | none | ZEC, which was only ever sold |

    Each role decides one date. BTC is dated as a quote asset, five minutes in, before
    either of its buys. BGB is dated by a rebate (ruling R1), before the fee paid in it. ETH
    is dated by the second venue's fill, before the first venue's.
    """
    return {
        ExchangeKey.BITGET: [
            trade(1001, minute(20)),
            trade(
                1002,
                minute(5),
                base="ETH",
                quote="BTC",
                side=FillSide.SELL,
                fee="0.01",
                fee_asset="BGB",
            ),
            trade(1003, minute(1), base="1000SATS", fee="0", fee_asset="KAS"),
            trade(1004, minute(2), base="SOL", quote="USDC", fee="3", fee_asset="USDT"),
        ],
        ExchangeKey.BINGX: [
            trade(2001, minute(9)),
            trade(2002, minute(3), base="ETH", fee="-0.1", fee_asset="BGB"),
            trade(2003, minute(8), base="ZEC", side=FillSide.SELL),
        ],
    }


EXPECTED_TWO_VENUES: Final = [
    ("1000SATS", minute(1)),
    ("BGB", minute(3)),
    ("BTC", minute(5)),
    ("ETH", minute(3)),
    ("SOL", minute(2)),
    ("ZEC", minute(8)),
]


async def test_each_asset_is_dated_by_its_earliest_fill_in_any_role_sorted_by_asset(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Base, quote and non-zero fee count; the zero fee and the cash assets do not.

    The digit sorts before the letters, BTC is dated by the fill it was the quote of, ETH by
    the second venue's fill rather than the first-planted one, and BGB by a rebate.
    """
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, two_venues())
        answer = await first_trades(client)

    assert instants(answer) == EXPECTED_TWO_VENUES
    listed = [entry["asset"] for entry in answer["assets"]]
    assert "KAS" not in listed, "a zero fee moves nothing, so it begins nothing"
    assert not {"USDC", "USDT"} & set(listed), "cash is the unit of account, never listed"
    assert answer["assets"][2] == {"asset": "BTC", "first_trade_at": "2025-03-01T10:05:37Z"}


async def test_a_sale_counts_like_a_buy(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ETH is sold at +0 and bought at +10: its history begins at the sale. DOGE is only
    ever sold, and is listed."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(
            app,
            {
                ExchangeKey.BITGET: [
                    trade(1, minute(10), base="ETH"),
                    trade(2, minute(0), base="ETH", side=FillSide.SELL),
                    trade(3, minute(4), base="DOGE", side=FillSide.SELL),
                ]
            },
        )
        answer = await first_trades(client)

    assert instants(answer) == [("DOGE", minute(4)), ("ETH", minute(0))]


async def test_the_instant_is_the_fills_own_time_to_the_millisecond(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venue's clock reports milliseconds. The earlier of two fills 1 ms apart is served,
    as an aware instant."""
    early = SPEC_INSTANT + timedelta(milliseconds=123)
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(
            app,
            {ExchangeKey.BITGET: [trade(1, early + timedelta(milliseconds=1)), trade(2, early)]},
        )
        answer = await first_trades(client)

    assert instants(answer) == [("BTC", early)]
    assert answer["assets"][0]["first_trade_at"].startswith("2025-03-01T10:00:37.123")


# --------------------------------------------------------------------------------------
# Criterion 2: another owner's fills, and manual adjustments
# --------------------------------------------------------------------------------------


async def plant_stranger(app: FastAPI) -> int:
    """A second user who traded BTC ten hours before the owner, and an asset of their own."""
    async with app.state.db_sessionmaker() as session:
        stranger = await plant_owner(session, "someone-else")
        account = await plant_account(session, stranger, ExchangeKey.BITGET)
        await plant_fills(
            session,
            account,
            [trade(9001, minute(-600)), trade(9002, minute(-300), base=STRANGER_ASSET)],
        )
    return stranger


async def test_another_owners_fills_are_not_read(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, two_venues())
        stranger = await plant_stranger(app)
        response = await client.get(FIRST_TRADES)
        asked_for_theirs = await first_trades(client, f"{FIRST_TRADES}?user_id={stranger}")

    assert response.status_code == 200, response.text
    assert STRANGER_ASSET not in response.text
    assert instants(response.json()) == EXPECTED_TWO_VENUES
    assert asked_for_theirs == response.json(), "a query string chooses nobody's history"


async def test_an_owner_with_no_fills_sees_none_of_a_strangers(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_stranger(app)
        answer = await first_trades(client)

    assert answer == {"assets": []}


async def test_manual_adjustments_are_not_counted(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opening balance of BTC a year before its first fill, and one of an asset no fill
    names, both recorded through the API: the list is the imported history's, unchanged."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: [trade(1001, SPEC_INSTANT)]})
        before = await first_trades(client)
        for asset in ("BTC", "DOGE"):
            created = await client.post(
                ADJUSTMENTS_PATH,
                json=body(asset=asset, occurred_at=iso(SPEC_INSTANT - timedelta(days=365))),
                headers=JSON_HEADERS,
            )
            assert created.status_code == 201, created.text
        listed = await client.get(ADJUSTMENTS_PATH)
        after = await first_trades(client)

    assert len(listed.json()["adjustments"]) == 2, "the control: both adjustments are stored"
    assert before == {"assets": [{"asset": "BTC", "first_trade_at": SPEC_INSTANT_ON_THE_WIRE}]}
    assert after == before


async def test_adjustments_alone_give_an_empty_list(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (_app, client):
        created = await client.post(
            ADJUSTMENTS_PATH,
            json=body(occurred_at=iso(SPEC_INSTANT)),
            headers=JSON_HEADERS,
        )
        answer = await first_trades(client)

    assert created.status_code == 201, created.text
    assert answer == {"assets": []}


# --------------------------------------------------------------------------------------
# It reads the stored fills, not the snapshot
# --------------------------------------------------------------------------------------


async def test_it_answers_from_the_fills_before_any_recompute_has_seen_them(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fills are planted after the startup recompute and nothing recomputes: the
    positions do not know them yet, and the first trades do."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, two_venues())
        answer = await first_trades(client)
        positions = await client.get(POSITIONS_PATH)
        again = await first_trades(client)
        await recompute(app)
        recomputed = await first_trades(client)

    assert positions.json()["positions"] == [], "the control: the snapshot has not caught up"
    assert instants(answer) == EXPECTED_TWO_VENUES
    assert again == answer
    assert recomputed == answer, "a recompute changes nothing about when the history begins"


async def test_it_answers_while_the_recompute_fails_on_a_fill_it_cannot_convert(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row stored before #99, BTC for BTC, fails every recompute. The form's date hint
    still has to work, and the row is part of the history: it dates BTC."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: [trade(1001, minute(9), base="ETH", quote="BTC")]})
        async with app.state.db_sessionmaker() as session:
            account = await session.scalar(text("SELECT id FROM exchange_accounts"))
            await plant_unconvertible_fill(
                session,
                int(account),
                trade_id=MARKED_TRADE_ID,
                shape="same_asset",
                executed_at=minute(4),
            )
        status = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
        response = await client.get(FIRST_TRADES)

    assert status.outcome is RecomputeOutcome.FAILED, "the control: the recompute failed"
    assert status.error == "UnconvertibleFillError"
    assert response.status_code == 200, response.text
    assert instants(response.json()) == [("BTC", minute(4)), ("ETH", minute(9))]
    assert MARKED_TRADE_ID not in response.text


HAND_WRITTEN_FILL: Final = (
    "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, external_order_id, "
    "symbol, base_asset, quote_asset, side, quantity, price, quote_quantity, "
    "quote_quantity_derived, fee_amount, fee_asset, executed_at, raw_payload, ingested_at) "
    "VALUES (:account, :trade, NULL, :symbol, :base, :quote, 'buy', :one, :one, :one, 0, :fee, "
    ":fee_asset, :at, '{}', :ingested)"
)


async def test_hand_edited_rows_are_answered_and_never_a_500(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rows no venue sends and the application never writes, as a hand edit leaves them.

    One has a fee amount and no fee asset (ruling R2): it names its base and its quote, and
    nothing else. The other has a fee that is a signalling NaN, which any comparison raises
    on. The reduction converts nothing and so cannot raise on either (ruling R7): the answer
    is a `200`, with each row's base and quote dated by it. A fee that is not a number is
    "not zero", so the second row's fee asset is listed as well.
    """
    del api_environment
    rows = [
        ("synthetic-no-fee-asset", "ETH", "BTC", fixed(Decimal("0.5")), None, minute(4)),
        ("synthetic-nan-fee", "SOL", "BTC", "sNaN", "BGB", minute(6)),
    ]
    async with application(monkeypatch) as (app, client):
        user_id = await owner_id(app)
        async with app.state.db_sessionmaker() as session:
            account = await plant_account(session, user_id, ExchangeKey.BITGET)
            for trade_id, base, quote, fee, fee_asset, when in rows:
                await session.execute(
                    text(HAND_WRITTEN_FILL),
                    {
                        "account": account,
                        "trade": trade_id,
                        "symbol": f"{base}{quote}",
                        "base": base,
                        "quote": quote,
                        "one": fixed(Decimal(1)),
                        "fee": fee,
                        "fee_asset": fee_asset,
                        "at": sqlite_timestamp(when),
                        "ingested": sqlite_timestamp(INGESTED_AT),
                    },
                )
            await session.commit()
        response = await client.get(FIRST_TRADES)

    assert response.status_code == 200, response.text
    answer = dict(instants(response.json()))
    assert answer == {
        "BGB": minute(6),
        "BTC": minute(4),
        "ETH": minute(4),
        "SOL": minute(6),
    }
    assert all(isinstance(entry["asset"], str) for entry in response.json()["assets"])


# --------------------------------------------------------------------------------------
# Nothing else crosses the wire
# --------------------------------------------------------------------------------------


async def test_no_trade_id_and_no_payload_is_served(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fill with a distinctive trade id and a distinctive payload: the response is an
    asset and an instant, and neither text is anywhere in it."""
    del api_environment
    marked = replace(
        make_fill(1001, SPEC_INSTANT, fee_amount="0", fee_asset=None, raw_payload=MARKED_PAYLOAD),
        external_trade_id=MARKED_TRADE_ID,
    )
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: [marked]})
        async with app.state.db_sessionmaker() as session:
            stored = await session.scalar(
                text("SELECT external_trade_id || raw_payload FROM exchange_fills")
            )
        response = await client.get(FIRST_TRADES)

    assert stored == MARKED_TRADE_ID + MARKED_PAYLOAD, "the control: both markers are stored"
    assert response.json() == {
        "assets": [{"asset": "BTC", "first_trade_at": SPEC_INSTANT_ON_THE_WIRE}]
    }
    assert MARKED_TRADE_ID not in response.text
    assert "payload-sentinel" not in response.text
    assert "marker" not in response.text
