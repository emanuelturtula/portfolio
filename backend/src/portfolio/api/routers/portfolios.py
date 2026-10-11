"""The portfolio registry: list, create, rename and delete portfolios (spec 044).

Thin on purpose: each route parses, calls the service and serializes. What a name may be and
what may be deleted are the service's rules. No path here is in `PUBLIC_API_PATHS`, so every
one requires a session: the deny-by-default middleware's doing, not this module's.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from portfolio.api.dependencies import get_portfolio_registry_service, get_principal
from portfolio.api.errors import ConflictError, NotFoundError, UnprocessableEntityError
from portfolio.api.schemas.portfolios import (
    PortfolioListResponse,
    PortfolioRequest,
    PortfolioResponse,
)
from portfolio.services.auth import Principal
from portfolio.services.portfolios import (
    PortfolioInUseError,
    PortfolioNameTakenError,
    PortfolioNotFoundError,
    PortfolioRegistryService,
)

# Declared here rather than imported from `api.dependencies`, for the reason the balance
# router gives: FastAPI resolves these annotations at import time.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentRegistry = Annotated[PortfolioRegistryService, Depends(get_portfolio_registry_service)]

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


@router.get(
    "",
    operation_id="listPortfolios",
    summary="Every portfolio, with how many wallets and operations each holds",
    response_model=PortfolioListResponse,
)
async def list_portfolios(
    principal: CurrentPrincipal, service: CurrentRegistry
) -> PortfolioListResponse:
    """Oldest first. The first is where a wallet, an upload or a manual entry that names no
    portfolio goes."""
    views = await service.list_portfolios(principal.user_id)
    return PortfolioListResponse(portfolios=[PortfolioResponse.of(view) for view in views])


@router.post(
    "",
    operation_id="createPortfolio",
    summary="Create a portfolio",
    status_code=status.HTTP_201_CREATED,
    response_model=PortfolioResponse,
)
async def create_portfolio(
    body: PortfolioRequest, principal: CurrentPrincipal, service: CurrentRegistry
) -> PortfolioResponse:
    """A name already in use is a 409."""
    try:
        view = await service.create(principal.user_id, body.name)
    except ValueError as exc:
        raise UnprocessableEntityError(str(exc)) from exc
    except PortfolioNameTakenError as exc:
        raise ConflictError(str(exc)) from exc
    return PortfolioResponse.of(view)


@router.patch(
    "/{portfolio_id}",
    operation_id="renamePortfolio",
    summary="Rename a portfolio",
    response_model=PortfolioResponse,
)
async def rename_portfolio(
    portfolio_id: int,
    body: PortfolioRequest,
    principal: CurrentPrincipal,
    service: CurrentRegistry,
) -> PortfolioResponse:
    """A name another portfolio has is a 409; its own name again is not."""
    try:
        view = await service.rename(principal.user_id, portfolio_id, body.name)
    except ValueError as exc:
        raise UnprocessableEntityError(str(exc)) from exc
    except PortfolioNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except PortfolioNameTakenError as exc:
        raise ConflictError(str(exc)) from exc
    return PortfolioResponse.of(view)


@router.delete(
    "/{portfolio_id}",
    operation_id="deletePortfolio",
    summary="Delete an empty portfolio",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_portfolio(
    portfolio_id: int, principal: CurrentPrincipal, service: CurrentRegistry
) -> Response:
    """Only a portfolio that holds no wallet and no operation, and is not the last one, can be
    deleted; otherwise a 409 says which. Nothing is ever deleted along with it."""
    try:
        await service.delete(principal.user_id, portfolio_id)
    except PortfolioNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except PortfolioInUseError as exc:
        raise ConflictError(str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
