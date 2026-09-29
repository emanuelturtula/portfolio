"""The four manual-adjustment endpoints (#18): list, create, replace and delete.

Thin on purpose: each parses a body, calls one service method, and turns the service's
exception into a status code. None of the policy is here -- not what makes an adjustment
acceptable, not the order of the list, not the recompute a change sets off -- because a rule
that lives in a router is a rule the CLI does not have.

**A change is answered after the snapshot is recomputed.** The service commits, then awaits the
recompute that `get_adjustment_service` bound to it, then returns; so a client that reads
`GET /api/accounting/positions` after the response sees the change. A recompute that fails
leaves the change saved and says `failed` in that endpoint's `last_recompute`.

None of these paths is in `PUBLIC_API_PATHS`, so all four require a session: the deny-by-default
middleware's doing, not this module's.

**A refused adjustment becomes a `RequestValidationError`**, as a refused address does in the
wallet router, so the 422 is the problem document a client already handles: `errors` holds one
`{loc: ["body", <field>], msg: <rule>, type: "value_error"}`. The rule is a sentence naming the
field and what it must be; **no value is ever in it**, because a 422 is the one response that
would otherwise carry the owner's figures back out into a client-side log.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Response, status
from fastapi.exceptions import RequestValidationError

from portfolio.api.dependencies import get_adjustment_service, get_principal
from portfolio.api.errors import NotFoundError
from portfolio.api.schemas.adjustments import (
    AdjustmentCreateRequest,
    AdjustmentListResponse,
    AdjustmentReplaceRequest,
    AdjustmentResponse,
)
from portfolio.services.adjustments import (
    AdjustmentNotFoundError,
    AdjustmentService,
    InvalidAdjustmentError,
)
from portfolio.services.auth import Principal

# Declared here rather than imported from `api.dependencies`, for the reason the wallet router
# gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentAdjustmentService = Annotated[AdjustmentService, Depends(get_adjustment_service)]

MAX_ADJUSTMENT_ID: Final = 2**63 - 1
"""The largest id SQLite can hold: its `INTEGER` is a signed 64-bit integer.

A larger id cannot name a row, and binding one raises `OverflowError` inside the driver -- a
500 with a traceback -- so the path parameter refuses it first, as a 422 (spec 023, R8). An id
below 1 is refused the same way: `AUTOINCREMENT` starts at 1, so none names a row either.
"""

AdjustmentId = Annotated[
    int,
    Path(ge=1, le=MAX_ADJUSTMENT_ID, description="The adjustment's id, as a create returned it."),
]

router = APIRouter(prefix="/accounting/adjustments", tags=["accounting"])

REFUSAL_TYPE: Final = "value_error"
"""The `type` of a refused adjustment's error: what Pydantic gives a `ValueError` in a
validator, which is what a refusal by the engine's `Adjustment` is."""


def _refused(exc: InvalidAdjustmentError) -> RequestValidationError:
    """The service's refusal as the field-level 422 the API already speaks. No value in it."""
    return RequestValidationError(
        [{"loc": ("body", exc.field), "msg": exc.rule, "type": REFUSAL_TYPE}]
    )


@router.get(
    "",
    operation_id="listAdjustments",
    summary="List the manual adjustments: opening balances and off-exchange acquisitions",
    response_model=AdjustmentListResponse,
)
async def list_adjustments(
    principal: CurrentPrincipal,
    service: CurrentAdjustmentService,
) -> AdjustmentListResponse:
    """Return the caller's adjustments, by `occurred_at` and then id: the order they replay in."""
    views = await service.list(principal.user_id)
    return AdjustmentListResponse(adjustments=[AdjustmentResponse.of(view) for view in views])


@router.post(
    "",
    operation_id="createAdjustment",
    summary="Record coins the imported history does not show",
    status_code=status.HTTP_201_CREATED,
    response_model=AdjustmentResponse,
)
async def create_adjustment(
    body: AdjustmentCreateRequest,
    principal: CurrentPrincipal,
    service: CurrentAdjustmentService,
) -> AdjustmentResponse:
    """Record an inflow: `quantity` of `asset` acquired at `occurred_at`, at `unit_cost` or unknown.

    For an opening balance -- coins bought before the exchange history begins -- **date it
    before the first sale it has to cover.** An adjustment replays among the fills by
    `occurred_at`, and one at the same instant as a fill replays after it.

    Omit `unit_cost`, or send `null`, when the cost is not known: the quantity counts, the cost
    does not, and the asset shows `unknown_basis`. A sale of those units then realizes no profit
    and its proceeds go to `unmatched_proceeds`. Anything the accounting engine could not replay
    is refused here with a 422, and nothing is stored. The positions are recomputed before this
    answers.
    """
    try:
        view = await service.create(principal.user_id, body.to_draft())
    except InvalidAdjustmentError as exc:
        raise _refused(exc) from exc
    return AdjustmentResponse.of(view)


@router.put(
    "/{adjustment_id}",
    operation_id="replaceAdjustment",
    summary="Replace a manual adjustment's five fields",
    response_model=AdjustmentResponse,
)
async def replace_adjustment(
    adjustment_id: AdjustmentId,
    body: AdjustmentReplaceRequest,
    principal: CurrentPrincipal,
    service: CurrentAdjustmentService,
) -> AdjustmentResponse:
    """Replace every editable field, `unit_cost` included: `null` makes the cost unknown.

    `PUT` rather than `PATCH`, because `unit_cost: null` is a value, and a partial update could
    not tell it from a field that was not sent. The body is validated before the id is looked
    up. Another owner's id and a missing one are the same 404. The positions are recomputed
    before this answers.
    """
    try:
        view = await service.update(principal.user_id, adjustment_id, body.to_draft())
    except InvalidAdjustmentError as exc:
        raise _refused(exc) from exc
    except AdjustmentNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return AdjustmentResponse.of(view)


@router.delete(
    "/{adjustment_id}",
    operation_id="deleteAdjustment",
    summary="Delete a manual adjustment",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_adjustment(
    adjustment_id: AdjustmentId,
    principal: CurrentPrincipal,
    service: CurrentAdjustmentService,
) -> Response:
    """Delete the adjustment; its id is never reused. The positions are recomputed first.

    A repeat is a 404, as are another owner's id and a missing one.

    **`/api/docs` cannot send this one.** Every write must carry `Content-Type:
    application/json`, and Swagger UI sends no content type for a request without a body, so
    the request is refused with a 403 before it gets here. `docs/operations.md`, section 15,
    gives the one line to run in the browser console instead.
    """
    try:
        await service.delete(principal.user_id, adjustment_id)
    except AdjustmentNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
