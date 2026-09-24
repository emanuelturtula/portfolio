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
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

from portfolio.api.schemas.money import MAX_WIRE_EXPONENT, MoneyStr
from portfolio.db.types import NumericText
from portfolio.domain.money import MONEY_PRECISION

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BASE_URL: Final = "http://moneytest"

# Only needed so the column type can be asked what it would have stored; the tests that
# use it are about the wire, not about the database.
DIALECT: Final = sqlite_dialect()

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
# The exponent is bounded, because rendering is linear in it and the client picks it.
# --------------------------------------------------------------------------------------


async def test_an_enormous_exponent_is_refused_without_rendering_it(
    money_client: AsyncClient,
) -> None:
    """A 15-byte field used to cost a gigabyte of output. On a Pi that is an OOM kill.

    `format(value, "f")` writes every position between the digits and the point, so its
    cost is linear in the exponent while the request body stays tiny: `"1E+1000000"` used
    to render a 1,000,014-character string. The amplifier is the renderer, not the parser,
    and the client chooses the exponent and does not pay for it.
    """
    response = await post_raw_json(money_client, b'{"amount": "1E+1000000"}')

    assert response.status_code == 422
    assert "exponent within" in response.text
    # The refusal did not render the value on its way to being refused.
    assert len(response.text) < 2_000


@pytest.mark.parametrize(
    ("sent", "accepted"),
    [
        pytest.param("1E+38", True, id="max-positive-exponent"),
        pytest.param("1E+39", False, id="one-past-positive"),
        pytest.param("1E-38", True, id="max-negative-exponent"),
        pytest.param("1E-39", False, id="one-past-negative"),
        pytest.param("1E+1000000000", False, id="absurd"),
    ],
)
async def test_the_exponent_bound_is_walked_from_both_sides(
    money_client: AsyncClient, sent: str, accepted: bool
) -> None:
    """The bound is `MONEY_PRECISION` in either direction, and it is exactly that."""
    response = await post_raw_json(money_client, f'{{"amount": "{sent}"}}'.encode())

    assert (response.status_code == 200) is accepted
    assert MAX_WIRE_EXPONENT == MONEY_PRECISION


@pytest.mark.parametrize(
    ("sent", "id_"),
    [
        pytest.param("9" * 100, "wide-integer-part", id="adjusted-clause"),
        pytest.param("0." + "9" * 100, "wide-fraction", id="exponent-clause"),
    ],
)
async def test_each_half_of_the_magnitude_guard_is_load_bearing(
    money_client: AsyncClient, sent: str, id_: str
) -> None:
    """Two clauses, and until these inputs existed either could be deleted silently.

    The guard is `abs(exponent) > MAX or abs(adjusted()) > MAX`, and every other input in
    this file trips both clauses at once, so deleting either one left the whole suite
    green. These two separate them:

        "9" * 100        exponent=0     adjusted=99    exponent-clause False
        "0." + "9" * 100 exponent=-100  adjusted=-1    adjusted-clause False

    A hundred nines is not a DoS on its own -- with one clause the render stays linear in
    the request size -- so what these protect is the bound quietly widening, not the
    out-of-memory case.
    """
    response = await post_raw_json(money_client, f'{{"amount": "{sent}"}}'.encode())

    assert response.status_code == 422, id_


@pytest.mark.parametrize("scale", [0, 2, 8, 18, 38])
async def test_anything_a_money_column_accepts_also_serializes(
    money_client: AsyncClient, scale: int
) -> None:
    """The reason the bound is derived from `MONEY_PRECISION` rather than picked.

    A separate number would eventually differ, and the difference would show up as a value
    that persists fine and then fails to serialize -- a row in the database that no
    endpoint can return. This walks the widest value each scale admits, stores it through
    `NumericText`, and sends the same value over the wire.
    """
    integer_digits = MONEY_PRECISION - scale
    widest = Decimal(f"{'9' * integer_digits}.{'9' * scale}" if scale else "9" * integer_digits)
    stored = NumericText(scale).process_bind_param(widest, DIALECT)

    response = await post_raw_json(money_client, f'{{"amount": "{widest}"}}'.encode())

    assert stored is not None
    assert response.status_code == 200
    assert json.loads(response.text) == {"amount": stored}


# --------------------------------------------------------------------------------------
# Negative zero, so the wire and the column agree.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        pytest.param("-0.00", "0.00", id="two-places"),
        pytest.param("-0", "0", id="bare"),
        pytest.param("-0.000", "0.000", id="three-places"),
        pytest.param("0.00", "0.00", id="already-positive"),
    ],
)
async def test_negative_zero_is_normalised_on_the_wire(
    money_client: AsyncClient, sent: str, expected: str
) -> None:
    """A computed loss of four tenths of a cent rendered as `-0.00` in the UI.

    The scale is preserved and only the sign is dropped, which is what keeps this
    agreeing with `NumericText`: the column stores `0.00` for the same amount, and before
    this the two layers disagreed about how to spell it.
    """
    response = await post_raw_json(money_client, f'{{"amount": "{sent}"}}'.encode())

    assert response.status_code == 200
    assert json.loads(response.text) == {"amount": expected}
    assert "-0" not in response.text


def test_the_wire_and_the_column_spell_zero_the_same_way() -> None:
    """Asserted against `NumericText` directly, so the two cannot drift apart."""
    on_the_wire = Amount(amount=Decimal("-0.00")).model_dump()["amount"]
    in_the_column = NumericText(2).process_bind_param(Decimal("-0.00"), DIALECT)

    assert on_the_wire == in_the_column == "0.00"


def test_a_negative_amount_that_is_not_zero_keeps_its_sign() -> None:
    """The normalisation is for zero only; `-0.01` is a real loss and must show as one."""
    assert Amount(amount=Decimal("-0.01")).model_dump()["amount"] == "-0.01"


# --------------------------------------------------------------------------------------
# The shipped schema is untouched.
# --------------------------------------------------------------------------------------


def test_the_shipped_application_serves_exactly_these_operations(app: FastAPI) -> None:
    """The path set, pinned, so that a new operation is a deliberate line in a diff.

    When this changes, the OpenAPI drift job is about to fail too and
    `frontend/src/api/generated/schema.ts` needs regenerating -- which is the whole reason
    the set is written out rather than counted.

    #10 is the change the older wording of this test was waiting for: "reading a balance is
    #6 to #8, and that is the change that will have to introduce `Amount` and argue for it
    here". It did not introduce `Amount` -- the balance schemas declare their own string
    fields -- so the argument moved to
    `test_every_monetary_field_in_the_schema_is_declared_a_string`, which asserts the
    property that mattered instead of the absence of one class.
    """
    schema: dict[str, Any] = app.openapi()

    assert set(schema["paths"]) == {
        "/api/health",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/session",
        "/api/auth/password",
        # The wallet registry (#5): which addresses balances are read from.
        "/api/wallets",
        "/api/wallets/{wallet_id}",
        # Balance sync, the valued read and the history (#10). The first operations in this
        # application that carry money and on-chain quantities across the wire.
        "/api/balances/sync",
        "/api/balances/current",
        "/api/balances/runs",
        "/api/wallets/{wallet_id}/balances",
    }


#: Property names that carry a monetary amount or an on-chain base-unit count. Both must be
#: JSON strings and for two different reasons -- rule 2 for the first group, and
#: `Number.MAX_SAFE_INTEGER` for the second -- which is why they are asserted together here
#: and argued apart in `tests/api/test_balances.py`.
MONEY_PROPERTIES: Final = frozenset(
    {"total", "value", "quantity", "amount", "confirmed", "pending"}
)


def declared_types(schema: dict[str, Any]) -> set[str]:
    """The JSON types a property may take, flattening the `anyOf` a nullable field becomes."""
    if "anyOf" in schema:
        return {str(option.get("type")) for option in schema["anyOf"]}
    return {str(schema.get("type"))}


def test_every_monetary_field_in_the_schema_is_declared_a_string(app: FastAPI) -> None:
    """Rule 2 at the generated client, over every schema rather than a named few.

    The frontend's `no-restricted-globals` rule bans `parseFloat`, `parseInt` and `Number()`
    on money, and that ban only means something if the generated TypeScript declares these
    fields as `string`. A field typed `number` there compiles perfectly and is already
    inexact by the time any code runs.

    Walked rather than listed, because a test naming today's fields would keep passing when
    somebody adds tomorrow's -- and tomorrow's is the one that arrives as a number. `null`
    is allowed alongside `string`: an unvalued holding and a chain that cannot answer the
    mempool question are absences, not zeros.
    """
    schemas: dict[str, Any] = app.openapi().get("components", {}).get("schemas", {})
    offences = [
        f"{name}.{field}: {sorted(declared_types(definition))}"
        for name, schema in schemas.items()
        for field, definition in schema.get("properties", {}).items()
        if field in MONEY_PROPERTIES and not declared_types(definition) <= {"string", "null"}
    ]
    seen = {
        field
        for schema in schemas.values()
        for field in schema.get("properties", {})
        if field in MONEY_PROPERTIES
    }

    assert offences == []
    # The walk has to have found something, or an application with no money in it at all
    # would satisfy the assertion above.
    assert seen >= {"total", "value", "quantity", "amount", "confirmed", "pending"}


def test_the_money_field_walk_can_actually_fail() -> None:
    """The control: a property typed `number` under a money name has to be reported.

    Without it, a typo in `MONEY_PROPERTIES` or a `declared_types` that returned the empty
    set would make the test above pass over a document full of floats.
    """
    assert declared_types({"type": "string"}) == {"string"}
    assert declared_types({"anyOf": [{"type": "string"}, {"type": "null"}]}) == {"string", "null"}
    assert declared_types({"type": "number"}) == {"number"}
    assert not declared_types({"type": "number"}) <= {"string", "null"}
