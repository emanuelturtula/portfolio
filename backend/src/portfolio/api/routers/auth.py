"""The four authentication endpoints.

Thin on purpose: each one parses a body, calls a service, and turns the service's
exception into a status code. There is no policy here -- not the password rules, not the
session lifetime, not the throttle -- because a rule that lives in a router is a rule the
CLI and the bootstrap path do not have.

The session cookie is set and cleared here, because a cookie is a transport concern and
the service has no business knowing what HTTP is. `api.session_cookie` decides which
cookie a request gets.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from portfolio.api.dependencies import get_auth_service, get_principal
from portfolio.api.errors import TooManyRequestsError, UnauthorizedError, UnprocessableEntityError
from portfolio.api.session_cookie import clear_session_cookie, set_session_cookie
from portfolio.domain.passwords import PasswordPolicyError
from portfolio.services.auth import (
    AuthService,
    InvalidCredentialsError,
    Principal,
    TooManyAttemptsError,
)

# Declared here rather than imported from `api.dependencies`: FastAPI resolves these
# annotations at import time to build the dependency graph, so the names inside them are
# runtime values wearing a type's clothes. An alias imported from another module reads to
# a linter as a typing-only import, and moving it into a type-checking block would break
# the server while leaving the type checker perfectly happy.
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentAuthService = Annotated[AuthService, Depends(get_auth_service)]

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Credentials submitted by the login form."""

    username: str = Field(min_length=1, max_length=200)
    # No maximum length rule beyond a sane bound: Argon2id hashes any length in constant
    # memory, and a passphrase is the kind of password this policy wants to encourage.
    password: str = Field(min_length=1, max_length=1024)


class PasswordChangeRequest(BaseModel):
    """A password change, which revokes every session including the caller's own."""

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    """Who the caller is. Deliberately the only thing a session read discloses."""

    username: str


@router.post(
    "/login",
    operation_id="login",
    summary="Exchange a username and password for a session cookie",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def login(
    body: LoginRequest,
    service: CurrentAuthService,
    request: Request,
) -> Response:
    """Sign in. An unknown username and a wrong password are the same answer."""
    try:
        issued = await service.login(body.username, body.password)
    except TooManyAttemptsError as exc:
        raise TooManyRequestsError(str(exc)) from exc
    except InvalidCredentialsError as exc:
        raise UnauthorizedError(str(exc)) from exc

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    set_session_cookie(response, request, issued.token)
    return response


@router.post(
    "/logout",
    operation_id="logout",
    summary="Revoke the current session",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def logout(
    principal: CurrentPrincipal,
    service: CurrentAuthService,
) -> Response:
    """Sign out. The row is deleted, so replaying the cookie is worth nothing."""
    await service.logout(principal)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response)
    return response


@router.get(
    "/session",
    operation_id="getSession",
    summary="Report who the current session belongs to",
    response_model=SessionResponse,
)
async def get_session(principal: CurrentPrincipal) -> SessionResponse:
    """Return the signed-in username. The middleware has already proven the session."""
    return SessionResponse(username=principal.username)


@router.post(
    "/password",
    operation_id="changePassword",
    summary="Change the password and revoke every session",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def change_password(
    body: PasswordChangeRequest,
    principal: CurrentPrincipal,
    service: CurrentAuthService,
) -> Response:
    """Change the password. Every session dies, the caller's included, so the cookie goes."""
    try:
        await service.change_password(principal, body.current_password, body.new_password)
    except TooManyAttemptsError as exc:
        # The same counter login uses, on the same username. A guessed current password is
        # a permanent takeover of an application with no reset flow, so this is the path
        # that most needs a limit, not the one that least needs one.
        raise TooManyRequestsError(str(exc)) from exc
    except PasswordPolicyError as exc:
        raise UnprocessableEntityError(str(exc)) from exc
    except InvalidCredentialsError as exc:
        raise UnauthorizedError(str(exc)) from exc

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response)
    return response
