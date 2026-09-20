"""`MoneyStr`: criterion 6, over a real HTTP round trip.

The assertion is made against the response **text**, not against `response.json()`. That is
the whole point of the criterion: `json.loads` turns `{"amount": 0.1}` and
`{"amount": "0.1"}` into two different Python objects, but a test that compares
`response.json()["amount"]` to `Decimal("0.1")` passes for both -- so it would go on passing
the day `PlainSerializer` is dropped and FastAPI starts emitting a bare JSON number. Reading
the bytes is the only way to see the quotation marks.

The router here is defined in this module and mounted on a `FastAPI()` built in this module.
Nothing is added to the shipped application: an extra operation would change
`/api/openapi.json`, turn the OpenAPI drift job red, and require
`frontend/src/api/generated/schema.ts` to be regenerated for a type that ships no endpoint.
The last test in this file pins that.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, Final, get_args, get_origin

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import BaseModel

from portfolio.api.schemas.money import MoneyStr

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BASE_URL: Final = "http://moneytest"

# 20 integer digits and 20 decimal ones: far past what an IEEE-754 double can carry, so a
# response that survives this intact cannot have gone through a float on the way out.
LOSSLESS = Decimal("12345678901234567890.12345678901234567890")
# The canonical example of a value binary floating point cannot hold at all.
ONE_TENTH = Decimal("0.1")


class Amount(BaseModel):
    """The throwaway response and request body."""

    amount: MoneyStr


def build_money_app() -> FastAPI:
    """A single-route application that exists only inside this module."""
    router = APIRouter()

    @router.get("/amount", response_model=Amount)
    async def read_amount() -> Amount:
        return Amount(amount=LOSSLESS)

    @router.get("/tenth", response_model=Amount)
    async def read_one_tenth() -> Amount:
        return Amount(amount=ONE_TENTH)

    @router.get("/exponent", response_model=Amount)
    async def read_an_exponent() -> Amount:
        return Amount(amount=Decimal("1E+2"))

    @router.post("/echo", response_model=Amount)
    async def echo_amount(body: Amount) -> Amount:
        return body

    application = FastAPI()
    application.include_router(router)
    return application


@pytest.fixture
def money_app() -> FastAPI:
    return build_money_app()


@pytest.fixture
async def money_client(money_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=money_app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


def quoted(field: str, value: str) -> re.Pattern[str]:
    """A pattern that matches `"field": "value"` and not `"field": value`."""
    return re.compile(rf'"{re.escape(field)}"\s*:\s*"{re.escape(value)}"')


async def post_raw_json(client: AsyncClient, body: bytes) -> Response:
    """POST a body exactly as written, so the JSON type of the value is under test.

    `json=` would re-encode a Python object and decide the JSON type itself, which is the
    one thing these tests are trying to control. The content type has to be set by hand for
    the same reason -- without it FastAPI never reaches the model at all.
    """
    return await client.post("/echo", content=body, headers={"content-type": "application/json"})


# --------------------------------------------------------------------------------------
# Criterion 6: the wire form is a JSON string.
# --------------------------------------------------------------------------------------


async def test_money_is_serialized_as_a_json_string(money_client: AsyncClient) -> None:
    """Asserted against the raw bytes, then against their parsed type."""
    response = await money_client.get("/amount")

    assert response.status_code == 200
    body = response.text
    assert quoted("amount", "12345678901234567890.12345678901234567890").search(body)
    # The unquoted spelling must be absent, or the assertion above could be matching a
    # substring of something larger.
    assert not re.search(r'"amount"\s*:\s*[-0-9]', body)

    parsed: dict[str, Any] = json.loads(body)
    amount: object = parsed["amount"]
    # Checked in this order on purpose: the negative is the claim, and it has to be made
    # before the positive narrows the value's static type and makes it look vacuous.
    assert not isinstance(amount, float | int)
    assert isinstance(amount, str)
    assert Decimal(amount) == LOSSLESS


async def test_a_value_a_double_cannot_hold_arrives_intact(money_client: AsyncClient) -> None:
    """The digits are the test. A double would have rounded them on the way out."""
    response = await money_client.get("/tenth")
    parsed: dict[str, Any] = json.loads(response.text)

    assert parsed == {"amount": "0.1"}
    assert Decimal(parsed["amount"]) == ONE_TENTH
    # What the same value looks like once a float has touched it: 55 digits of noise.
    assert str(Decimal.from_float(0.1)) != parsed["amount"]
    assert str(Decimal.from_float(0.1)).startswith("0.1000000000000000055511151231")


async def test_the_wire_form_is_never_scientific(money_client: AsyncClient) -> None:
    """`str(Decimal("1E+2"))` is `"1E+2"`: correct, and hostile to hand a client."""
    response = await money_client.get("/exponent")

    assert json.loads(response.text) == {"amount": "100"}
    assert "E+" not in response.text


def test_money_str_annotates_a_decimal() -> None:
    """The Python side of the contract: a `Decimal`, whatever the wire form is.

    Also the reason `MoneyStr` is imported at module scope rather than inside a
    type-checking block: Pydantic resolves the annotation at runtime to build the model,
    so the name has to be a real import, and this test uses it as a real value.
    """
    assert get_origin(MoneyStr) is Annotated
    assert get_args(MoneyStr)[0] is Decimal
    assert Amount.model_fields["amount"].annotation is Decimal


def test_money_is_typed_as_a_string_in_the_schema(money_app: FastAPI) -> None:
    """`WithJsonSchema` is what makes the generated TypeScript say `string`."""
    schema: dict[str, Any] = money_app.openapi()
    amount = schema["components"]["schemas"]["Amount"]["properties"]["amount"]

    assert amount["type"] == "string"
    assert "number" not in json.dumps(amount)


async def test_a_json_number_is_rejected(money_client: AsyncClient) -> None:
    """Accepting it would launder the error: `0.1` is already inexact when it arrives."""
    response = await post_raw_json(money_client, b'{"amount": 0.1}')

    assert response.status_code == 422
    assert "must arrive as a JSON string" in response.text


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"amount": 1.0}', id="whole-float"),
        pytest.param(b'{"amount": true}', id="bool"),
        pytest.param(b'{"amount": 1e3}', id="exponent-notation"),
    ],
)
async def test_other_json_numbers_are_rejected_too(money_client: AsyncClient, body: bytes) -> None:
    """`1.0` is the dangerous one: a float that looks exactly like an amount."""
    response = await post_raw_json(money_client, body)

    assert response.status_code == 422
    assert "must arrive as a JSON string" in response.text


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        pytest.param('"0.1"', "0.1", id="string"),
        pytest.param('"12345678901234567890.12345678901234567890"', str(LOSSLESS), id="wide"),
        pytest.param("7", "7", id="integer"),
        pytest.param('"-1.50"', "-1.50", id="negative"),
    ],
)
async def test_an_accepted_value_survives_the_round_trip(
    money_client: AsyncClient, sent: str, expected: str
) -> None:
    """A string in, the same digits out -- including the trailing zero in `-1.50`."""
    response = await post_raw_json(money_client, f'{{"amount": {sent}}}'.encode())

    assert response.status_code == 200
    assert json.loads(response.text) == {"amount": expected}


async def test_a_malformed_string_is_an_ordinary_422(money_client: AsyncClient) -> None:
    """Handed to Pydantic's own `Decimal` validation rather than raised from the guard."""
    response = await post_raw_json(money_client, b'{"amount": "not a number"}')

    assert response.status_code == 422
    assert "must arrive as a JSON string" not in response.text


# --------------------------------------------------------------------------------------
# The shipped schema is untouched.
# --------------------------------------------------------------------------------------


def test_the_shipped_application_gained_no_money_operation(app: FastAPI) -> None:
    """No endpoint returns money yet, so the generated client must not change.

    `app` is the real application from the suite-wide conftest. If this ever fails, the
    OpenAPI drift job is about to fail too, and `frontend/src/api/generated/schema.ts`
    needs regenerating -- which is out of scope for this issue by design.
    """
    schema: dict[str, Any] = app.openapi()

    assert set(schema["paths"]) == {"/api/health"}
    assert "Amount" not in schema.get("components", {}).get("schemas", {})
