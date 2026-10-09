"""The monthly export reminder's endpoints (spec 040).

Thin on purpose: the routes parse the month, call the service and serialize. Neither path is
in `PUBLIC_API_PATHS`, so both require a session: the deny-by-default middleware's doing, not
this module's.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.exceptions import RequestValidationError

from portfolio.api.dependencies import get_export_reminder_service, get_principal
from portfolio.api.errors import ConflictError
from portfolio.api.schemas.exports import MONTH_PATTERN, ExportReminderResponse
from portfolio.domain.export_reminders import (
    MonthNotClosedError,
    MonthNotRemindedError,
    parse_month,
)
from portfolio.services.auth import Principal
from portfolio.services.export_reminders import ExportReminderService

# Declared here rather than imported from `api.dependencies`, for the reason the wallet
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentReminderService = Annotated[ExportReminderService, Depends(get_export_reminder_service)]
MonthParam = Annotated[
    str,
    Path(pattern=MONTH_PATTERN, description="The month, YYYY-MM, in Argentina time."),
]

router = APIRouter(prefix="/exports", tags=["exports"])


@router.get(
    "/reminder",
    operation_id="readExportReminder",
    summary="The closed months whose exchange exports are not marked done",
    response_model=ExportReminderResponse,
)
async def read_export_reminder(
    principal: CurrentPrincipal,
    service: CurrentReminderService,
) -> ExportReminderResponse:
    """Every month from the first reminded one that has ended and is not marked done."""
    reminder = await service.reminder(principal.user_id)
    return ExportReminderResponse.of(reminder)


@router.post(
    "/months/{month}/done",
    operation_id="markExportMonthDone",
    summary="Mark a month's exchange exports as done",
    response_model=ExportReminderResponse,
)
async def mark_export_month_done(
    month: MonthParam,
    principal: CurrentPrincipal,
    service: CurrentReminderService,
) -> ExportReminderResponse:
    """Mark `month` done and answer what is still owed. Marking it twice changes nothing.

    A month that has not ended yet, or that is before the first reminded month, is a `409`.
    """
    try:
        parsed = parse_month(month)
    except ValueError as exc:
        raise RequestValidationError(
            [{"loc": ("path", "month"), "msg": str(exc), "type": "invalid_month"}]
        ) from exc
    try:
        reminder = await service.mark_done(principal.user_id, parsed)
    except (MonthNotClosedError, MonthNotRemindedError) as exc:
        raise ConflictError(str(exc)) from exc
    return ExportReminderResponse.of(reminder)
