"""Spec 042 over HTTP, the whole stack: upload, list, manual entries, and the invested figures.

The investment scenario every figure below is worked out from:

* **Held**: 0.4 BTC at 60000 and 6000 KAS at 0.1, so 24000 + 600 (`plant_the_scenario`).
* **Bitget**: 0.5 BTC bought for 20000 USDT with a 0.0005 BTC fee, then 0.0995 BTC sold for
  7000 USDT with a 7 USDT fee. Invested 20000 - 6993 = **13007**; explained 0.4.
* **By hand**: 6100 KAS bought in a wallet app for 500 USDT. Explained 6100, so 100 more than
  the wallet holds.

Every sample is synthetic, and the only addresses are the testnet ones the summary uses.
"""

from __future__ import annotations

import base64
import io
import re
import zipfile
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from httpx import ASGITransport, AsyncClient

from portfolio.domain.chains import ChainKey
from portfolio.services import exchange_operations
from tests.address_vectors import KASPA_TESTNET_V0
from tests.api.test_portfolio_summary import add_wallet, application, dec, plant_the_scenario
from tests.auth.conftest import BASE_URL

if TYPE_CHECKING:
    from pathlib import Path

IMPORTS: Final = "/api/exchange-operations/imports"
OPERATIONS: Final = "/api/exchange-operations"
INVESTMENT: Final = "/api/investment"
JSON: Final = {"Content-Type": "application/json"}
"""A DELETE has no body, and the write guard still wants it declared as JSON."""

BITGET_FILLS: Final = (
    "﻿Date,Trading pair,Base Asset,Quote Asset,Direction,Price,Amount,Total,Fee,Fee Coin\r\n"
    "2026-05-01 09:00:00,BTC/USDT,BTC,USDT,Buy,40000,0.5,20000,0.0005,BTC,\r\n"
    "2026-05-03 21:30:00,BTC/USDT,BTC,USDT,Sell,70351.75,0.0995,7000,7,USDT,\r\n"
)
TANGEM_SWAP: Final = {
    "venue": "Tangem",
    "executed_at": "2026-05-02T10:00:00Z",
    "kind": "buy",
    "asset": "kas",
    "quantity": "6100",
    "quote_currency": "usdt",
    "quote_amount": "500",
    "description": "Swap in the wallet app",
}


def upload_body(filename: str, content: bytes) -> dict[str, str]:
    return {"filename": filename, "content_base64": base64.b64encode(content).decode()}


def zipped(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def corrupted(archive: bytes) -> bytes:
    """`archive` with its first member's compressed bytes garbled, its directory intact."""
    garbled = bytearray(archive)
    for index in range(40, 60):
        garbled[index] ^= 0xFF
    return bytes(garbled)


async def upload(client: AsyncClient, filename: str, content: bytes) -> dict[str, Any]:
    response = await client.post(IMPORTS, json=upload_body(filename, content))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def refused(client: AsyncClient, filename: str, content: bytes) -> str:
    response = await client.post(IMPORTS, json=upload_body(filename, content))
    assert response.status_code == 422, response.text
    detail: str = response.json()["detail"]
    return detail


async def listed(client: AsyncClient, query: str = "") -> dict[str, Any]:
    response = await client.get(OPERATIONS + query)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------------------
# Authentication and the write guard (criterion 5)
# --------------------------------------------------------------------------------------


async def test_every_new_path_requires_a_session(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, _client):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            responses = [
                await anonymous.post(IMPORTS, json=upload_body("a.csv", b"x")),
                await anonymous.get(OPERATIONS),
                await anonymous.post(OPERATIONS, json=TANGEM_SWAP),
                await anonymous.delete(f"{OPERATIONS}/1", headers=JSON),
                await anonymous.get(INVESTMENT),
            ]

    assert [response.status_code for response in responses] == [401] * 5


async def test_an_upload_that_is_not_json_is_refused(api_environment: Path) -> None:
    del api_environment
    async with application() as (_app, client):
        response = await client.post(
            IMPORTS, content=BITGET_FILLS.encode(), headers={"Content-Type": "text/csv"}
        )
        body = await listed(client)

    assert response.status_code in {403, 415}
    assert body["count"] == 0


# --------------------------------------------------------------------------------------
# Uploads (criteria 2 and 3)
# --------------------------------------------------------------------------------------


async def test_an_upload_stores_its_operations_once(api_environment: Path) -> None:
    del api_environment
    async with application() as (_app, client):
        first = await upload(client, "spot order details.csv", BITGET_FILLS.encode())
        again = await upload(client, "spot order details.csv", BITGET_FILLS.encode())
        body = await listed(client)

    assert first == {
        "filename": "spot order details.csv",
        "files": [
            {
                "name": "spot order details.csv",
                "format": "bitget_spot_order_details",
                "rows": 2,
                "stored": 2,
                "already_stored": 0,
                "skipped_reason": None,
            }
        ],
        "stored": 2,
        "already_stored": 0,
    }
    assert (again["stored"], again["already_stored"]) == (0, 2)
    assert body["count"] == 2
    newest, oldest = body["operations"]
    assert (newest["kind"], newest["asset"], dec(newest["quantity"])) == (
        "sell",
        "BTC",
        Decimal("0.0995"),
    )
    assert newest["executed_at"].startswith("2026-05-04T00:30:00")
    assert (newest["fee_asset"], dec(newest["fee_amount"])) == ("USDT", Decimal(7))
    assert (oldest["venue"], oldest["source"], oldest["manual"]) == ("Bitget", "bitget", False)
    assert dec(oldest["quote_amount"]) == Decimal(20000)


async def test_a_zip_reports_every_file_it_could_not_read(api_environment: Path) -> None:
    del api_environment
    content = zipped(
        {
            "bitget/fills.csv": BITGET_FILLS.encode(),
            "bitget/fills copy.csv": BITGET_FILLS.encode(),
            "bitget/earn.csv": b"Time,Coin,APR\n2026-05-01 00:00:00,USDT,1\n",
            "bitget/inner.zip": zipped({"x.csv": b"x"}),
            "binary.csv": b"\xff\xfe\x00\x01",
            "__MACOSX/._fills.csv": b"\x00",
            "empty/": b"",
        }
    )
    async with application() as (_app, client):
        report = await upload(client, "Bitget.zip", content)

    assert [(f["name"], f["format"], f["rows"], f["stored"]) for f in report["files"]] == [
        ("bitget/fills.csv", "bitget_spot_order_details", 2, 2),
        ("bitget/fills copy.csv", "bitget_spot_order_details", 2, 0),
        ("bitget/earn.csv", None, 1, 0),
        ("bitget/inner.zip", None, 0, 0),
        ("binary.csv", None, 0, 0),
    ]
    assert [f["skipped_reason"] for f in report["files"]] == [
        None,
        None,
        "not a format this importer reads",
        "a zip inside the zip is not opened",
        "not a UTF-8 text file",
    ]
    assert (report["stored"], report["already_stored"]) == (2, 2)


async def test_an_unreadable_row_refuses_the_whole_upload(api_environment: Path) -> None:
    broken = BITGET_FILLS + "2026-05-04 00:00:00,BTC/USDT,BTC,USDT,Buy,1,lots,1,0,BTC,\r\n"
    content = zipped({"good.csv": BITGET_FILLS.encode(), "bad.csv": broken.encode()})
    del api_environment
    async with application() as (_app, client):
        detail = await refused(client, "Bitget.zip", content)
        body = await listed(client)

    assert detail == "bad.csv, line 4: Amount is not a number: 'lots'"
    assert body["count"] == 0


async def test_the_clock_a_file_needs_must_be_found(api_environment: Path) -> None:
    binance = (
        b"User ID,Time,Account,Operation,Coin,Change,Remark\n1,2026-05-01 00:00:00,Spot,x,USDT,1,\n"
    )
    bingx = (
        b"UID,type,amount,new_available_amount,asset_name,Time(Nowhere/Atlantis),remark\n"
        b"1,Withdraw,-1,0,KAS,2026-05-01 00:00:00,\n"
    )
    del api_environment
    async with application() as (_app, client):
        renamed = await refused(client, "binance.csv", binance)
        named = await upload(client, "binance(UTC-3).csv", binance)
        unknown = await refused(client, "Fund_Account.csv", bingx)

    assert "as in (UTC-3)" in renamed
    assert named["stored"] == 1
    assert (
        unknown
        == "Fund_Account.csv, line 1: the header names an unknown time zone, 'Nowhere/Atlantis'"
    )


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"filename": "a.csv", "content_base64": "not base64!"}, "the upload is not valid base64"),
        (upload_body("a.zip", b"PK\x03\x04 not really a zip"), "the zip cannot be opened"),
        (
            upload_body("a.zip", corrupted(zipped({"a.csv": b"hello world " * 100}))),
            "the zip cannot be opened",
        ),
    ],
)
async def test_an_upload_that_cannot_be_decoded_is_refused(
    api_environment: Path, body: dict[str, str], detail: str
) -> None:
    del api_environment
    async with application() as (_app, client):
        response = await client.post(IMPORTS, json=body)

    assert response.status_code == 422
    assert response.json()["detail"] == detail


async def test_the_upload_limits(api_environment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del api_environment
    monkeypatch.setattr(exchange_operations, "MAX_UPLOAD_BYTES", 30)
    monkeypatch.setattr(exchange_operations, "MAX_ZIP_FILES", 2)
    monkeypatch.setattr(exchange_operations, "MAX_ZIP_BYTES", 10)
    async with application() as (_app, client):
        encoded_too_large = await refused(client, "a.csv", b"x" * 60)
        decoded_too_large = await client.post(
            IMPORTS, json={"filename": "a.csv", "content_base64": "eHh4" * 11}
        )
        monkeypatch.setattr(exchange_operations, "MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
        too_many = await refused(client, "a.zip", zipped({"a": b"", "b": b"", "c": b""}))
        too_big = await refused(client, "a.zip", zipped({"a": b"x" * 11}))

    assert encoded_too_large == "the file is larger than 0 MiB"
    assert decoded_too_large.status_code == 422
    assert too_many == "the zip holds more than 2 files"
    assert too_big == "the zip holds more than 0 MiB uncompressed"


# --------------------------------------------------------------------------------------
# Listing and manual entries (criterion 6)
# --------------------------------------------------------------------------------------


async def test_the_listing_pages_newest_first(api_environment: Path) -> None:
    del api_environment
    async with application() as (_app, client):
        await upload(client, "fills.csv", BITGET_FILLS.encode())
        created = await client.post(OPERATIONS, json=TANGEM_SWAP)
        first = await listed(client, "?limit=1")
        second = await listed(client, "?limit=1&offset=1")
        too_large = await client.get(OPERATIONS + "?limit=501")

    assert created.status_code == 201
    assert first["count"] == second["count"] == 3
    assert first["operations"][0]["kind"] == "sell"
    assert second["operations"][0]["venue"] == "Tangem"
    assert too_large.status_code == 422


async def test_a_manual_entry_can_be_added_and_deleted_and_an_import_cannot(
    api_environment: Path,
) -> None:
    del api_environment
    async with application() as (_app, client):
        await upload(client, "fills.csv", BITGET_FILLS.encode())
        created = await client.post(
            OPERATIONS, json={**TANGEM_SWAP, "fee_asset": "usdt", "fee_amount": "1.5"}
        )
        entry = created.json()
        imported = next(o for o in (await listed(client))["operations"] if not o["manual"])
        refused_delete = await client.delete(f"{OPERATIONS}/{imported['id']}", headers=JSON)
        deleted = await client.delete(f"{OPERATIONS}/{entry['id']}", headers=JSON)
        missing = await client.delete(f"{OPERATIONS}/{entry['id']}", headers=JSON)
        after = await listed(client)

    assert created.status_code == 201, created.text
    assert entry["manual"] is True
    assert entry["source"] == "manual"
    assert entry["external_id"].startswith("manual:")
    assert (entry["asset"], entry["quote_currency"], entry["fee_asset"]) == ("KAS", "USDT", "USDT")
    assert (entry["quantity"], entry["quote_amount"], entry["fee_amount"]) == ("6100", "500", "1.5")
    assert entry["executed_at"].startswith("2026-05-02T10:00:00")
    assert refused_delete.status_code == 409
    assert deleted.status_code == 204
    assert missing.status_code == 404
    assert after["count"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "deposit"},
        {"executed_at": "2026-05-02T10:00:00"},
        {"quantity": 6100.5},
        {"quantity": "0"},
        {"quote_amount": "-1"},
        {"asset": ""},
        {"unexpected": "field"},
    ],
)
async def test_a_manual_entry_out_of_shape_is_refused(
    api_environment: Path, change: dict[str, object]
) -> None:
    del api_environment
    async with application() as (_app, client):
        response = await client.post(OPERATIONS, json={**TANGEM_SWAP, **change})
        body = await listed(client)

    assert response.status_code == 422
    assert body["count"] == 0


async def test_a_fee_needs_both_its_asset_and_its_amount(api_environment: Path) -> None:
    del api_environment
    async with application() as (_app, client):
        amount_only = await client.post(OPERATIONS, json={**TANGEM_SWAP, "fee_amount": "1"})
        asset_only = await client.post(OPERATIONS, json={**TANGEM_SWAP, "fee_asset": "KAS"})

    assert (amount_only.json()["fee_asset"], amount_only.json()["fee_amount"]) == (None, None)
    assert (asset_only.json()["fee_asset"], asset_only.json()["fee_amount"]) == (None, None)


# --------------------------------------------------------------------------------------
# The investment (criteria 4 and 7)
# --------------------------------------------------------------------------------------


async def test_the_investment_of_the_scenario(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        await upload(client, "fills.csv", BITGET_FILLS.encode())
        await client.post(OPERATIONS, json=TANGEM_SWAP)
        response = await client.get(INVESTMENT)

    assert response.status_code == 200, response.text
    body = response.json()
    btc, kas = body["assets"]
    assert btc == {
        "asset": "BTC",
        "invested": "13007.000000000000000000",
        "value": "24000.000000000000000000",
        "pnl": "10993.000000000000000000",
        "pnl_pct": "84.5160",
        "held": "0.40000000",
        "explained": "0.400000000000000000",
        "difference": "0.000000000000000000",
        "trades": 2,
        "unvalued_trades": 0,
        "unavailable": None,
    }
    assert (dec(kas["invested"]), dec(kas["pnl"]), dec(kas["pnl_pct"])) == (
        Decimal(500),
        Decimal(100),
        Decimal(20),
    )
    assert (dec(kas["held"]), dec(kas["explained"]), dec(kas["difference"])) == (
        Decimal(6000),
        Decimal(6100),
        Decimal(-100),
    )
    total = body["overall"]
    assert (dec(total["invested"]), dec(total["value"]), dec(total["pnl"])) == (
        Decimal(13507),
        Decimal(24600),
        Decimal(11093),
    )
    assert total["pnl_pct"] == "82.1278"
    assert [(day["day"], dec(day["invested"])) for day in body["invested_by_day"]] == [
        ("2026-05-01", Decimal(20000)),
        ("2026-05-02", Decimal(20500)),
        ("2026-05-04", Decimal(13507)),
    ]
    assert not re.search(
        r'"(invested|value|pnl|pnl_pct|held|explained|difference)":-?\d', response.text
    )


async def test_an_unknown_figure_is_null_with_its_reason(api_environment: Path) -> None:
    """KAS unpriced, and a BTC purchase paid in pesos: neither is ever rendered as zero."""
    pesos = (
        "User ID,Time,Account,Operation,Coin,Change,Remark\n"
        "1,2026-05-05 10:00:00,Funding,P2P Trading,BTC,0.001,\n"
    )
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app, kas_price=False)
        await upload(client, "history(UTC-3).csv", pesos.encode())
        body = (await client.get(INVESTMENT)).json()

    btc, kas = body["assets"]
    assert (btc["invested"], btc["pnl"], btc["unavailable"]) == (None, None, "unvalued_trades")
    assert (btc["trades"], btc["unvalued_trades"]) == (1, 1)
    assert (kas["value"], kas["pnl"], kas["unavailable"]) == (None, None, "value_unknown")
    assert (dec(kas["invested"]), dec(kas["held"])) == (Decimal(0), Decimal(6000))
    assert body["overall"]["invested"] is None
    assert body["overall"]["unavailable"] == "unvalued_trades"
    assert body["invested_by_day"] == [{"day": "2026-05-05", "invested": None}]


async def test_nothing_tracked_and_an_unread_wallet(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, client):
        empty = (await client.get(INVESTMENT)).json()
        await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0)
        unread = (await client.get(INVESTMENT)).json()

    assert empty["assets"] == []
    assert empty["overall"]["unavailable"] == "nothing_invested"
    (kas,) = unread["assets"]
    assert (kas["held"], kas["value"], kas["difference"]) == (None, None, None)
    assert kas["unavailable"] == "value_unknown"
