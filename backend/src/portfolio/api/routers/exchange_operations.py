"""The exchange-operation endpoints: upload an export, list, enter by hand, delete (spec 042),
and the invested figures built on them.

Thin on purpose: each route parses, calls a service and serializes. Recognising a format,
deduplicating and every sum are the service's and the domain's.

No path here is in `PUBLIC_API_PATHS`, so every one requires a session: the deny-by-default
middleware's doing, not this module's. The upload is JSON, base64 inside, because the write
guard lets only JSON change state (R12).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from portfolio.api.dependencies import (
    get_exchange_operation_service,
    get_investment_service,
    get_principal,
)
from portfolio.api.errors import ConflictError, NotFoundError, UnprocessableEntityError
from portfolio.api.schemas.exchange_operations import (
    ImportRequest,
    ImportResponse,
    InvestmentResponse,
    ManualOperationRequest,
    OperationListResponse,
    OperationResponse,
)
from portfolio.services.auth import Principal
from portfolio.services.exchange_operations import (
    ExchangeOperationService,
    ManualOperation,
    OperationNotFoundError,
    OperationNotManualError,
    UploadRefusedError,
)
from portfolio.services.investment import InvestmentService

# Declared here rather than imported from `api.dependencies`, for the reason the balance
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentOperationService = Annotated[
    ExchangeOperationService, Depends(get_exchange_operation_service)
]
CurrentInvestmentService = Annotated[InvestmentService, Depends(get_investment_service)]

MAX_PAGE: int = 500

router = APIRouter(tags=["exchange operations"])


@router.post(
    "/exchange-operations/imports",
    operation_id="importExchangeOperations",
    summary="Upload an exchange's export, a CSV or a zip of them",
    status_code=status.HTTP_201_CREATED,
    response_model=ImportResponse,
)
async def import_operations(
    body: ImportRequest,
    principal: CurrentPrincipal,
    service: CurrentOperationService,
) -> ImportResponse:
    """Store every operation of the upload not already stored, and say what each file held.

    A file is recognised by its header row. One this importer does not read is reported as
    skipped, with its row count. A row that cannot be read refuses the whole upload with a
    422 naming the file and the line, and nothing is stored.
    """
    try:
        report = await service.import_upload(principal.user_id, body.filename, body.content_base64)
    except UploadRefusedError as exc:
        raise UnprocessableEntityError(str(exc)) from exc
    return ImportResponse.of(report)


@router.get(
    "/exchange-operations",
    operation_id="listExchangeOperations",
    summary="The stored operations, newest first",
    response_model=OperationListResponse,
)
async def list_operations(
    principal: CurrentPrincipal,
    service: CurrentOperationService,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OperationListResponse:
    """One page of every stored operation, of every asset, with the total count."""
    page = await service.list_operations(principal.user_id, limit=limit, offset=offset)
    return OperationListResponse.of(page)


@router.post(
    "/exchange-operations",
    operation_id="createManualOperation",
    summary="Record a buy or a sell no export covers",
    status_code=status.HTTP_201_CREATED,
    response_model=OperationResponse,
)
async def create_manual_operation(
    body: ManualOperationRequest,
    principal: CurrentPrincipal,
    service: CurrentOperationService,
) -> OperationResponse:
    """Store a manual entry, such as a swap inside a wallet app. It can be deleted later."""
    fee_asset = body.fee_asset.strip().upper() if body.fee_asset else None
    view = await service.add_manual(
        principal.user_id,
        ManualOperation(
            venue=body.venue.strip(),
            executed_at=body.executed_at,
            kind=body.kind,
            asset=body.asset.strip().upper(),
            quantity=body.quantity,
            quote_currency=body.quote_currency.strip().upper(),
            quote_amount=body.quote_amount,
            fee_asset=fee_asset if body.fee_amount is not None else None,
            fee_amount=body.fee_amount if fee_asset is not None else None,
            description=body.description.strip(),
        ),
    )
    return OperationResponse.of(view)


@router.delete(
    "/exchange-operations/{operation_id}",
    operation_id="deleteManualOperation",
    summary="Delete a manual entry",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_manual_operation(
    operation_id: int,
    principal: CurrentPrincipal,
    service: CurrentOperationService,
) -> Response:
    """Delete one manual entry. An imported operation is a 409: re-uploading would bring it
    back, so deleting it would only hide it until then."""
    try:
        await service.delete_manual(principal.user_id, operation_id)
    except OperationNotFoundError as exc:
        raise NotFoundError("No such operation.") from exc
    except OperationNotManualError as exc:
        raise ConflictError("Only a manual entry can be deleted.") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/investment",
    operation_id="readInvestment",
    summary="Invested, value, profit and loss, and the quantities the operations explain",
    response_model=InvestmentResponse,
)
async def read_investment(
    principal: CurrentPrincipal,
    service: CurrentInvestmentService,
) -> InvestmentResponse:
    """The figures for the assets of the active wallets, from the stored operations.

    **A figure that cannot be known is `null` with the reason in `unavailable`, never `"0"`**:
    a trade not priced in a stablecoin, or an asset with no value now. Every amount is a JSON
    string.
    """
    investment = await service.investment(principal.user_id)
    return InvestmentResponse.of(investment)
