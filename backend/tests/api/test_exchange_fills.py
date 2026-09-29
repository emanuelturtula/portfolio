"""The backend criteria of #93, over HTTP: `GET /api/exchanges/fills`.

The whole stack runs -- middleware, router, the read service, the repository, SQLite. The
fills are the book of `tests/fill_view_harness.py`, planted through the application's own
insert, and every expected total is `BOOK_TOTALS`, worked by hand there. No venue is
configured: this endpoint reads the database and calls nothing.

## Money on the wire

Every amount is a JSON **string**. Figures are compared as `Decimal`s (the spelling is
`MoneyStr`'s, tested in `test_money_schema.py`), and the quotation marks are asserted against
the raw response text, because `response.json()` cannot tell `"0.1"` from `0.1` once a test
compares it to a `Decimal`.

## Criteria to tests

* Newest first, stable tie-break, never `raw_payload`: `test_the_fills_are_newest_first_*`,
  `test_no_raw_payload_or_trade_id_*` and `test_the_openapi_document_never_mentions_*`.
* The venue filter, and an unknown venue: `test_filtering_by_venue_*`,
  `test_an_unknown_venue_is_a_422`.
* The half-open range, and naive, inverted or digit-string bounds:
  `test_adjacent_ranges_partition_*`, `test_each_refused_bound_is_a_field_level_422_*`.
* Totals over the whole filtered set: `test_paging_through_every_page_*`,
  `test_a_filtered_page_totals_*`, `test_an_offset_past_the_end_*`.
* Exact past 28 digits: `test_a_sum_needing_more_than_28_digits_is_served_exactly`.
* USDT only from USDT quotes, per-quote sums, signed fees, the unvalued counts:
  `test_the_totals_are_the_books_worked_totals`, `test_each_row_carries_*`.
* Negative nets: `test_a_range_of_sales_serves_negative_nets`.
* An empty result: `test_nothing_imported_*`, `test_an_empty_range_*`.
* Money as strings: `test_every_money_field_is_a_json_string_*`,
  `test_every_money_field_is_declared_*`.
* `401` and the allowlist: `test_the_fills_require_a_session`,
  `test_the_public_allowlist_is_unchanged`.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.api.errors import PROBLEM_CONTENT_TYPE
from portfolio.api.middleware import PUBLIC_API_PATHS
from portfolio.api.schemas.exchanges import INSTANT_FORMAT_REFUSAL
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.main import create_app
from portfolio.services.exchanges import (
    INVERTED_RANGE_RULE,
    NAIVE_BOUND_RULE,
    UNREPRESENTABLE_BOUND_RULE,
)
from tests.accounting_harness import plant_owner
from tests.auth.conftest import BASE_URL, sign_in
from tests.fill_view_harness import (
    ASSET_FIELDS,
    BOOK_ORDER,
    BOOK_TOTALS,
    EMPTY_TOTALS,
    FEE_FIELDS,
    FILLS_PATH,
    MONEY_FIELDS,
    NOT_VALUED_FIELDS,
    PAYLOAD_SENTINEL,
    QUOTE_FIELDS,
    ROW_FIELDS,
    TOP_LEVEL_FIELDS,
    TOTALS_FIELDS,
    TRADE_ID_PREFIX,
    USDT_FIELDS,
    book_fill,
    decimals_of,
    minute,
    plant_history,
    row_ids,
    the_book,
    user_id_of,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence
    from pathlib import Path

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import NormalizedFill

BOUND: Final = 5
LARGEST_OFFSET: Final = 2**63 - 1

#: The response schemas `GET /api/exchanges/fills` reaches.
RESPONSE_SCHEMAS: Final = frozenset(
    {
        "ExchangeFillListResponse",
        "ExchangeFillResponse",
        "ExchangeFillTotalsResponse",
        "ExchangeFillAssetTotalsResponse",
        "ExchangeFillUsdtTotalsResponse",
        "ExchangeFillNotValuedInUsdtResponse",
        "ExchangeFillQuoteAssetTotalsResponse",
        "ExchangeFillFeeTotalResponse",
    }
)

#: The other user's fills: Bitget, like the owner's, and inside every range asked here.
INTRUDER_FILLS: Final = (9001, 9002)


# --------------------------------------------------------------------------------------
# The application, the book, and the request
# --------------------------------------------------------------------------------------


async def until(condition: Callable[[], bool]) -> None:
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


@asynccontextmanager
async def application(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """The real application, signed in, no venue configured, its startup recompute settled."""
    empty: MappingProxyType[ExchangeKey, object] = MappingProxyType({})
    monkeypatch.setattr("portfolio.main.exchange_providers", lambda client, **_: empty)
    app = create_app()
    async with app.router.lifespan_context(app):
        task: asyncio.Task[Any] = app.state.accounting_startup_task
        await asyncio.wait_for(until(task.done), timeout=BOUND)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            yield app, client


def factory_of(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = app.state.db_sessionmaker
    return factory


async def plant_book(app: FastAPI) -> dict[int, int]:
    """The book for the owner, two fills for another user; returns each fill's row id."""
    factory = factory_of(app)
    owner = await user_id_of(factory)
    await plant_history(factory, owner, the_book())
    async with factory() as session:
        intruder = await plant_owner(session, "intruder")
    await plant_history(
        factory,
        intruder,
        {
            ExchangeKey.BITGET: [
                book_fill(number, minute(10), quantity="7", quote_quantity="420000")
                for number in INTRUDER_FILLS
            ]
        },
    )
    return await row_ids(factory)


async def plant_owner_fills(app: FastAPI, fills: Mapping[ExchangeKey, Sequence[Any]]) -> None:
    factory = factory_of(app)
    await plant_history(factory, await user_id_of(factory), fills)


async def fills(client: AsyncClient, params: Any = None) -> tuple[dict[str, Any], str]:
    """A `200` body and the raw text it was parsed from."""
    response = await client.get(FILLS_PATH, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body, response.text


def numbers(body: Mapping[str, Any], ids: Mapping[int, int]) -> list[int]:
    """The book numbers of the served rows, in the order served."""
    by_row = {row: number for number, row in ids.items()}
    return [by_row[row["id"]] for row in body["fills"]]


def iso(moment: datetime) -> str:
    return moment.isoformat()


def zulu(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def dec(value: object) -> Decimal:
    assert isinstance(value, str), f"{value!r} is not a JSON string"
    return Decimal(value)


# --------------------------------------------------------------------------------------
# Order, rows, and what is never served
# --------------------------------------------------------------------------------------


async def test_the_fills_are_newest_first_with_ties_broken_by_id_descending(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ties -- within Bitget, and across venues -- served id-descending."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        ids = await plant_book(app)
        body, _raw = await fills(client)

    assert numbers(body, ids) == list(BOOK_ORDER)
    served = [(datetime.fromisoformat(row["executed_at"]), row["id"]) for row in body["fills"]]
    assert served == sorted(served, reverse=True)
    assert body["total_count"] == 7


async def test_each_row_carries_exactly_the_specs_fields(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2002 field by field; 1004's nulls; 2003's derived flag; USDT value only for USDT."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        ids = await plant_book(app)
        body, _raw = await fills(client)

    rows = dict(zip(numbers(body, ids), body["fills"], strict=True))
    assert all(set(row) == ROW_FIELDS for row in body["fills"])
    eth_sale = rows[2002]
    assert eth_sale["id"] == ids[2002]
    assert datetime.fromisoformat(eth_sale["executed_at"]) == minute(5)
    assert datetime.fromisoformat(eth_sale["executed_at"]).tzinfo is not None
    assert {
        key: eth_sale[key]
        for key in ("exchange_key", "symbol", "base_asset", "quote_asset", "side", "order_id")
    } == {
        "exchange_key": "bingx",
        "symbol": "ETHUSDT",
        "base_asset": "ETH",
        "quote_asset": "USDT",
        "side": "sell",
        "order_id": "ord-2002",
    }
    assert [dec(eth_sale[key]) for key in ("quantity", "price", "quote_quantity")] == [
        Decimal(1),
        Decimal(3000),
        Decimal("3000.123456789012345678"),
    ]
    assert dec(eth_sale["usdt_value"]) == Decimal("3000.123456789012345678"), "as stored"
    assert (dec(eth_sale["fee_amount"]), eth_sale["fee_asset"]) == (Decimal("-0.01"), "BNB")
    assert eth_sale["quote_quantity_derived"] is False

    no_order = rows[1004]
    assert (no_order["order_id"], no_order["fee_asset"], no_order["usdt_value"]) == (
        None,
        None,
        None,
    )
    assert dec(no_order["fee_amount"]) == 0
    assert no_order["quote_asset"] == "USDC"
    assert rows[1003]["usdt_value"] is None, "quoted in BTC"
    assert rows[2003]["quote_quantity_derived"] is True
    assert dec(rows[2003]["usdt_value"]) == Decimal(100)
    assert rows[1002]["side"] == "sell"
    assert rows[1001]["side"] == "buy"


async def test_no_raw_payload_or_trade_id_in_a_real_response(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every fill carries a distinctive payload and trade id; neither is in the response."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_book(app)
        _body, raw = await fills(client)
        async with factory_of(app)() as session:
            stored = await session.scalar(
                text("SELECT group_concat(raw_payload) FROM exchange_fills")
            )

    assert PAYLOAD_SENTINEL in str(stored), "the control: the payloads were stored"
    assert PAYLOAD_SENTINEL not in raw
    assert "raw_payload" not in raw
    assert TRADE_ID_PREFIX not in raw
    assert "external_trade_id" not in raw
    assert "ord-2002" in raw, "the control: the order id is served"


def test_the_openapi_document_never_mentions_the_payload_or_the_trade_id(app: FastAPI) -> None:
    """The whole document, every schema and every description: no `raw_payload` anywhere."""
    document = json.dumps(app.openapi())

    assert "raw_payload" not in document
    assert "external_trade_id" not in document
    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]
    assert set(schemas["ExchangeFillResponse"]["properties"]) == ROW_FIELDS


# --------------------------------------------------------------------------------------
# Totals, and the wire form of money
# --------------------------------------------------------------------------------------


async def test_the_totals_are_the_books_worked_totals(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every figure of `BOOK_TOTALS`: mixed quotes, the unvalued counts, signed fees."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_book(app)
        body, _raw = await fills(client)

    assert set(body) == TOP_LEVEL_FIELDS
    totals = body["totals"]
    assert set(totals) == TOTALS_FIELDS
    assert all(set(row) == ASSET_FIELDS for row in totals["by_asset"])
    assert set(totals["usdt"]) == USDT_FIELDS
    assert set(totals["not_valued_in_usdt"]) == NOT_VALUED_FIELDS
    assert all(set(row) == QUOTE_FIELDS for row in totals["not_valued_in_usdt"]["by_quote_asset"])
    assert all(set(row) == FEE_FIELDS for row in totals["fees"])
    assert decimals_of(totals) == BOOK_TOTALS
    assert body["total_count"] == totals["fill_count"] == 7


async def test_every_money_field_is_a_json_string_on_the_wire(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against the bytes: no money-named field is ever a bare JSON number."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_book(app)
        body, raw = await fills(client)

    found: list[tuple[str, object]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in MONEY_FIELDS:
                    found.append((key, value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    assert {field for field, _value in found} == MONEY_FIELDS, "every money field was seen"
    assert [(field, value) for field, value in found if not isinstance(value, str | None)] == []
    for field in MONEY_FIELDS:
        assert not re.search(rf'"{field}"\s*:\s*[-0-9]', raw), field
    assert "E+" not in raw
    assert "E-" not in raw, "positional notation, never scientific"


def test_every_money_field_is_declared_a_string_in_the_schema(app: FastAPI) -> None:
    """Against the OpenAPI document the generated client is built from."""
    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]
    assert set(schemas) >= RESPONSE_SCHEMAS

    def declared(definition: dict[str, Any]) -> set[str]:
        if "anyOf" in definition:
            return {str(option.get("type")) for option in definition["anyOf"]}
        return {str(definition.get("type"))}

    offences = [
        f"{name}.{field}: {sorted(declared(definition))}"
        for name in RESPONSE_SCHEMAS
        for field, definition in schemas[name]["properties"].items()
        if field in MONEY_FIELDS and not declared(definition) <= {"string", "null"}
    ]
    seen = {
        field
        for name in RESPONSE_SCHEMAS
        for field in schemas[name]["properties"]
        if field in MONEY_FIELDS
    }

    assert offences == []
    assert seen == MONEY_FIELDS


def test_every_net_is_described_as_buys_minus_sells(app: FastAPI) -> None:
    """Spec 024: the schema descriptions say what sign a net has, and that it may be negative."""
    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]
    nets = [
        schemas["ExchangeFillAssetTotalsResponse"]["properties"]["net"],
        schemas["ExchangeFillAssetTotalsResponse"]["properties"]["usdt_net"],
        schemas["ExchangeFillUsdtTotalsResponse"]["properties"]["net"],
        schemas["ExchangeFillQuoteAssetTotalsResponse"]["properties"]["net"],
    ]

    for net in nets:
        description = net["description"]
        assert "negative" in description, description
        assert "minus" in description or " - " in description, description


async def test_a_sum_needing_more_than_28_digits_is_served_exactly(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """20 integer digits and 18 places, plus one unit: every one of the 38 digits arrives."""
    del api_environment
    widest = "12345678901234567890.123456789012345678"
    one_unit = "0.000000000000000001"
    async with application(monkeypatch) as (app, client):
        await plant_owner_fills(
            app,
            {
                ExchangeKey.BITGET: [
                    book_fill(
                        5001,
                        minute(0),
                        quantity=widest,
                        price="1",
                        quote_quantity=widest,
                        fee_amount="0",
                        fee_asset=None,
                    ),
                    book_fill(
                        5002,
                        minute(1),
                        quantity=one_unit,
                        price="1",
                        quote_quantity=one_unit,
                        fee_amount="0",
                        fee_asset=None,
                    ),
                ]
            },
        )
        body, _raw = await fills(client)

    exact = "12345678901234567890.123456789012345679"
    (btc,) = body["totals"]["by_asset"]
    assert btc["bought"] == exact
    assert btc["net"] == exact
    assert body["totals"]["usdt"]["spent"] == exact
    assert body["totals"]["usdt"]["net"] == exact


async def test_a_range_of_sales_serves_negative_nets(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bitget over [10, 11): only 1002, a sale of 0.2 BTC for 13000 USDT. Never clamped."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_book(app)
        body, _raw = await fills(
            client,
            {"exchange": "bitget", "from": zulu(minute(10)), "to": zulu(minute(11))},
        )

    (btc,) = body["totals"]["by_asset"]
    assert (dec(btc["net"]), dec(btc["usdt_net"])) == (Decimal("-0.2"), Decimal(-13000))
    assert btc["net"].startswith("-")
    assert dec(body["totals"]["usdt"]["net"]) == Decimal(-13000)


# --------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, set(BOOK_ORDER)),
        ({"exchange": "bitget"}, {1001, 1002, 1003, 1004}),
        ({"exchange": "bingx"}, {2001, 2002, 2003}),
        ({"exchange": ["bitget", "bingx"]}, set(BOOK_ORDER)),
        ({"exchange": ["bingx", "bingx"]}, {2001, 2002, 2003}),
    ],
    ids=["none is all", "one", "the other", "several", "a repeat counts once"],
)
async def test_filtering_by_venue_returns_exactly_the_matching_fills(
    api_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    expected: set[int],
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        ids = await plant_book(app)
        body, _raw = await fills(client, params)

    served = numbers(body, ids)
    assert sorted(served) == sorted(expected), "each matching fill exactly once"
    assert body["total_count"] == body["totals"]["fill_count"] == len(expected)
    assert set(INTRUDER_FILLS).isdisjoint(served), "another owner's fills are never served"


async def test_an_unknown_venue_is_a_422(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_book(app)
        response = await client.get(FILLS_PATH, params={"exchange": ["bitget", "kraken"]})

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    (error,) = response.json()["errors"]
    assert (error["loc"], error["type"]) == (["query", "exchange", "1"], "enum")


async def test_adjacent_ranges_partition_the_book_with_each_boundary_fill_once(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`from` inclusive, `to` exclusive: the ranges meeting at minutes 5, 10, 20 and 30."""
    del api_environment
    cuts = [minute(-1), minute(5), minute(10), minute(20), minute(30), minute(31)]
    async with application(monkeypatch) as (app, client):
        ids = await plant_book(app)
        ranges = []
        for since, end in pairwise(cuts):
            body, _raw = await fills(client, {"from": zulu(since), "to": zulu(end)})
            ranges.append(set(numbers(body, ids)))

    assert ranges == [{1001}, {2002}, {1002, 2001}, {1003, 1004}, {2003}]
    assert sum(len(found) for found in ranges) == len(BOOK_ORDER)


async def test_a_bound_in_any_offset_is_the_same_instant(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Z`, `+00:00`, `+02:00` and `-05:00` spellings of [10, 20): the same two fills."""
    del api_environment
    plus_two = timezone(timedelta(hours=2))
    minus_five = timezone(timedelta(hours=-5))
    spellings = [
        (zulu(minute(10)), zulu(minute(20))),
        (iso(minute(10)), iso(minute(20))),
        (iso(minute(10).astimezone(plus_two)), iso(minute(20).astimezone(minus_five))),
    ]
    async with application(monkeypatch) as (app, client):
        ids = await plant_book(app)
        served = []
        for since, end in spellings:
            body, _raw = await fills(client, {"from": since, "to": end})
            served.append(numbers(body, ids))

    assert served == [[2001, 1002]] * 3


REFUSED_BOUNDS: Final = [
    ("naive from", {"from": "2024-07-19T13:57:11"}, "from", f"from {NAIVE_BOUND_RULE}"),
    ("naive to", {"to": "2024-07-19T13:57:11"}, "to", f"to {NAIVE_BOUND_RULE}"),
    ("a date alone", {"from": "2024-07-19"}, "from", f"from {NAIVE_BOUND_RULE}"),
    ("a basic date", {"to": "20240719"}, "to", f"to {NAIVE_BOUND_RULE}"),
    ("unix seconds", {"from": "1721397431"}, "from", f"Value error, {INSTANT_FORMAT_REFUSAL}"),
    ("unix to", {"to": "1721397431"}, "to", f"Value error, {INSTANT_FORMAT_REFUSAL}"),
    ("fractional", {"from": "1721397431.5"}, "from", f"Value error, {INSTANT_FORMAT_REFUSAL}"),
    ("prose", {"to": "next tuesday"}, "to", f"Value error, {INSTANT_FORMAT_REFUSAL}"),
    (
        "from equals to",
        {"from": "2026-03-01T09:10:00Z", "to": "2026-03-01T09:10:00Z"},
        "to",
        INVERTED_RANGE_RULE,
    ),
    (
        "from after to",
        {"from": "2026-03-01T09:20:00Z", "to": "2026-03-01T09:10:00Z"},
        "to",
        INVERTED_RANGE_RULE,
    ),
    (
        "inverted across offsets",
        {"from": "2026-03-01T09:10:00Z", "to": "2026-03-01T10:05:00+01:00"},
        "to",
        INVERTED_RANGE_RULE,
    ),
    (
        "before the first instant",
        {"from": "0001-01-01T00:00:00+01:00"},
        "from",
        f"from {UNREPRESENTABLE_BOUND_RULE}",
    ),
]


@pytest.mark.parametrize(
    ("params", "field", "msg"),
    [(params, field, msg) for _name, params, field, msg in REFUSED_BOUNDS],
    ids=[name for name, *_rest in REFUSED_BOUNDS],
)
async def test_each_refused_bound_is_a_field_level_422_that_echoes_nothing(
    api_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, str],
    field: str,
    msg: str,
) -> None:
    """A digit string is never read as Unix time, and a naive datetime never as UTC."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_book(app)
        response = await client.get(FILLS_PATH, params=params)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    problem = response.json()
    assert problem["errors"] == [{"loc": ["query", field], "msg": msg, "type": "value_error"}]
    for value in params.values():
        assert value not in response.text, "the 422 echoed the value it refused"


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"limit": 0}, 422),
        ({"limit": 1}, 200),
        ({"limit": 200}, 200),
        ({"limit": 201}, 422),
        ({"offset": -1}, 422),
        ({"offset": 0}, 200),
        ({"offset": LARGEST_OFFSET}, 200),
        ({"offset": LARGEST_OFFSET + 1}, 422),
        ({"offset": 10**20}, 422),
    ],
    ids=[
        "limit zero",
        "limit one",
        "limit two hundred",
        "limit two hundred and one",
        "offset negative",
        "offset zero",
        "offset largest",
        "offset two to the 63",
        "offset ten to the 20",
    ],
)
async def test_the_limit_and_offset_are_bounded_at_both_ends(
    api_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, int],
    status: int,
) -> None:
    del api_environment
    async with application(monkeypatch) as (_app, client):
        response = await client.get(FILLS_PATH, params=params)

    assert response.status_code == status, response.text


# --------------------------------------------------------------------------------------
# Paging: the totals cover the whole filtered set
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 3, 7])
async def test_paging_through_every_page_gives_the_whole_and_the_same_totals(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        ids = await plant_book(app)
        pages = []
        for offset in range(0, len(BOOK_ORDER), limit):
            body, _raw = await fills(client, {"limit": limit, "offset": offset})
            pages.append(body)

    assert [number for page in pages for number in numbers(page, ids)] == list(BOOK_ORDER)
    assert all(len(page["fills"]) <= limit for page in pages)
    assert all(decimals_of(page["totals"]) == BOOK_TOTALS for page in pages)
    assert all(page["total_count"] == 7 for page in pages)


async def test_a_filtered_page_totals_the_filtered_set(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BingX from minute 5: 2001, 2002 and 2003. The second page of one still totals three."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        ids = await plant_book(app)
        body, _raw = await fills(
            client, {"exchange": "bingx", "from": zulu(minute(5)), "limit": 1, "offset": 1}
        )

    assert numbers(body, ids) == [2001]
    assert body["total_count"] == 3
    assert {row["asset"]: row["fill_count"] for row in body["totals"]["by_asset"]} == {
        "BTC": 1,
        "ETH": 1,
        "KAS": 1,
    }


async def test_an_offset_past_the_end_is_an_empty_page_with_the_same_totals(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_book(app)
        past, _raw = await fills(client, {"offset": 7})
        farthest, _raw = await fills(client, {"offset": LARGEST_OFFSET})

    for body in (past, farthest):
        assert body["fills"] == []
        assert body["total_count"] == 7
        assert decimals_of(body["totals"]) == BOOK_TOTALS


async def test_the_default_page_is_fifty(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fifty-one fills: the first page holds fifty, the newest, and counts all fifty-one."""
    del api_environment
    many: list[NormalizedFill] = [
        book_fill(
            6000 + index,
            minute(index),
            quantity="1",
            price="2",
            quote_quantity="2",
            fee_amount="0",
            fee_asset=None,
        )
        for index in range(51)
    ]
    async with application(monkeypatch) as (app, client):
        await plant_owner_fills(app, {ExchangeKey.BITGET: many})
        body, _raw = await fills(client)

    assert len(body["fills"]) == 50
    assert body["total_count"] == 51
    assert datetime.fromisoformat(body["fills"][0]["executed_at"]) == minute(50)
    (btc,) = body["totals"]["by_asset"]
    assert dec(btc["bought"]) == Decimal(51)


# --------------------------------------------------------------------------------------
# Empty results
# --------------------------------------------------------------------------------------


async def test_nothing_imported_is_a_200_with_zero_totals(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (_app, client):
        body, _raw = await fills(client)

    assert body["fills"] == []
    assert body["total_count"] == 0
    assert decimals_of(body["totals"]) == EMPTY_TOTALS
    assert body["totals"]["usdt"]["spent"] == "0.000000000000000000", "at the stored scale"


async def test_an_empty_range_is_a_200_with_zero_totals(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_book(app)
        body, _raw = await fills(client, {"from": zulu(minute(1)), "to": zulu(minute(2))})

    assert (body["fills"], body["total_count"]) == ([], 0)
    assert decimals_of(body["totals"]) == EMPTY_TOTALS


# --------------------------------------------------------------------------------------
# Authentication, and the document
# --------------------------------------------------------------------------------------


async def test_the_fills_require_a_session(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct `401` without a cookie, as the problem document every refusal is."""
    del api_environment
    async with application(monkeypatch) as (app, _client):
        await plant_book(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(FILLS_PATH)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert "fills" not in response.json()
    assert PAYLOAD_SENTINEL not in response.text


def test_the_public_allowlist_is_unchanged() -> None:
    """The endpoint is protected by being absent from the allowlist."""
    assert frozenset({"/api/health", "/api/auth/login"}) == PUBLIC_API_PATHS
    assert FILLS_PATH not in PUBLIC_API_PATHS


def test_the_operation_is_in_the_schema_with_its_parameters(app: FastAPI) -> None:
    operation = app.openapi()["paths"][FILLS_PATH]

    assert set(operation) == {"get"}
    get = operation["get"]
    assert get["operationId"] == "listExchangeFills"
    parameters = {parameter["name"]: parameter for parameter in get["parameters"]}
    assert set(parameters) == {"exchange", "from", "to", "limit", "offset"}
    assert all(parameter["in"] == "query" for parameter in parameters.values())
    assert all(parameter.get("required", False) is False for parameter in parameters.values())
    limit = parameters["limit"]["schema"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 200, 50)
    offset = parameters["offset"]["schema"]
    assert (offset["minimum"], offset["maximum"], offset["default"]) == (0, LARGEST_OFFSET, 0)
    response = get["responses"]["200"]["content"]["application/json"]["schema"]
    assert response == {"$ref": "#/components/schemas/ExchangeFillListResponse"}


def test_the_side_is_a_word_in_the_schema(app: FastAPI) -> None:
    """The frontend shows the side as a word; the schema says which two words exist."""
    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]

    assert set(schemas["FillSide"]["enum"]) == {side.value for side in FillSide}
