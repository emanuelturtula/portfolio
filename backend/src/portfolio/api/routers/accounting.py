"""The accounting endpoints: the owner's positions valued in USD, and the holdings check.

Thin on purpose: each route calls a service and serializes. Nothing is computed here -- not
the valuation, not the totals, not which positions the totals leave out, not the tolerance a
quantity is compared within -- because a rule that lives in a router is a rule the CLI does not
have. Nothing is recomputed here either: the snapshot is written by the triggers in `main.py`,
and a request is served from what is stored.

Neither `/api/accounting/positions` nor `/api/accounting/reconciliation` is in
`PUBLIC_API_PATHS`, so both require a session. That is the deny-by-default middleware's doing,
not this module's.

## `last_recompute` comes through a dependency

It lives on `app.state.accounting_status`, which the trigger writes. The route reads it through
`get_accounting_status` rather than touching `app.state` itself, the way the coordinators are
reached, so that where it is kept is decided in one place.

## Nothing here can reach a vendor

The services import the price cache's read side and the repositories, and nothing under
`portfolio.providers`: the `prices-are-never-fetched-in-a-request` and
`api-never-reaches-an-exchange-provider` import contracts hold for these routes as they are.
The reconciliation reads the venue balances the exchange sync stored; it never asks a venue.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from portfolio.api.dependencies import (
    get_accounting_service,
    get_accounting_status,
    get_principal,
    get_reconciliation_service,
)
from portfolio.api.schemas.accounting import PositionsResponse, ReconciliationResponse
from portfolio.services.accounting import AccountingService, AccountingStatus
from portfolio.services.auth import Principal
from portfolio.services.reconciliation import ReconciliationService

# Declared here rather than imported from `api.dependencies`, for the reason the balance
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentAccountingService = Annotated[AccountingService, Depends(get_accounting_service)]
LastRecompute = Annotated[AccountingStatus | None, Depends(get_accounting_status)]
CurrentReconciliationService = Annotated[ReconciliationService, Depends(get_reconciliation_service)]

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


@router.get(
    "/reconciliation",
    operation_id="readReconciliation",
    summary="Each asset's replayed quantity beside the balances held, and the sources read",
    response_model=ReconciliationResponse,
)
async def read_reconciliation(
    principal: CurrentPrincipal,
    service: CurrentReconciliationService,
    last_recompute: LastRecompute,
) -> ReconciliationResponse:
    """Compare what the history says is held with the balances read, per asset.

    **Per asset**: the quantity the cost-basis snapshot holds, the quantity read from the
    wallets and from the exchange accounts, their difference, and whether it is a `match`, a
    `history_short` -- more is held than the history accounts for, which usually means buys
    are missing from it -- or a `history_over`. **Per source**: when each exchange account's
    balances were last read, why the last attempt failed, and why the account was left out if
    it was; and how many wallets were compared, how many had a reading too old, and how many
    were never read. Only a reading at most `max_reading_age_hours` old is compared. Every
    quantity is a JSON string.

    The balances are the ones the syncs stored: nothing is read from a chain or a venue here.
    With no snapshot yet the answer is still `200`, with `computed_at: null` and no assets.
    `last_recompute` says whether the snapshot compared is current, as on the positions.
    """
    view = await service.reconciliation(principal.user_id)
    return ReconciliationResponse.of(view, last_recompute=last_recompute)
