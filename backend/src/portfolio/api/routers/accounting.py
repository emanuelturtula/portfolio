"""The accounting endpoint: the owner's positions, costs and returns, valued in USD.

Thin on purpose: it calls the service and serializes. Nothing is computed here -- not the
valuation, not the totals, not which positions the totals leave out -- because a rule that
lives in a router is a rule the CLI does not have. Nothing is recomputed here either: the
snapshot is written by the triggers in `main.py`, and a request is served from what is stored.

`/api/accounting/positions` is not in `PUBLIC_API_PATHS`, so it requires a session. That is
the deny-by-default middleware's doing, not this module's.

## `last_recompute` comes through a dependency

It lives on `app.state.accounting_status`, which the trigger writes. The route reads it through
`get_accounting_status` rather than touching `app.state` itself, the way the coordinators are
reached, so that where it is kept is decided in one place.

## Nothing here can reach a vendor

The service imports the price cache's read side and the fill table's repository, and nothing
under `portfolio.providers`: the `prices-are-never-fetched-in-a-request` and
`api-never-reaches-an-exchange-provider` import contracts hold for this route as they are.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from portfolio.api.dependencies import (
    get_accounting_service,
    get_accounting_status,
    get_principal,
)
from portfolio.api.schemas.accounting import PositionsResponse
from portfolio.services.accounting import AccountingService, AccountingStatus
from portfolio.services.auth import Principal

# Declared here rather than imported from `api.dependencies`, for the reason the balance
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentAccountingService = Annotated[AccountingService, Depends(get_accounting_service)]
LastRecompute = Annotated[AccountingStatus | None, Depends(get_accounting_status)]

router = APIRouter(prefix="/accounting", tags=["accounting"])


@router.get(
    "/positions",
    operation_id="readAccountingPositions",
    summary="Every asset's position, cost and return, valued in USD, with portfolio totals",
    response_model=PositionsResponse,
)
async def read_positions(
    principal: CurrentPrincipal,
    service: CurrentAccountingService,
    last_recompute: LastRecompute,
) -> PositionsResponse:
    """Return the stored cost-basis snapshot, each position valued at its cached price.

    **Per asset**: quantity, average cost, total invested, the price with its age, market value,
    unrealized P&L and percentage return, realized P&L beside them, and the flags that qualify
    them. **For the portfolio**: the totals over the positions that can be compared, and the
    ones left out with their reason. Every amount is a JSON string.

    With no snapshot yet the answer is still `200`, with `computed_at: null` and empty lists:
    the first recompute runs at startup and after every exchange sync that stores a fill.
    """
    view = await service.positions(principal.user_id)
    return PositionsResponse.of(view, last_recompute=last_recompute)
