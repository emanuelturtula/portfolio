"""The four wallet registry endpoints.

Thin on purpose: each one parses a body, calls a service, and turns the service's exception
into a status code. None of the policy is here -- not what makes an address valid, not what
makes a duplicate a conflict, not what `DELETE` does -- because a rule that lives in a
router is a rule the CLI does not have.

None of these paths is in `PUBLIC_API_PATHS`, so all four require a session. That is not a
decision made here; it is what the deny-by-default middleware does with any path it has not
been told to let through, which is why adding an endpoint protects it.

**A rejected address becomes a `RequestValidationError`.** That is deliberate rather than
convenient: it renders through the handler Pydantic's own failures already use, so the 422
body is byte for byte the shape a client already handles -- a problem document plus an
`errors` array of `{loc, msg, type}`. The `msg` is the domain's fixed sentence for the
reason and the `type` is the reason itself, so a client can branch on `bad_checksum`
without parsing prose. **Neither one contains the address**, which is the whole point: a
422 is the one response that would otherwise carry the owner's address back out of the
process and into a client-side log.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.exceptions import RequestValidationError

from portfolio.api.dependencies import get_principal, get_wallet_service
from portfolio.api.errors import ConflictError, NotFoundError
from portfolio.api.schemas.wallets import (
    LABEL_FIELD_NAME,
    WalletCreateRequest,
    WalletListResponse,
    WalletResponse,
    WalletUpdateRequest,
)
from portfolio.domain.addresses import AddressInvalidError
from portfolio.services.auth import Principal
from portfolio.services.wallets import (
    UNSET,
    WalletAlreadyExistsError,
    WalletNotFoundError,
    WalletService,
)

# Declared here rather than imported from `api.dependencies`, for the reason the auth
# router documents: FastAPI resolves these annotations at import time to build the
# dependency graph, so the names inside them are runtime values wearing a type's clothes.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentWalletService = Annotated[WalletService, Depends(get_wallet_service)]
IncludeArchived = Annotated[
    bool,
    Query(description="Include wallets that have been archived."),
]

router = APIRouter(prefix="/wallets", tags=["wallets"])

ADDRESS_FIELD_LOCATION = ("body", "address")
"""Where a rejected address is reported. Always the address field: `chain_key` is an
enumeration in the request schema, so an unknown chain never reaches the domain."""


def _address_rejected(exc: AddressInvalidError) -> RequestValidationError:
    """Turn a domain rejection into the field-level 422 the API already speaks.

    The reason travels as the error's `type`, which is what lets a client distinguish "you
    mistyped a character" from "that is an extended public key, not an address" without
    reading the sentence -- and without the sentence ever having to name the value.
    """
    return RequestValidationError(
        [
            {
                "loc": ADDRESS_FIELD_LOCATION,
                "msg": exc.message,
                "type": exc.reason.value,
            }
        ]
    )


@router.get(
    "",
    operation_id="listWallets",
    summary="List the wallets balances are read from",
    response_model=WalletListResponse,
)
async def list_wallets(
    principal: CurrentPrincipal,
    service: CurrentWalletService,
    include_archived: IncludeArchived = False,
) -> WalletListResponse:
    """Return the caller's wallets, oldest first, archived ones hidden by default."""
    views = await service.list_wallets(principal, include_archived=include_archived)
    return WalletListResponse(wallets=[WalletResponse.of(view) for view in views])


@router.post(
    "",
    operation_id="createWallet",
    summary="Register an address to read balances from",
    status_code=status.HTTP_201_CREATED,
    response_model=WalletResponse,
)
async def create_wallet(
    body: WalletCreateRequest,
    principal: CurrentPrincipal,
    service: CurrentWalletService,
) -> WalletResponse:
    """Register an address after verifying its checksum, offline.

    A duplicate is a 409 whether or not the row holding the slot is archived, and the
    problem detail says which -- because "you already have this" and "you archived this"
    call for different next steps from the owner.
    """
    try:
        view = await service.create_wallet(
            principal,
            chain_key=body.chain_key.value,
            address=body.address,
            label=body.label,
        )
    except AddressInvalidError as exc:
        raise _address_rejected(exc) from exc
    except WalletAlreadyExistsError as exc:
        raise ConflictError(str(exc)) from exc
    return WalletResponse.of(view)


@router.patch(
    "/{wallet_id}",
    operation_id="updateWallet",
    summary="Rename a wallet, archive it, or restore it",
    response_model=WalletResponse,
)
async def update_wallet(
    wallet_id: int,
    body: WalletUpdateRequest,
    principal: CurrentPrincipal,
    service: CurrentWalletService,
) -> WalletResponse:
    """Apply only the fields the request actually sent.

    `model_fields_set` is what separates "the label was omitted" from `"label": null`; the
    first leaves the label alone and the second clears it. Without that distinction,
    archiving a wallet would silently erase its name.
    """
    label = body.label if LABEL_FIELD_NAME in body.model_fields_set else UNSET
    archived = body.archived if body.archived is not None else UNSET
    try:
        view = await service.update_wallet(principal, wallet_id, label=label, archived=archived)
    except WalletNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return WalletResponse.of(view)


@router.delete(
    "/{wallet_id}",
    operation_id="archiveWallet",
    summary="Archive a wallet, keeping its history",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def archive_wallet(
    wallet_id: int,
    principal: CurrentPrincipal,
    service: CurrentWalletService,
) -> Response:
    """Archive rather than delete, and succeed again if it is already archived.

    Nothing is removed, because the balance snapshots that will reference `wallets.id`
    need the row to outlive the owner's interest in the address. Repeating the request is
    a 204 rather than a 404: the end state the caller asked for is the end state they get,
    so a retry after a dropped response is not an error.
    """
    try:
        await service.archive_wallet(principal, wallet_id)
    except WalletNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
