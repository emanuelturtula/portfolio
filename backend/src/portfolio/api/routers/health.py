"""Liveness, and the state of the application's own sources.

`GET /api/health` is deliberately cheap: it reports what this process is, never whether a
dependency is up, so a slow dependency cannot make the container look dead. It is public,
because the container's health check calls it before anyone signs in, and it stays that cheap.

`GET /api/health/detail` is the other half: how the scheduled backups stand (#22, spec 029),
and the other sources beside them (#23, spec 030) -- the five timers, the balance sync per
chain and the prices, each as its last recorded attempt left it. **No vendor is called**: the
page refetches every minute, and a check that asked a chain index or a price source would
spend the rate limits the syncs are budgeted against. It is **not** in `PUBLIC_API_PATHS`, so
the middleware requires a session for it like every other path; nothing here had to ask for
that. The router parses, calls `HealthService.detail` and serializes, and touches no file and
no table.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from portfolio import __version__
from portfolio.api.dependencies import get_health_service, get_principal
from portfolio.api.schemas.health import HealthDetailResponse
from portfolio.config import Settings, get_settings
from portfolio.services.auth import Principal
from portfolio.services.health import HealthService

# Declared here rather than in the signature, for the reason the balance router gives:
# FastAPI resolves these annotations at import time.
CurrentHealthService = Annotated[HealthService, Depends(get_health_service)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]

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


@router.get(
    "/health/detail",
    operation_id="getHealthDetail",
    summary="Report how the backups, timers, balance sync and prices stand",
    response_model=HealthDetailResponse,
)
async def get_health_detail(
    service: CurrentHealthService, principal: CurrentPrincipal
) -> HealthDetailResponse:
    """Return each source's state as its last recorded attempt left it. Calls no vendor."""
    return HealthDetailResponse.of(await service.detail(principal.user_id))
