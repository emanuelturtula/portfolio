"""Criteria 1 to 9 of #18, over HTTP: `/api/accounting/adjustments` and what it changes.

The whole stack runs -- middleware, router, the adjustment service, the recompute trigger the
application publishes, the accounting service, SQLite. A test that says "the positions
changed" reads `GET /api/accounting/positions` **after** the change's response, and never runs
a recompute itself, so the recompute it observes is the one the request ran before answering.

Every expected figure is worked by hand beside the literal, on the same history
`tests/services/test_accounting_adjustments.py` checks against the `Fraction` oracle: 1 BTC
bought for 30000, then 1.5 BTC sold for 75000.

## Money on the wire

Every amount is a JSON **string** at eighteen places, both ways. A JSON number is refused on
the way in -- an integer as well as a float -- and the bodies that carry one are sent as raw
text, so that no float literal appears in this file either.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.api.errors import PROBLEM_CONTENT_TYPE
from portfolio.api.middleware import PUBLIC_API_PATHS
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.main import run_accounting_recompute
from portfolio.services.accounting import AccountingStatus, RecomputeReason
from portfolio.services.adjustments import ASSET_SYMBOL_PATTERN, NOTE_MAX_LENGTH
from tests.accounting_harness import (
    at,
    plant_account,
    plant_fills,
    plant_owner,
    plant_unconvertible_fill,
    rows,
)
from tests.adjustments_harness import (
    ADJUSTMENT_FIELDS,
    ADJUSTMENTS_PATH,
    ADJUSTMENTS_SQL,
    POSITIONS_PATH,
    body,
    iso,
    plant_adjustment,
)
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in
from tests.exchange_sync_harness import make_fill

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping, Sequence

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import NormalizedFill

BOUND: Final = 5
NOT_FOUND_DETAIL: Final = "No adjustment with that id."
OPERATION_IDS: Final = {
    ("get", ADJUSTMENTS_PATH): "listAdjustments",
    ("post", ADJUSTMENTS_PATH): "createAdjustment",
    ("put", f"{ADJUSTMENTS_PATH}/{{adjustment_id}}"): "replaceAdjustment",
    ("delete", f"{ADJUSTMENTS_PATH}/{{adjustment_id}}"): "deleteAdjustment",
}


# --------------------------------------------------------------------------------------
# The application, the owner's history, and what the trigger saw
# --------------------------------------------------------------------------------------


async def until(condition: Callable[[], bool]) -> None:
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


async def settled(app: FastAPI) -> None:
    """Wait for the lifespan's startup recompute, so no test races it."""
    task: asyncio.Task[Any] = app.state.accounting_startup_task
    await asyncio.wait_for(until(task.done), timeout=BOUND)


async def owner_id(app: FastAPI) -> int:
    async with app.state.db_sessionmaker() as session:
        found = await session.scalar(text("SELECT id FROM users WHERE username = 'owner'"))
    return int(found)


async def plant_history(app: FastAPI, fills: Sequence[NormalizedFill]) -> int:
    """The owner's Bitget account and `fills`, then the recompute a sync would run."""
    user_id = await owner_id(app)
    async with app.state.db_sessionmaker() as session:
        account = await plant_account(session, user_id, ExchangeKey.BITGET)
        await plant_fills(session, account, fills)
    status = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
    assert status.error is None, status
    return account


def buy(trade_id: int, when: datetime, quantity: str, cost: str) -> NormalizedFill:
    return make_fill(
        trade_id,
        when,
        quantity=quantity,
        price=str(Decimal(cost) / Decimal(quantity)),
        quote_quantity=cost,
        fee_amount="0",
        fee_asset=None,
    )


def sell(trade_id: int, when: datetime, quantity: str, proceeds: str) -> NormalizedFill:
    return make_fill(
        trade_id,
        when,
        side=FillSide.SELL,
        quantity=quantity,
        price=str(Decimal(proceeds) / Decimal(quantity)),
        quote_quantity=proceeds,
        fee_amount="0",
        fee_asset=None,
    )


#: 1 BTC bought for 30000, then 1.5 BTC sold for 75000: a sale 0.5 past the history.
OVERSOLD: Final = (buy(1001, at(10), "1", "30000"), sell(1002, at(20), "1.5", "75000"))


class RecordingRecompute:
    """A stand-in for `app.state.accounting_recompute` that records and then runs the real one.

    At each call it reads the adjustments table over a session of its own, which sees only
    what was committed: a trigger called before the commit would find the change missing.
    """

    def __init__(self, app: FastAPI, *, error: Exception | None = None) -> None:
        self.app = app
        self.real: Callable[[RecomputeReason], Awaitable[AccountingStatus]] = (
            app.state.accounting_recompute
        )
        self.error = error
        self.reasons: list[RecomputeReason] = []
        self.committed: list[list[dict[str, Any]]] = []
        self.finished = 0

    async def __call__(self, reason: RecomputeReason) -> AccountingStatus:
        self.reasons.append(reason)
        self.committed.append(await rows(self.app.state.db_sessionmaker, ADJUSTMENTS_SQL))
        if self.error is not None:
            raise self.error
        status = await self.real(reason)
        self.finished += 1
        return status


def recording(app: FastAPI, *, error: Exception | None = None) -> RecordingRecompute:
    recorder = RecordingRecompute(app, error=error)
    app.state.accounting_recompute = recorder
    return recorder


async def post(client: AsyncClient, payload: dict[str, Any]) -> Response:
    return await client.post(ADJUSTMENTS_PATH, json=payload, headers=JSON_HEADERS)


async def post_ok(client: AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = await post(client, payload)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def put(client: AsyncClient, adjustment_id: int, payload: dict[str, Any]) -> Response:
    return await client.put(
        f"{ADJUSTMENTS_PATH}/{adjustment_id}", json=payload, headers=JSON_HEADERS
    )


async def remove(client: AsyncClient, adjustment_id: int) -> Response:
    return await client.delete(f"{ADJUSTMENTS_PATH}/{adjustment_id}", headers=JSON_HEADERS)


async def listed(client: AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(ADJUSTMENTS_PATH)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"adjustments"}
    adjustments: list[dict[str, Any]] = payload["adjustments"]
    return adjustments


async def positions(client: AsyncClient) -> dict[str, Any]:
    response = await client.get(POSITIONS_PATH)
    assert response.status_code == 200, response.text
    served: dict[str, Any] = response.json()
    return served


def position(served: dict[str, Any], asset: str) -> dict[str, Any]:
    (found,) = [entry for entry in served["positions"] if entry["asset"] == asset]
    entry: dict[str, Any] = found
    return entry


def dec(value: object) -> Decimal:
    assert isinstance(value, str), f"{value!r} is not a JSON string"
    return Decimal(value)


# --------------------------------------------------------------------------------------
# Criterion 1: the four routes
# --------------------------------------------------------------------------------------


async def test_create_list_replace_and_delete_round_trip(
    api_app: FastAPI, signed_in_api_client: AsyncClient
) -> None:
    """Every route, in the order an owner meets them, with the shape each one answers in."""
    await settled(api_app)
    client = signed_in_api_client

    created = await post_ok(
        client,
        body(
            quantity="0.5",
            unit_cost="30000",
            occurred_at="2025-06-01T14:00:00+02:00",
            note="  Bought before the imported history  ",
        ),
    )

    assert set(created) == ADJUSTMENT_FIELDS
    assert isinstance(created["id"], int)
    assert created["asset"] == "BTC"
    assert created["quantity"] == "0.500000000000000000"
    assert created["unit_cost"] == "30000.000000000000000000"
    assert datetime.fromisoformat(created["occurred_at"]) == datetime(2025, 6, 1, 12, tzinfo=UTC)
    assert datetime.fromisoformat(created["occurred_at"]).utcoffset() == timedelta(0)
    assert created["note"] == "  Bought before the imported history  "
    assert created["created_at"] == created["updated_at"]
    assert await listed(client) == [created]

    replaced = await put(
        client,
        created["id"],
        body(asset="KAS", quantity="1000", unit_cost=None, note="It was KAS"),
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["id"] == created["id"]
    assert (replaced.json()["asset"], replaced.json()["unit_cost"]) == ("KAS", None)
    assert replaced.json()["quantity"] == "1000.000000000000000000"
    assert replaced.json()["created_at"] == created["created_at"]
    assert await listed(client) == [replaced.json()]

    deleted = await remove(client, created["id"])
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert await listed(client) == []
    again = await remove(client, created["id"])
    assert again.status_code == 404, "a repeated delete is a 404: the id names nothing now"


async def test_post_may_omit_the_cost_and_that_is_unknown_not_zero(
    api_app: FastAPI, signed_in_api_client: AsyncClient
) -> None:
    await settled(api_app)

    created = await post_ok(signed_in_api_client, body(omit=("unit_cost",)))

    assert created["unit_cost"] is None


async def test_put_requires_the_cost_to_be_present_even_as_null(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`PUT` is a full replacement: an omitted cost cannot say "unknown" or "unchanged"."""
    await settled(api_app)
    created = await post_ok(signed_in_api_client, body())
    before = await rows(api_sessionmaker, ADJUSTMENTS_SQL)

    omitted = await put(signed_in_api_client, created["id"], body(omit=("unit_cost",)))
    explicit = await put(signed_in_api_client, created["id"], body(unit_cost=None))

    assert omitted.status_code == 422, omitted.text
    assert [error["loc"] for error in omitted.json()["errors"]] == [["body", "unit_cost"]]
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["unit_cost"] is None
    after = await rows(api_sessionmaker, ADJUSTMENTS_SQL)
    assert before[0]["unit_cost"] is not None
    assert after[0]["unit_cost"] is None


async def test_an_unknown_field_is_refused(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`extra="forbid"`: a misspelt `unit_cst` must not silently record an unknown cost."""
    await settled(api_app)
    payload = body(omit=("unit_cost",))
    payload["unit_cst"] = "30000"

    response = await post(signed_in_api_client, payload)

    assert response.status_code == 422, response.text
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == []


async def test_the_list_is_in_replay_order(
    api_app: FastAPI, signed_in_api_client: AsyncClient
) -> None:
    """By the instant, then the id: 10:00 at +02:00 is before 09:00 UTC."""
    await settled(api_app)
    client = signed_in_api_client
    nine = await post_ok(client, body(occurred_at="2025-06-01T09:00:00Z"))
    eight = await post_ok(client, body(occurred_at="2025-06-01T10:00:00+02:00"))
    tie = await post_ok(client, body(occurred_at="2025-06-01T09:00:00Z"))

    assert [entry["id"] for entry in await listed(client)] == [eight["id"], nine["id"], tie["id"]]


# --------------------------------------------------------------------------------------
# Criterion 2, end to end: the opening balance repairs the oversold history
# --------------------------------------------------------------------------------------


async def test_an_opening_balance_removes_the_warning_and_the_flag_by_the_time_it_returns(
    api_app: FastAPI, signed_in_api_client: AsyncClient
) -> None:
    """Before: a 0.5 BTC shortfall, `history_incomplete`, 20000 realized, 25000 unmatched.

    After `POST` of 1 BTC at 20000, dated before the history: 2 BTC at a basis of 50000 before
    the sale, 1.5 sold at an average of 25000, so 75000 - 37500 = 37500 realized; 0.5 BTC left
    at 12500. Nothing here recomputes: the request did.
    """
    await settled(api_app)
    await plant_history(api_app, OVERSOLD)
    before = await positions(signed_in_api_client)
    assert [warning["kind"] for warning in before["warnings"]] == ["negative_inventory"]
    assert position(before, "BTC")["flags"] == ["history_incomplete"]
    assert dec(position(before, "BTC")["realized_pnl"]) == Decimal(20000)
    assert dec(position(before, "BTC")["unmatched_proceeds"]) == Decimal(25000)

    await post_ok(
        signed_in_api_client,
        body(quantity="1", unit_cost="20000", occurred_at=iso(at(0))),
    )
    after = await positions(signed_in_api_client)

    btc = position(after, "BTC")
    assert after["warnings"] == []
    assert btc["flags"] == []
    assert dec(btc["realized_pnl"]) == Decimal(37500)
    assert dec(btc["unmatched_proceeds"]) == 0
    assert dec(btc["quantity"]) == Decimal("0.5")
    assert dec(btc["total_invested"]) == Decimal(12500)
    assert dec(btc["average_cost"]) == Decimal(25000)
    assert after["event_count"] == 3
    assert after["last_recompute"]["outcome"] == "written"


# --------------------------------------------------------------------------------------
# Criterion 3: no cost is unknown basis, in every positions response
# --------------------------------------------------------------------------------------


async def test_an_adjustment_without_a_cost_is_unknown_basis_and_its_sale_realizes_nothing(
    api_app: FastAPI, signed_in_api_client: AsyncClient
) -> None:
    """1 BTC of unknown cost, then 0.4 sold for 20000.

    By hand: nothing realized, 20000 unmatched, 0.6 BTC left of unknown cost -- flagged, with
    no average, and out of the totals as `unknown_basis`. Read twice: the flag and the quantity
    are in every positions response, not only the first after the change.
    """
    await settled(api_app)
    await plant_history(api_app, [sell(1101, at(10), "0.4", "20000")])

    await post_ok(signed_in_api_client, body(quantity="1", unit_cost=None, occurred_at=iso(at(0))))
    served = [await positions(signed_in_api_client) for _ in range(2)]

    assert served[0] == served[1]
    btc = position(served[0], "BTC")
    assert btc["flags"] == ["unknown_basis"]
    assert dec(btc["quantity"]) == Decimal("0.6")
    assert dec(btc["unknown_basis_quantity"]) == Decimal("0.6")
    assert dec(btc["total_invested"]) == 0
    assert btc["average_cost"] is None
    assert dec(btc["realized_pnl"]) == 0, "never valued at zero cost: no profit is realized"
    assert dec(btc["unmatched_proceeds"]) == Decimal(20000)
    assert {"asset": "BTC", "reason": "unknown_basis"} in served[0]["totals"]["excluded"]
    assert dec(served[0]["totals"]["realized_pnl"]) == 0


# --------------------------------------------------------------------------------------
# Criterion 5: every change recomputes before it answers; a failure never loses the change
# --------------------------------------------------------------------------------------


async def test_positions_follow_each_change_by_the_time_its_response_arrives(
    api_app: FastAPI, signed_in_api_client: AsyncClient
) -> None:
    """Create 1 BTC, replace it with 2, delete it: the positions read after each answer agree."""
    await settled(api_app)
    client = signed_in_api_client

    created = await post_ok(client, body(quantity="1", unit_cost="100"))
    after_create = await positions(client)
    replaced = await put(client, created["id"], body(quantity="2", unit_cost="100"))
    assert replaced.status_code == 200, replaced.text
    after_replace = await positions(client)
    deleted = await remove(client, created["id"])
    assert deleted.status_code == 204
    after_delete = await positions(client)

    assert dec(position(after_create, "BTC")["quantity"]) == 1
    assert dec(position(after_create, "BTC")["total_invested"]) == 100
    assert dec(position(after_replace, "BTC")["quantity"]) == 2
    assert dec(position(after_replace, "BTC")["total_invested"]) == 200
    assert (after_delete["event_count"], after_delete["positions"]) == (0, [])


async def test_each_change_triggers_one_adjustment_recompute_after_its_commit(
    api_app: FastAPI, signed_in_api_client: AsyncClient
) -> None:
    """Reads and refusals trigger nothing; each change triggers once, and saw itself committed."""
    await settled(api_app)
    recorder = recording(api_app)
    client = signed_in_api_client

    created = await post_ok(client, body(quantity="1"))
    assert [row["id"] for row in recorder.committed[-1]] == [created["id"]]
    replaced = await put(client, created["id"], body(asset="KAS", quantity="5"))
    assert replaced.status_code == 200
    assert [row["asset"] for row in recorder.committed[-1]] == ["KAS"]
    await listed(client)
    assert (await post(client, body(asset="usdt"))).status_code == 422
    assert (await put(client, created["id"] + 1000, body())).status_code == 404
    assert (await remove(client, created["id"])).status_code == 204
    assert recorder.committed[-1] == []
    assert (await remove(client, created["id"])).status_code == 404

    assert recorder.reasons == [RecomputeReason.ADJUSTMENT] * 3


async def test_a_failed_recompute_keeps_the_change_and_says_failed(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A stored fill the engine cannot replay fails the recompute the `POST` sets off.

    The `POST` still answers 201, the adjustment is saved and listed, `last_recompute` says
    `failed` with the class name only, and the previous snapshot is still the one served.
    """
    await settled(api_app)
    account = await plant_history(api_app, OVERSOLD)
    before = await positions(signed_in_api_client)
    async with api_sessionmaker() as session:
        await plant_unconvertible_fill(
            session, account, trade_id="tid-UNCV-adjust", shape="same_asset"
        )

    created = await post_ok(signed_in_api_client, body(occurred_at=iso(at(0))))
    after = await positions(signed_in_api_client)

    assert [entry["id"] for entry in await listed(signed_in_api_client)] == [created["id"]]
    assert after["last_recompute"]["outcome"] == "failed"
    assert after["last_recompute"]["error"] == "UnconvertibleFillError"
    del before["last_recompute"], after["last_recompute"]
    assert after == before, "the previous snapshot is still served"


async def test_a_trigger_that_raises_still_answers_with_the_saved_change(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Belt and braces: the real trigger never raises, and a replaced one that does is logged."""
    await settled(api_app)
    recording(api_app, error=RuntimeError("trigger-message-" + "Jn7" * 4))

    response = await post(signed_in_api_client, body())

    assert response.status_code == 201, response.text
    assert "trigger-message" not in response.text
    assert [row["id"] for row in await rows(api_sessionmaker, ADJUSTMENTS_SQL)] == [
        response.json()["id"]
    ]


async def test_the_request_waits_for_a_recompute_already_running(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The trigger's lock serialises a request's recompute with a sync's (spec 023).

    With the lock held -- a sync's recompute in progress -- the `POST` has committed its row
    but has not answered. Released, it recomputes and answers, and the positions show it.
    """
    await settled(api_app)
    lock: asyncio.Lock = api_app.state.accounting_lock

    async with lock:
        pending = asyncio.create_task(post(signed_in_api_client, body(quantity="3")))
        await asyncio.wait_for(until_rows(api_sessionmaker, count=1), timeout=BOUND)
        await asyncio.sleep(0.05)
        assert not pending.done(), "the request answered without waiting for the recompute"

    response = await asyncio.wait_for(pending, timeout=BOUND)
    assert response.status_code == 201, response.text
    assert dec(position(await positions(signed_in_api_client), "BTC")["quantity"]) == 3


async def until_rows(factory: async_sessionmaker[AsyncSession], *, count: int) -> None:
    while len(await rows(factory, ADJUSTMENTS_SQL)) < count:  # noqa: ASYNC110
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------------------
# Criteria 6, 7 and 9: refused at entry, with a 422 that names no value
# --------------------------------------------------------------------------------------

#: Distinctive values, so that finding one in a response is an echo, not a coincidence.
LEAKY_NOTE: Final = "note-sentinel-" + "Pq4" * 6
LEAKY_ASSET: Final = "zqxwvk"

REFUSED: Final[tuple[tuple[str, dict[str, Any], str, str | None, tuple[str, ...]], ...]] = (
    ("blank note", body(note="   "), "note", "note must not be blank", ()),
    (
        "note over 500 characters",
        body(note=LEAKY_NOTE + "n" * 500),
        "note",
        "note must be at most 500 characters",
        (LEAKY_NOTE,),
    ),
    (
        "lower-case asset",
        body(asset=LEAKY_ASSET),
        "asset",
        "asset must be the symbol exactly as the exchange spells it: 1 to 20 upper-case "
        "letters or digits, such as BTC",
        (LEAKY_ASSET,),
    ),
    (
        "cash asset",
        body(asset="USDT"),
        "asset",
        "asset must not be a cash asset (USDC, USDT): cash is the unit of account, and an "
        "adjustment of it changes nothing",
        (),
    ),
    ("zero quantity", body(quantity="0"), "quantity", "quantity must be greater than zero", ()),
    (
        "negative quantity",
        body(quantity="-7777.123"),
        "quantity",
        "quantity must be greater than zero",
        ("7777.123",),
    ),
    (
        "negative unit cost",
        body(unit_cost="-6666.5"),
        "unit_cost",
        "unit_cost must not be negative",
        ("6666.5",),
    ),
    (
        "too many fractional digits",
        body(quantity="0.1234567890123456789"),
        "quantity",
        "quantity has more than 18 decimal places",
        ("1234567890123456789",),
    ),
    (
        "too many integer digits",
        body(unit_cost="123456789012345678901"),
        "unit_cost",
        "unit_cost has more than 20 digits before the decimal point",
        ("123456789012345678901",),
    ),
    (
        "total cost overflow",
        body(quantity="31415926535", unit_cost="27182818284"),
        "unit_cost",
        "unit_cost times quantity has more than 20 digits before the decimal point",
        ("31415926535", "27182818284"),
    ),
    (
        "naive datetime",
        body(occurred_at="2025-06-01T12:34:56"),
        "occurred_at",
        "occurred_at must be a timezone-aware datetime",
        ("12:34:56",),
    ),
    (
        "future datetime",
        body(occurred_at="2999-03-04T05:06:07Z"),
        "occurred_at",
        "occurred_at must not be later than now",
        ("2999",),
    ),
    (
        "datetime not representable in UTC",
        body(occurred_at="0001-01-01T00:00:00+05:00"),
        "occurred_at",
        "occurred_at is outside the range a UTC datetime can represent",
        (),
    ),
    ("missing note", body(omit=("note",)), "note", None, ()),
    # Neither a string nor a number: passed on by the ISO parser, refused by Pydantic's own
    # datetime validation, and never read as anything.
    ("null occurred_at", body(occurred_at=None), "occurred_at", None, ()),
    ("object occurred_at", body(occurred_at={"year": 2026}), "occurred_at", None, ()),
    ("missing occurred_at", body(omit=("occurred_at",)), "occurred_at", None, ()),
    ("null note", body(note=None), "note", None, ()),
    ("missing quantity", body(omit=("quantity",)), "quantity", None, ()),
    ("null quantity", body(quantity=None), "quantity", None, ()),
    ("malformed quantity", body(quantity="12,5x"), "quantity", None, ("12,5x",)),
    ("NaN quantity", body(quantity="NaN"), "quantity", None, ()),
    ("boolean quantity", body(quantity=True), "quantity", None, ()),
)


@pytest.mark.parametrize(
    ("payload", "field", "rule", "echoes"),
    [(payload, field, rule, echoes) for _name, payload, field, rule, echoes in REFUSED],
    ids=[name for name, *_rest in REFUSED],
)
async def test_each_refusal_is_a_field_level_422_that_echoes_nothing_and_stores_nothing(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    field: str,
    rule: str | None,
    echoes: tuple[str, ...],
) -> None:
    await settled(api_app)
    recorder = recording(api_app)

    response = await post(signed_in_api_client, payload)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    problem = response.json()
    assert problem["status"] == 422
    assert [error["loc"] for error in problem["errors"]] == [["body", field]]
    if rule is not None:
        assert problem["errors"] == [{"loc": ["body", field], "msg": rule, "type": "value_error"}]
    for value in echoes:
        assert value not in response.text, "the 422 echoed the value it refused"
    assert all(set(error) == {"loc", "msg", "type"} for error in problem["errors"]), (
        "no `input` echo and no `ctx`"
    )
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == []
    assert recorder.reasons == []


@pytest.mark.parametrize(
    ("field", "raw"),
    [
        ("quantity", "7"),
        ("quantity", "0.5"),
        ("quantity", "4242.4242"),
        ("quantity", "1e2"),
        ("unit_cost", "30000"),
        ("unit_cost", "20000.5"),
    ],
    ids=["integer", "decimal", "distinctive", "exponent", "cost integer", "cost decimal"],
)
async def test_a_json_number_for_money_is_refused(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    field: str,
    raw: str,
) -> None:
    """Criterion 9: an integer as well as a float. Sent as raw JSON text, never as a float."""
    await settled(api_app)
    placeholder = "__NUMBER__"
    payload = body()
    payload[field] = placeholder
    document = json.dumps(payload).replace(f'"{placeholder}"', raw)

    response = await signed_in_api_client.post(
        ADJUSTMENTS_PATH, content=document, headers=JSON_HEADERS
    )

    assert response.status_code == 422, response.text
    (error,) = response.json()["errors"]
    assert error["loc"] == ["body", field]
    assert error["msg"] == "Value error, a monetary value must arrive as a JSON string"
    assert error["type"] == "value_error"
    if len(raw) > 3:
        assert raw not in response.text
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == []


async def test_a_refused_replacement_leaves_the_row_as_it_was(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """And a refused body on a missing id is a 422: the body is judged before the id."""
    await settled(api_app)
    created = await post_ok(signed_in_api_client, body())
    before = await rows(api_sessionmaker, ADJUSTMENTS_SQL)

    refused = await put(signed_in_api_client, created["id"], body(note=" "))
    missing = await put(signed_in_api_client, created["id"] + 1000, body(note=" "))

    assert (refused.status_code, missing.status_code) == (422, 422)
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == before


async def test_a_lone_surrogate_note_is_refused_not_a_500(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """JSON can carry `\\ud800`; UTF-8 cannot. The service refuses it before SQLite fails on it."""
    await settled(api_app)
    escaped = chr(92) + "ud800"
    document = json.dumps(body(note="Opening balance")).replace("Opening balance", escaped)

    response = await signed_in_api_client.post(
        ADJUSTMENTS_PATH, content=document, headers=JSON_HEADERS
    )

    assert response.status_code == 422, response.text
    assert response.json()["errors"] == [
        {
            "loc": ["body", "note"],
            "msg": "note must be text that encodes as UTF-8",
            "type": "value_error",
        }
    ]
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == []


async def test_money_leaves_as_strings_at_eighteen_places(
    api_app: FastAPI, signed_in_api_client: AsyncClient
) -> None:
    """Against the bytes: no amount is ever a bare JSON number on the way out."""
    await settled(api_app)
    await post_ok(signed_in_api_client, body(quantity="1.5", unit_cost="0"))
    await post_ok(signed_in_api_client, body(quantity="0.000000000000000001", unit_cost=None))

    response = await signed_in_api_client.get(ADJUSTMENTS_PATH)

    for field in ("quantity", "unit_cost"):
        assert not re.search(rf'"{field}"\s*:\s*[-0-9]', response.text), field
    first, second = response.json()["adjustments"]
    assert (first["quantity"], first["unit_cost"]) == (
        "1.500000000000000000",
        "0.000000000000000000",
    )
    assert (second["quantity"], second["unit_cost"]) == ("0.000000000000000001", None)


# --------------------------------------------------------------------------------------
# Criterion 8: authenticated, owner-scoped, and the allowlist untouched
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ADJUSTMENTS_PATH),
        ("POST", ADJUSTMENTS_PATH),
        ("PUT", f"{ADJUSTMENTS_PATH}/1"),
        ("DELETE", f"{ADJUSTMENTS_PATH}/1"),
    ],
)
async def test_the_routes_require_a_session_and_write_nothing_without_one(
    api_app: FastAPI,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    method: str,
    path: str,
) -> None:
    await settled(api_app)
    user_id = await owner_id(api_app)
    async with api_sessionmaker() as session:
        await plant_adjustment(session, user_id, occurred_at=at(0), adjustment_id=1)
    before = await rows(api_sessionmaker, ADJUSTMENTS_SQL)
    transport = ASGITransport(app=api_app)

    async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
        response = await anonymous.request(method, path, json=body(), headers=JSON_HEADERS)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == before


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_another_owners_adjustment_is_the_same_404_as_a_missing_one(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    method: str,
) -> None:
    """Nothing in the answer tells "not yours" from "not there", and nothing of theirs moves."""
    await settled(api_app)
    async with api_sessionmaker() as session:
        stranger = await plant_owner(session, "stranger")
        theirs = await plant_adjustment(session, stranger, occurred_at=at(0), note=LEAKY_NOTE)
    before = await rows(api_sessionmaker, ADJUSTMENTS_SQL)

    answers = []
    for adjustment_id in (theirs, theirs + 1000):
        response = await signed_in_api_client.request(
            method,
            f"{ADJUSTMENTS_PATH}/{adjustment_id}",
            json=body(),
            headers=JSON_HEADERS,
        )
        assert response.status_code == 404, response.text
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        problem = response.json()
        assert problem["instance"] == f"{ADJUSTMENTS_PATH}/{adjustment_id}"
        del problem["instance"]
        answers.append(problem)
        assert LEAKY_NOTE not in response.text

    assert answers[0] == answers[1]
    assert answers[0]["detail"] == NOT_FOUND_DETAIL
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == before
    assert await listed(signed_in_api_client) == [], "their adjustment is not listed as mine"


def test_the_public_allowlist_is_unchanged() -> None:
    assert frozenset({"/api/health", "/api/auth/login"}) == PUBLIC_API_PATHS
    assert not any(path.startswith(ADJUSTMENTS_PATH) for path in PUBLIC_API_PATHS)


def test_the_operations_are_in_the_schema_under_their_ids(app: FastAPI) -> None:
    paths = app.openapi()["paths"]

    found = {(method, path): paths[path][method]["operationId"] for method, path in OPERATION_IDS}

    assert found == OPERATION_IDS
    assert set(paths[ADJUSTMENTS_PATH]) == {"get", "post"}
    assert set(paths[f"{ADJUSTMENTS_PATH}/{{adjustment_id}}"]) == {"put", "delete"}


def test_every_amount_is_declared_a_string_in_the_schema(app: FastAPI) -> None:
    """The generated client is built from this: an amount it types as a number is a float."""
    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]

    def declared(definition: dict[str, Any]) -> set[str]:
        if "anyOf" in definition:
            return {str(option.get("type")) for option in definition["anyOf"]}
        return {str(definition.get("type"))}

    for name in ("AdjustmentCreateRequest", "AdjustmentReplaceRequest", "AdjustmentResponse"):
        properties = schemas[name]["properties"]
        assert declared(properties["quantity"]) == {"string"}, name
        assert declared(properties["unit_cost"]) == {"string", "null"}, name
    assert "unit_cost" in schemas["AdjustmentReplaceRequest"]["required"]
    assert "unit_cost" not in schemas["AdjustmentCreateRequest"].get("required", [])
    described = schemas["AdjustmentResponse"]["properties"]["unit_cost"]["description"]
    assert "not zero" in described
    assert "unknown_basis" in described


# --------------------------------------------------------------------------------------
# Spec 023, R5 and R6: no JSON number for an instant, and the limits in the schema
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["1700000000", "1700000000.25", "true"],
    ids=["unix seconds", "fractional seconds", "boolean"],
)
async def test_a_json_number_for_the_instant_is_refused_not_read_as_a_timestamp(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    raw: str,
) -> None:
    """R5: Pydantic would read `1700000000` as a Unix timestamp; the contract is ISO 8601."""
    await settled(api_app)
    placeholder = "__INSTANT__"
    document = json.dumps(body(occurred_at=placeholder)).replace(f'"{placeholder}"', raw)

    response = await signed_in_api_client.post(
        ADJUSTMENTS_PATH, content=document, headers=JSON_HEADERS
    )

    assert response.status_code == 422, response.text
    assert response.json()["errors"] == [
        {
            "loc": ["body", "occurred_at"],
            "msg": "Value error, occurred_at must arrive as an ISO 8601 string with a "
            "timezone, not as a JSON number",
            "type": "value_error",
        }
    ]
    assert "1700000000" not in response.text
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == []


def test_the_schema_publishes_the_services_limits(app: FastAPI) -> None:
    """R6: the asset pattern and the note's length, from the service's own constants.

    Metadata for a client's form (#111), not a second validator: the 422s above for `btc` and
    a 501-character note carry the service's sentences, not Pydantic's.
    """
    schemas: dict[str, Any] = app.openapi()["components"]["schemas"]

    for name in ("AdjustmentCreateRequest", "AdjustmentReplaceRequest"):
        properties = schemas[name]["properties"]
        assert properties["asset"]["pattern"] == ASSET_SYMBOL_PATTERN == "^[A-Z0-9]{1,20}$", name
        assert properties["note"]["maxLength"] == NOTE_MAX_LENGTH == 500, name


# --------------------------------------------------------------------------------------
# Criterion 5, at the protocol: the recompute is over before the response begins
# --------------------------------------------------------------------------------------


async def test_the_recompute_has_finished_before_the_response_starts(
    api_app: FastAPI,
) -> None:
    """Not merely inside the ASGI call: a Starlette `BackgroundTask` also runs inside it.

    A background task runs after `http.response.start` and the body have been sent, so a
    client could read the positions before it ran. This wraps the application's `send` and
    records, at the moment each change's response starts, how many recomputes had already
    **finished**. Each must be one more than before.
    """
    await settled(api_app)
    recorder = recording(api_app)
    starts: list[tuple[str, int]] = []

    async def spied(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        async def watched(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start" and scope["path"].startswith(
                ADJUSTMENTS_PATH
            ):
                starts.append((str(scope["method"]), recorder.finished))
            await send(message)

        await api_app(scope, receive, watched)

    async with AsyncClient(transport=ASGITransport(app=spied), base_url=BASE_URL) as client:
        await sign_in(client)
        created = await post_ok(client, body(quantity="1"))
        replaced = await put(client, created["id"], body(quantity="2"))
        deleted = await remove(client, created["id"])

    assert (replaced.status_code, deleted.status_code) == (200, 204)
    assert starts == [("POST", 1), ("PUT", 2), ("DELETE", 3)]
    assert recorder.reasons == [RecomputeReason.ADJUSTMENT] * 3


# --------------------------------------------------------------------------------------
# Spec 023, R8: an instant is ISO 8601, never Unix time; an id is within 64 bits
# --------------------------------------------------------------------------------------

FORMAT_REFUSAL: Final = (
    "Value error, occurred_at must be an ISO 8601 datetime with a timezone, such as "
    "2026-01-01T00:00:00Z"
)
NAIVE_REFUSAL: Final = "occurred_at must be a timezone-aware datetime"


@pytest.mark.parametrize(
    ("spelled", "msg"),
    [
        ("1767225600", FORMAT_REFUSAL),
        ("1767225600.5", FORMAT_REFUSAL),
        (" 2026-01-01T00:00:00Z", FORMAT_REFUSAL),
        ("next tuesday", FORMAT_REFUSAL),
        ("20260101", NAIVE_REFUSAL),
        ("2026-01-01", NAIVE_REFUSAL),
    ],
    ids=["unix seconds", "unix fractional", "leading space", "prose", "basic date", "date"],
)
async def test_an_instant_that_is_not_an_aware_iso_datetime_is_refused(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    spelled: str,
    msg: str,
) -> None:
    """R8's must-fix: Pydantic's lax parser read a string of digits as Unix time.

    `"1767225600"` was stored as 2026-01-01, and `"20260101"` -- ISO 8601's basic date -- as
    1970-08-23, before the whole history. Now a string that is not ISO 8601 is a fixed
    refusal, and one that parses to a naive instant meets the service's aware rule. Neither
    quotes what was sent.
    """
    await settled(api_app)
    recorder = recording(api_app)

    response = await post(signed_in_api_client, body(occurred_at=spelled))

    assert response.status_code == 422, response.text
    assert response.json()["errors"] == [
        {"loc": ["body", "occurred_at"], "msg": msg, "type": "value_error"}
    ]
    if spelled.strip() not in msg:
        assert spelled.strip() not in response.text, "the refusal quoted what was sent"
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == []
    assert recorder.reasons == []


@pytest.mark.parametrize(
    "spelled",
    ["2026-01-01T00:00:00Z", "2026-01-01T02:00:00+02:00", "20260101T000000Z"],
    ids=["extended Z", "extended offset", "basic Z"],
)
async def test_an_aware_iso_instant_is_accepted_in_either_iso_format(
    api_app: FastAPI, signed_in_api_client: AsyncClient, spelled: str
) -> None:
    """The control: every spelling of 2026-01-01T00:00Z is the same stored instant."""
    await settled(api_app)

    created = await post_ok(signed_in_api_client, body(occurred_at=spelled))

    assert datetime.fromisoformat(created["occurred_at"]) == datetime(2026, 1, 1, tzinfo=UTC)


LARGEST_ID: Final = 2**63 - 1


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
@pytest.mark.parametrize(
    ("adjustment_id", "bound"),
    [
        (2**63, "less than or equal to 9223372036854775807"),
        (10**20, "less than or equal to 9223372036854775807"),
        (0, "greater than or equal to 1"),
        (-1, "greater than or equal to 1"),
    ],
    ids=["two to the 63", "ten to the 20", "zero", "negative"],
)
async def test_an_id_outside_sqlites_range_is_a_422_not_a_500(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    method: str,
    adjustment_id: int,
    bound: str,
) -> None:
    """R8: an id past 64 bits made SQLite raise, a 500. Now the path refuses it, and says why.

    The refusal quotes the bound, never the id that was sent. Zero and negative ids are
    outside the range an `AUTOINCREMENT` table assigns, and are refused the same way.
    """
    await settled(api_app)
    created = await post_ok(signed_in_api_client, body())
    before = await rows(api_sessionmaker, ADJUSTMENTS_SQL)
    recorder = recording(api_app)

    response = await signed_in_api_client.request(
        method, f"{ADJUSTMENTS_PATH}/{adjustment_id}", json=body(), headers=JSON_HEADERS
    )

    assert response.status_code == 422, response.text
    (error,) = response.json()["errors"]
    assert error["loc"] == ["path", "adjustment_id"]
    assert error["msg"] == f"Input should be {bound}"
    if abs(adjustment_id) > 1:
        assert str(adjustment_id) not in response.json()["errors"][0]["msg"]
    assert await rows(api_sessionmaker, ADJUSTMENTS_SQL) == before
    assert recorder.reasons == []
    assert created["id"] >= 1


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_the_largest_id_sqlite_can_assign_is_looked_up(
    api_app: FastAPI, signed_in_api_client: AsyncClient, method: str
) -> None:
    """The bound is inclusive: 2**63 - 1 is a real id nobody holds, so it is a 404."""
    await settled(api_app)

    response = await signed_in_api_client.request(
        method, f"{ADJUSTMENTS_PATH}/{LARGEST_ID}", json=body(), headers=JSON_HEADERS
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == NOT_FOUND_DETAIL
