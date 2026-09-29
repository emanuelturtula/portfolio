"""The four exchange endpoints: list the venues, trigger a sync, read the run log, read fills.

Thin on purpose: parse, call a service or the coordinator, serialize. None of the policy is
here -- not what a run is, not when an `auth_failed` account is retried, not what
`history_truncated` means, not which fills a range holds or what they add up to.

None of these paths is in `PUBLIC_API_PATHS`, so all four require a session; the
deny-by-default middleware does that, not this module.

**A refused date range becomes a `RequestValidationError`**, as a refused adjustment does, so
the 422 is the problem document a client already handles: one `{loc: ["query", "from" |
"to"], msg: <rule>, type: "value_error"}`. The rule is the service's sentence and quotes no
value.

## Nothing here can reach an exchange provider

This module imports the read-side service and the coordinator, and neither imports
`portfolio.providers.exchanges`: `backend/.importlinter`'s
`api-never-reaches-an-exchange-provider` contract forbids every module under `portfolio.api`
from reaching it, directly or indirectly. `POST /api/exchanges/sync` causes venue traffic
through the coordinator the lifespan built, whose runner closes over the providers in
`main.py` -- the one place that holds them.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError

from portfolio.api.dependencies import (
    get_exchange_service,
    get_exchange_sync_coordinator,
    get_principal,
)
from portfolio.api.schemas.exchanges import (
    ExchangeFillListResponse,
    ExchangeListResponse,
    ExchangeResponse,
    ExchangeSyncRunListResponse,
    ExchangeSyncRunResponse,
    ExchangeSyncTriggeredResponse,
    InstantQuery,
)
from portfolio.services.auth import Principal
from portfolio.services.exchanges import (
    DEFAULT_EXCHANGE_RUNS_LIMIT,
    DEFAULT_FILLS_LIMIT,
    MAX_EXCHANGE_RUNS_LIMIT,
    MAX_FILLS_LIMIT,
    ExchangeKey,
    ExchangeService,
    ExchangeSyncRunSummary,
    InvalidFillRangeError,
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

MAX_FILLS_OFFSET: Final = 2**63 - 1
"""The largest `offset` accepted: SQLite's `INTEGER` ceiling, spec 023's bound for integers
that reach it. This one never reaches SQLite -- the page is sliced in Python -- but the bound
keeps an absurd offset a 422 rather than anything else."""

FillExchanges = Annotated[
    list[ExchangeKey] | None,
    Query(
        alias="exchange",
        description="A venue to include. Repeat it for several; omit it for every venue. A "
        "venue repeated counts once.",
    ),
]
FillsFrom = Annotated[
    InstantQuery | None,
    Query(
        alias="from",
        description="Inclusive start: an ISO 8601 datetime with a timezone offset. A naive "
        "datetime is refused rather than assumed to be UTC.",
    ),
]
FillsTo = Annotated[
    InstantQuery | None,
    Query(
        description="Exclusive end: an ISO 8601 datetime with a timezone offset, later than "
        "`from`. A fill exactly on it belongs to the next range.",
    ),
]
FillsLimit = Annotated[
    int,
    Query(ge=1, le=MAX_FILLS_LIMIT, description="How many fills to return, newest first."),
]
FillsOffset = Annotated[
    int,
    Query(
        ge=0,
        le=MAX_FILLS_OFFSET,
        description="How many matching fills to skip. Past the end is an empty page with the "
        "same totals.",
    ),
]

router = APIRouter(tags=["exchanges"])


def _range_refused(exc: InvalidFillRangeError) -> RequestValidationError:
    """The service's refusal as the field-level 422 the API already speaks. No value in it."""
    return RequestValidationError(
        [{"loc": ("query", exc.field), "msg": exc.rule, "type": "value_error"}]
    )


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
    fixed the key and recreated the container (`up --force-recreate`: a restart does not
    re-read `secrets.env`).
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


@router.get(
    "/exchanges/fills",
    operation_id="listExchangeFills",
    summary="The owner's imported fills, filtered by exchange and date, with their totals",
    response_model=ExchangeFillListResponse,
)
async def list_exchange_fills(
    principal: CurrentPrincipal,
    service: CurrentExchangeService,
    exchanges: FillExchanges = None,
    from_: FillsFrom = None,
    to: FillsTo = None,
    limit: FillsLimit = DEFAULT_FILLS_LIMIT,
    offset: FillsOffset = 0,
) -> ExchangeFillListResponse:
    """Return one page of fills, newest first, and the totals over every fill that matched.

    **The totals cover the whole filtered set, not the page**, so they are the same whatever
    `limit` and `offset` are, and a client never sums a page. Every amount is a JSON string.
    Only a USDT-quoted fill has a USDT value; any other quote is totalled in its own asset
    under `not_valued_in_usdt` and never converted. Totals cover only what has been imported:
    `GET /api/exchanges` says when that history is partial.

    A naive `from` or `to`, one that is not ISO 8601, or `from` not before `to` is a 422, as
    is an unknown `exchange`. Reads the database; calls no venue.
    """
    try:
        page = await service.list_fills(
            principal.user_id,
            exchanges=exchanges,
            from_=from_,
            to=to,
            limit=limit,
            offset=offset,
        )
    except InvalidFillRangeError as exc:
        raise _range_refused(exc) from exc
    return ExchangeFillListResponse.of(page)
