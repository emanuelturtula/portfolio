"""The portfolio endpoints: the dashboard's summary, and its change over 24 hours and 7 days.

Thin on purpose: the route calls the service and serializes. What counts as held and what a
holding is worth are the service's and the domain's rules.

Neither path is in `PUBLIC_API_PATHS`, so both require a session: the
deny-by-default middleware's doing, not this module's. The service reads what the sync and
the price refresh stored, and imports nothing under `portfolio.providers`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from portfolio.api.dependencies import (
    get_portfolio_change_service,
    get_portfolio_service,
    get_principal,
)
from portfolio.api.schemas.portfolio import PortfolioChangesResponse, PortfolioSummaryResponse
from portfolio.services.auth import Principal
from portfolio.services.portfolio import PortfolioService
from portfolio.services.portfolio_changes import PortfolioChangeService

# Declared here rather than imported from `api.dependencies`, for the reason the balance
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentPortfolioService = Annotated[PortfolioService, Depends(get_portfolio_service)]
CurrentChangeService = Annotated[PortfolioChangeService, Depends(get_portfolio_change_service)]

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get(
    "/summary",
    operation_id="readPortfolioSummary",
    summary="Total value in USDT, with every holding and its share",
    response_model=PortfolioSummaryResponse,
)
async def read_portfolio_summary(
    principal: CurrentPrincipal,
    service: CurrentPortfolioService,
) -> PortfolioSummaryResponse:
    """Return what the wallets hold and what it is worth.

    **Reads what is stored; asks no chain or price source anything.** A figure that could not
    include something -- an unread wallet, an unpriced asset -- names it in `missing` rather
    than counting it as zero. Every amount is a JSON string.
    """
    view = await service.summary(principal.user_id)
    return PortfolioSummaryResponse.of(view)


@router.get(
    "/changes",
    operation_id="readPortfolioChanges",
    summary="How much the value in USDT changed over the last 24 hours and 7 days",
    response_model=PortfolioChangesResponse,
)
async def read_portfolio_changes(
    principal: CurrentPrincipal,
    service: CurrentChangeService,
) -> PortfolioChangesResponse:
    """The value now, and its change since 24 hours and 7 days ago (spec 041).

    The value now is the summary's total. The value then is each active wallet's balance at
    that instant times the hourly close that priced it. **A change that cannot be worked out
    is `null` with the reason in `unavailable`, never `"0"`.** Every amount is a JSON string.
    """
    changes = await service.changes(principal.user_id)
    return PortfolioChangesResponse.of(changes)
