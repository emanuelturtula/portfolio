"""The portfolio endpoint: the dashboard's summary (#154).

Thin on purpose: the route calls the service and serializes. What counts as held, what a
holding is worth and what was invested are the service's and the domain's rules.

`/api/portfolio/summary` is not in `PUBLIC_API_PATHS`, so it requires a session: the
deny-by-default middleware's doing, not this module's. The service reads what the syncs and
the price refresh stored, and imports nothing under `portfolio.providers`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from portfolio.api.dependencies import get_portfolio_service, get_principal
from portfolio.api.schemas.portfolio import PortfolioSummaryResponse
from portfolio.services.auth import Principal
from portfolio.services.portfolio import PortfolioService

# Declared here rather than imported from `api.dependencies`, for the reason the balance
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentPortfolioService = Annotated[PortfolioService, Depends(get_portfolio_service)]

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get(
    "/summary",
    operation_id="readPortfolioSummary",
    summary="Total value, net invested and P/L in USDT, with every holding and its share",
    response_model=PortfolioSummaryResponse,
)
async def read_portfolio_summary(
    principal: CurrentPrincipal,
    service: CurrentPortfolioService,
) -> PortfolioSummaryResponse:
    """Return what is held, what it is worth, and what went into it.

    **Reads what is stored; asks no chain, venue or price source anything.** A figure that
    could not include something -- an unread wallet, an unpriced asset, a fill not quoted in
    cash -- names it in `missing` rather than counting it as zero. Every amount is a JSON
    string.
    """
    view = await service.summary(principal.user_id)
    return PortfolioSummaryResponse.of(view)
