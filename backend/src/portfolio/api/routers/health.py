"""Liveness, and the state of the application's own sources.

`GET /api/health` is deliberately cheap: it reports what this process is, never whether a
dependency is up, so a slow dependency cannot make the container look dead. It is public,
because the container's health check calls it before anyone signs in, and it stays that cheap.

`GET /api/health/detail` is the other half (#22, spec 029): how the scheduled backups stand,
and -- with #23 -- the other sources beside them. It is **not** in `PUBLIC_API_PATHS`, so the
middleware requires a session for it like every other path; nothing here had to ask for that.
It reads the backup directory through the service, and the router itself touches no file.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from portfolio import __version__
from portfolio.api.dependencies import get_backup_service
from portfolio.api.schemas.health import BackupStatusResponse, HealthDetailResponse
from portfolio.config import Settings, get_settings
from portfolio.services.backup import BackupService

# Declared here rather than in the signature, for the reason the accounting router gives:
# FastAPI resolves these annotations at import time.
CurrentBackupService = Annotated[BackupService, Depends(get_backup_service)]

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
    summary="Report how the scheduled backups stand",
    response_model=HealthDetailResponse,
)
async def get_health_detail(service: CurrentBackupService) -> HealthDetailResponse:
    """Return the backup's state, the newest copy's instant, the count and the last attempt."""
    return HealthDetailResponse(backup=BackupStatusResponse.of(await service.status()))
