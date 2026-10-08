"""The value-history endpoints: what the wallets were worth on each day (spec 037).

Thin on purpose: the routes parse the range, call the service and serialize. Neither path is
in `PUBLIC_API_PATHS`, so both require a session: the deny-by-default middleware's doing, not
this module's. The service reads stored snapshots and stored daily prices, and imports
nothing under `portfolio.providers`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from portfolio.api.dependencies import get_portfolio_history_service, get_principal
from portfolio.api.errors import NotFoundError
from portfolio.api.schemas.history import PortfolioHistoryResponse, WalletValueHistoryResponse
from portfolio.services.auth import Principal
from portfolio.services.portfolio_history import HistoryRange, PortfolioHistoryService
from portfolio.services.wallets import WalletNotFoundError

# Declared here rather than imported from `api.dependencies`, for the reason the balance
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentHistoryService = Annotated[PortfolioHistoryService, Depends(get_portfolio_history_service)]
Range = Annotated[
    HistoryRange,
    Query(alias="range", description="How far back: 30d, 90d, 1y, or all since the first reading."),
]

router = APIRouter(tags=["history"])


@router.get(
    "/portfolio/history",
    operation_id="readPortfolioHistory",
    summary="The wallets' value in USDT at the end of each day of a range",
    response_model=PortfolioHistoryResponse,
)
async def read_portfolio_history(
    principal: CurrentPrincipal,
    service: CurrentHistoryService,
    history_range: Range = HistoryRange.DAYS_90,
) -> PortfolioHistoryResponse:
    """One point per day, oldest first, ending today (UTC).

    A day's value is each active wallet's closing balance times that day's price. **A day
    nothing can value is `null`, never `"0"`**: no wallet read yet, or a holding with no
    price that day. Every amount is a JSON string.
    """
    history = await service.portfolio(principal.user_id, history_range)
    return PortfolioHistoryResponse.of(history)


@router.get(
    "/wallets/{wallet_id}/value-history",
    operation_id="readWalletValueHistory",
    summary="One wallet's quantity and value in USDT at the end of each day of a range",
    response_model=WalletValueHistoryResponse,
)
async def read_wallet_value_history(
    wallet_id: int,
    principal: CurrentPrincipal,
    service: CurrentHistoryService,
    history_range: Range = HistoryRange.DAYS_90,
) -> WalletValueHistoryResponse:
    """One point per day for one wallet, archived ones included.

    A wallet that is not the caller's is a `404`, the answer a wallet that does not exist
    gets, because any other status would confirm the id.
    """
    try:
        history = await service.wallet(principal.user_id, wallet_id, history_range)
    except WalletNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return WalletValueHistoryResponse.of(history)
