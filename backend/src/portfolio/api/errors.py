"""Error rendering as RFC 9457 problem details (`application/problem+json`).

Every error the API returns has the same shape, so the frontend needs exactly one code
path to display a failure, and an unexpected exception never leaks its message -- which
may quote a database row, a URL or a credential -- to the client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Final

import structlog
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI, Request
    from starlette.responses import Response

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

# What the client is told when the server hits something it did not plan for. The real
# cause goes to the log, never to the response body.
UNEXPECTED_DETAIL: Final = "The server encountered an unexpected condition."

_logger = structlog.get_logger(__name__)


class ProblemDetail(BaseModel):
    """An RFC 9457 problem details document."""

    type: str = Field(default="about:blank", description="URI identifying the problem type.")
    title: str = Field(description="Short, human-readable summary of the problem type.")
    status: int = Field(description="HTTP status code generated for this occurrence.")
    detail: str | None = Field(default=None, description="Explanation specific to this occurrence.")
    instance: str | None = Field(default=None, description="URI identifying this occurrence.")


class AppError(Exception):
    """Base class for failures whose title and status are safe to show a client.

    Subclasses override the class attributes; the message passed to the constructor
    becomes the `detail` member of the problem document.
    """

    status: ClassVar[int] = 500
    title: ClassVar[str] = "Internal Server Error"
    problem_type: ClassVar[str] = "about:blank"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail

    def as_problem(self, instance: str | None = None) -> ProblemDetail:
        """Render this error as a problem details document."""
        return ProblemDetail(
            type=self.problem_type,
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
        )


class UnauthorizedError(AppError):
    """No usable session, or credentials that did not verify.

    One class for both, because the API deliberately does not distinguish "this username
    does not exist" from "this password is wrong": the client sees the same document and
    the same status either way.
    """

    status: ClassVar[int] = 401
    title: ClassVar[str] = "Unauthorized"


class ForbiddenError(AppError):
    """A request that was rejected before it reached a route: origin or content type."""

    status: ClassVar[int] = 403
    title: ClassVar[str] = "Forbidden"


class TooManyRequestsError(AppError):
    """A throttled sign-in attempt, refused without verifying the password."""

    status: ClassVar[int] = 429
    title: ClassVar[str] = "Too Many Requests"


class UnprocessableEntityError(AppError):
    """A well-formed request whose content a rule refuses -- a password below policy."""

    status: ClassVar[int] = 422
    title: ClassVar[str] = "Unprocessable Entity"


def problem_response(
    problem: ProblemDetail,
    extensions: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Serialise a problem document with the media type RFC 9457 requires."""
    content: dict[str, Any] = problem.model_dump(exclude_none=True)
    if extensions:
        content.update(extensions)
    return JSONResponse(
        status_code=problem.status,
        content=content,
        media_type=PROBLEM_CONTENT_TYPE,
    )


async def handle_app_error(request: Request, exc: Exception) -> Response:
    """Render a deliberate application error."""
    if not isinstance(exc, AppError):  # pragma: no cover - registered per exception type
        raise exc
    problem = exc.as_problem(instance=request.url.path)
    _logger.warning(
        "request_failed",
        status=problem.status,
        title=problem.title,
        path=request.url.path,
        method=request.method,
    )
    return problem_response(problem)


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    """Render a request that failed validation, keeping the field-level detail."""
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registered per type
        raise exc
    errors = [
        {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]
    problem = ProblemDetail(
        title="Unprocessable Entity",
        status=422,
        detail="The request parameters failed validation.",
        instance=request.url.path,
    )
    return problem_response(problem, {"errors": jsonable_encoder(errors)})


async def handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """Render an unhandled exception without telling the client what went wrong."""
    _logger.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
    )
    problem = ProblemDetail(
        title="Internal Server Error",
        status=500,
        detail=UNEXPECTED_DETAIL,
        instance=request.url.path,
    )
    return problem_response(problem)


def register_exception_handlers(app: FastAPI) -> None:
    """Install the problem-details handlers on an application."""
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
