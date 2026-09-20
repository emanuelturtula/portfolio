"""Every failure leaves the API as a problem document, and says nothing it should not."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import pytest
from fastapi import FastAPI, Query
from httpx import ASGITransport, AsyncClient

from portfolio.api.errors import (
    PROBLEM_CONTENT_TYPE,
    UNEXPECTED_DETAIL,
    AppError,
    register_exception_handlers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# The kind of string an exception message really carries: a fragment of internal state
# that must never be echoed back to a browser.
LEAKY_MESSAGE = "connection to store 'ledger-7' failed while reading row 42"


class UnplannedError(Exception):
    """An exception nobody planned for."""


class TeapotError(AppError):
    """A deliberate, client-safe failure."""

    status = 418
    title = "I'm a teapot"
    problem_type = "https://example.invalid/problems/teapot"


@pytest.fixture
def failing_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom", operation_id="boom")
    async def boom() -> None:
        raise UnplannedError(LEAKY_MESSAGE)

    @app.get("/teapot", operation_id="teapot")
    async def teapot() -> None:
        raise TeapotError("no coffee here")

    @app.get("/items", operation_id="items")
    async def items(limit: Annotated[int, Query()]) -> dict[str, int]:
        return {"limit": limit}

    return app


@pytest.fixture
async def failing_client(failing_app: FastAPI) -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False makes the client behave like a real server: it returns
    # the 500 response instead of re-raising the exception inside the test.
    transport = ASGITransport(app=failing_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_unhandled_error_renders_as_problem_json(failing_client: AsyncClient) -> None:
    response = await failing_client.get("/boom")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)

    problem = response.json()
    assert problem["status"] == 500
    assert problem["title"] == "Internal Server Error"
    assert problem["detail"] == UNEXPECTED_DETAIL
    assert problem["instance"] == "/boom"


async def test_unhandled_error_never_leaks_the_exception_message(
    failing_client: AsyncClient,
) -> None:
    response = await failing_client.get("/boom")

    body = response.text
    assert LEAKY_MESSAGE not in body
    assert "ledger-7" not in body
    assert UnplannedError.__name__ not in body
    assert "Traceback" not in body


async def test_app_error_keeps_its_status_and_title(failing_client: AsyncClient) -> None:
    response = await failing_client.get("/teapot")

    assert response.status_code == 418
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)

    problem = response.json()
    assert problem["type"] == TeapotError.problem_type
    assert problem["title"] == "I'm a teapot"
    assert problem["detail"] == "no coffee here"


async def test_validation_error_reports_the_offending_field(failing_client: AsyncClient) -> None:
    response = await failing_client.get("/items", params={"limit": "not-a-number"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)

    problem = response.json()
    assert problem["status"] == 422
    assert problem["title"] == "Unprocessable Entity"
    assert [error["loc"] for error in problem["errors"]] == [["query", "limit"]]
    assert problem["errors"][0]["msg"]
