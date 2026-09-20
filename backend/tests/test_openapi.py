"""The generated schema is the contract the frontend client is built from."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def test_openapi_schema_generates(app: FastAPI) -> None:
    schema = app.openapi()

    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "Portfolio API"
    assert "/api/health" in schema["paths"]


def test_every_api_operation_has_an_operation_id(app: FastAPI) -> None:
    """Without an explicit operationId the generated client method names churn."""
    schema = app.openapi()
    missing = [
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        if path.startswith("/api")
        for method, operation in operations.items()
        if method in HTTP_METHODS and not operation.get("operationId")
    ]

    assert missing == []


def test_operation_ids_are_unique(app: FastAPI) -> None:
    schema = app.openapi()
    operation_ids = [
        operation["operationId"]
        for operations in schema["paths"].values()
        for method, operation in operations.items()
        if method in HTTP_METHODS
    ]

    assert len(operation_ids) == len(set(operation_ids))


async def test_the_schema_is_not_served_to_an_unauthenticated_caller(
    client: AsyncClient,
) -> None:
    """`/api/openapi.json` is under `/api` and is not on the public allowlist (#3).

    It used to answer 200 here. That changed deliberately: the document describes every
    endpoint this application has, and the owner's browser sends the session cookie, so
    Swagger UI still works for them. The drift job dumps the schema in process rather than
    over HTTP, so nothing in this repository needs it unauthenticated.
    """
    response = await client.get("/api/openapi.json")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


def test_the_schema_is_generated_under_the_api_prefix(app: FastAPI) -> None:
    """The document itself is still built at the prefixed URL, cookie or no cookie."""
    schema: dict[str, Any] = app.openapi()

    assert app.openapi_url == "/api/openapi.json"
    assert schema["paths"]["/api/health"]["get"]["operationId"] == "getHealth"
