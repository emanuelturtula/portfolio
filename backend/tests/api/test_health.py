"""The health endpoint is the contract the container probe depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


async def test_health_payload_shape(client: AsyncClient) -> None:
    payload = (await client.get("/api/health")).json()

    assert set(payload) == {"status", "version", "environment"}
    assert payload["status"] == "ok"
    assert payload["environment"] in {"dev", "prod"}
    assert isinstance(payload["version"], str)
    assert payload["version"]
