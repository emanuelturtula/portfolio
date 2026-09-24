"""The four balance endpoints: trigger a sync, read the total, read a history, read the log.

Thin on purpose: each one parses its query string, calls a service, and turns the service's
exception into a status code. None of the policy is here -- not what a run is, not what
joining means, not what an unread wallet reports -- because a rule that lives in a router is
a rule the CLI does not have.

None of these paths is in `PUBLIC_API_PATHS`, so all four require a session. That is not a
decision made here; it is what the deny-by-default middleware does with any path it has not
been told to let through, which is why adding an endpoint protects it.

## One router, four paths, no prefix

`GET /api/wallets/{wallet_id}/balances` belongs to this module by subject and to the wallet
collection by URL, so the router is declared without a prefix and each route carries its
full path. The alternative -- a second `APIRouter` in this file, or the history endpoint
moved into `routers/wallets.py` -- would split three balance reads across two modules to
satisfy a naming convention.

## `POST /api/balances/sync` reaches a chain provider from a request path, deliberately

That is the asymmetry with #9's price contract, and it is the point of the endpoint rather
than an exception to a rule: the owner asked for this read and is waiting for it, where
nobody asks for a price refresh and every dashboard render would trigger one. The layering
contract already allows the indirect chain, so no contract changes -- and nothing in this
module imports a provider, a repository or `httpx` directly, which is what `thin-routers`
actually checks.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from portfolio.api.dependencies import get_balance_service, get_principal, get_sync_coordinator
from portfolio.api.errors import NotFoundError
from portfolio.api.schemas.balances import (
    DEFAULT_QUOTE_CURRENCY,
    AwareDatetime,
    CurrentBalancesResponse,
    SyncRunListResponse,
    SyncRunResponse,
    SyncTriggeredResponse,
    WalletHistoryResponse,
)
from portfolio.services.auth import Principal
from portfolio.services.balances import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_RUNS_LIMIT,
    MAX_HISTORY_LIMIT,
    MAX_RUNS_LIMIT,
    BalanceService,
)
from portfolio.services.sync_coordinator import SyncCoordinator, SyncTrigger
from portfolio.services.wallets import WalletNotFoundError

# Declared here rather than imported from `api.dependencies`, for the reason the auth and
# wallet routers document: FastAPI resolves these annotations at import time to build the
# dependency graph, so the names inside them are runtime values wearing a type's clothes.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentBalanceService = Annotated[BalanceService, Depends(get_balance_service)]
CurrentSyncCoordinator = Annotated[SyncCoordinator, Depends(get_sync_coordinator)]

QuoteCurrency = Annotated[
    str,
    Query(
        min_length=3,
        max_length=3,
        description="The fiat currency to value holdings in, for example EUR.",
    ),
]
Since = Annotated[
    AwareDatetime | None,
    Query(
        description=(
            "Only readings at or after this instant, and page forward from it. "
            "Omitted, the latest `limit` readings are returned instead. "
            "Must carry a timezone offset; a naive timestamp is refused rather than "
            "assumed to be UTC."
        )
    ),
]
HistoryLimit = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_HISTORY_LIMIT,
        description=(
            "How many readings to return: the latest that many, or that many counting "
            "forward from `since`."
        ),
    ),
]
RunsLimit = Annotated[
    int,
    Query(ge=1, le=MAX_RUNS_LIMIT, description="How many runs to return, newest first."),
]

router = APIRouter(tags=["balances"])


@router.post(
    "/balances/sync",
    operation_id="syncBalances",
    summary="Read every wallet's balance now and return the run summary",
    response_model=SyncTriggeredResponse,
)
async def sync_balances(
    principal: CurrentPrincipal,
    coordinator: CurrentSyncCoordinator,
) -> SyncTriggeredResponse:
    """Run a sync, or attach to the one already in flight, and return what it did.

    **A second caller joins rather than being refused.** Clicking refresh twice, or clicking
    it while the scheduler's tick is running, returns the in-flight run's summary with
    `joined: true` -- not a 409 that makes the client poll for a result it could have been
    handed, and not a second round of requests at a public index.

    The response is `200` whatever the run's own status was. A run in which Kaspa failed and
    Bitcoin succeeded is a `partial` run that this endpoint successfully performed and
    successfully reported; turning a vendor's outage into a 5xx would lose the Bitcoin
    balances in the body along with it.
    """
    del principal  # Authorisation only: the sync is process-wide, not per account.
    outcome = await coordinator.sync(SyncTrigger.MANUAL)
    return SyncTriggeredResponse.of_outcome(outcome)


@router.get(
    "/balances/current",
    operation_id="readCurrentBalances",
    summary="The latest reading of every active wallet, valued",
    response_model=CurrentBalancesResponse,
)
async def read_current_balances(
    principal: CurrentPrincipal,
    service: CurrentBalanceService,
    quote_currency: QuoteCurrency = DEFAULT_QUOTE_CURRENCY,
) -> CurrentBalancesResponse:
    """Return every active wallet's latest balance and what it is worth.

    **Reads the snapshot table; asks no chain anything.** A wallet the sync has never
    covered comes back with nulls rather than zeros, and a holding with no price is named in
    `unpriced` rather than valued at nothing.
    """
    view = await service.current_balances(principal, quote_currency=quote_currency.upper())
    return CurrentBalancesResponse.of(view)


@router.get(
    "/wallets/{wallet_id}/balances",
    operation_id="readWalletBalanceHistory",
    summary="One wallet's balance history, oldest first",
    response_model=WalletHistoryResponse,
)
async def read_wallet_balance_history(
    wallet_id: int,
    principal: CurrentPrincipal,
    service: CurrentBalanceService,
    since: Since = None,
    limit: HistoryLimit = DEFAULT_HISTORY_LIMIT,
) -> WalletHistoryResponse:
    """Return one wallet's readings, oldest first, for charting.

    **The latest window by default, a forward cursor from `since`.** Both come back
    oldest-first, and the asymmetry is the kind a reader assumes is a bug, so it is stated
    here as well as at the repository: a chart wants the recent end, and a client paging
    through a year wants the rows after the last one it saw. Asking for the oldest `limit`
    readings of a wallet watched since January is not a request anything makes.

    An archived wallet still answers: its history is the reason archiving is a timestamp
    rather than a delete. A wallet that is not the caller's is a `404`, the same answer a
    wallet that does not exist gets, because any other status would confirm the id.
    """
    try:
        history = await service.wallet_history(principal, wallet_id, since=since, limit=limit)
    except WalletNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return WalletHistoryResponse.of(history)


@router.get(
    "/balances/runs",
    operation_id="listSyncRuns",
    summary="The most recent balance sync runs, newest first",
    response_model=SyncRunListResponse,
)
async def list_sync_runs(
    principal: CurrentPrincipal,
    service: CurrentBalanceService,
    limit: RunsLimit = DEFAULT_RUNS_LIMIT,
) -> SyncRunListResponse:
    """Return the run log: what ran, when, how long it took and what each chain did.

    This is what makes "every run writes a row" checkable without opening the database, and
    it is where an `interrupted` run -- one whose process died mid-sync -- becomes visible.
    """
    del principal  # Authorisation only: the run log is process-wide, not per account.
    runs = await service.list_runs(limit=limit)
    return SyncRunListResponse(runs=[SyncRunResponse.of(run) for run in runs])
