"""Liveness endpoint.

Deliberately cheap: it reports what this process is, never whether a dependency is up.
A readiness probe that touches the database belongs in its own endpoint so a slow
dependency cannot make the container look dead.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from portfolio import __version__
from portfolio.config import Settings, get_settings

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Payload of a successful health check."""

    status: Literal["ok"]
    version: str
    environment: Literal["dev", "prod"]


@router.get(
    "/health",
    operation_id="getHealth",
    summary="Report that the API process is running",
    response_model=HealthResponse,
)
async def get_health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Return the identity of the running process."""
    return HealthResponse(status="ok", version=__version__, environment=settings.environment)
