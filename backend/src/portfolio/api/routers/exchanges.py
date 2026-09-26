"""The three exchange endpoints: list the venues, trigger a sync, read the run log.

Thin on purpose: parse, call a service or the coordinator, serialize. None of the policy is
here -- not what a run is, not when an `auth_failed` account is retried, not what
`history_truncated` means.

None of these paths is in `PUBLIC_API_PATHS`, so all three require a session; the
deny-by-default middleware does that, not this module.

## Nothing here can reach an exchange provider

This module imports the read-side service and the coordinator, and neither imports
`portfolio.providers.exchanges`: `backend/.importlinter`'s
`api-never-reaches-an-exchange-provider` contract forbids every module under `portfolio.api`
from reaching it, directly or indirectly. `POST /api/exchanges/sync` causes venue traffic
through the coordinator the lifespan built, whose runner closes over the providers in
`main.py` -- the one place that holds them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from portfolio.api.dependencies import (
    get_exchange_service,
    get_exchange_sync_coordinator,
    get_principal,
)
from portfolio.api.schemas.exchanges import (
    ExchangeListResponse,
    ExchangeResponse,
    ExchangeSyncRunListResponse,
    ExchangeSyncRunResponse,
    ExchangeSyncTriggeredResponse,
)
from portfolio.services.auth import Principal
from portfolio.services.exchanges import (
    DEFAULT_EXCHANGE_RUNS_LIMIT,
    MAX_EXCHANGE_RUNS_LIMIT,
    ExchangeService,
    ExchangeSyncRunSummary,
)
from portfolio.services.sync_coordinator import SyncCoordinator, SyncTrigger

# Declared here rather than imported from `api.dependencies`, for the reason the balance
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentExchangeService = Annotated[ExchangeService, Depends(get_exchange_service)]
CurrentExchangeSyncCoordinator = Annotated[
    SyncCoordinator[ExchangeSyncRunSummary],
    Depends(get_exchange_sync_coordinator),
]
RunsLimit = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_EXCHANGE_RUNS_LIMIT,
        description="How many runs to return, newest first.",
    ),
]

router = APIRouter(tags=["exchanges"])


@router.get(
    "/exchanges",
    operation_id="listExchanges",
    summary="Every configured exchange and every exchange with an account, with its sync state",
    response_model=ExchangeListResponse,
)
async def list_exchanges(
    principal: CurrentPrincipal,
    service: CurrentExchangeService,
) -> ExchangeListResponse:
    """Return each venue's sync state. **Never a credential**: `configured` is a boolean.

    Reads the database; calls no venue.
    """
    views = await service.list_exchanges(principal)
    return ExchangeListResponse(exchanges=[ExchangeResponse.of(view) for view in views])


@router.post(
    "/exchanges/sync",
    operation_id="syncExchanges",
    summary="Import fills from every configured exchange now and return the run summary",
    response_model=ExchangeSyncTriggeredResponse,
)
async def sync_exchanges(
    principal: CurrentPrincipal,
    coordinator: CurrentExchangeSyncCoordinator,
) -> ExchangeSyncTriggeredResponse:
    """Run a manual sync, or join the one in flight, and return what it did.

    `200` whatever the run's own status, for the reason `POST /api/balances/sync` gives: a run
    in which one venue failed is a `partial` run this endpoint performed and reported. **A
    manual sync is the one that retries an `auth_failed` account** -- after the owner has
    fixed the key and restarted the container.
    """
    del principal  # Authorisation only: the sync is process-wide.
    outcome = await coordinator.sync(SyncTrigger.MANUAL)
    return ExchangeSyncTriggeredResponse.of_outcome(outcome)


@router.get(
    "/exchanges/runs",
    operation_id="listExchangeSyncRuns",
    summary="The most recent exchange sync runs, newest first",
    response_model=ExchangeSyncRunListResponse,
)
async def list_exchange_sync_runs(
    principal: CurrentPrincipal,
    service: CurrentExchangeService,
    limit: RunsLimit = DEFAULT_EXCHANGE_RUNS_LIMIT,
) -> ExchangeSyncRunListResponse:
    """Return the exchange run log: what ran, when, and what each account did."""
    del principal  # Authorisation only: the run log is process-wide.
    runs = await service.list_runs(limit=limit)
    return ExchangeSyncRunListResponse(runs=[ExchangeSyncRunResponse.of(run) for run in runs])
