"""AppError hierarchy and RFC 9457 ``application/problem+json`` handlers.

Every error that leaves the API goes through one of the handlers below:
consistent shape, `request_id` included, and internals (stack traces, SQL)
never leak to clients.
"""

from collections.abc import Mapping
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"


class AppError(Exception):
    """Base class for all expected application errors.

    Subclasses set ``status_code``, ``slug`` and ``title``; callers pass a
    human-readable ``detail`` (safe for end users) and optional ``extra``
    fields merged into the problem document.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    slug: str = "internal-error"
    title: str = "Internal Server Error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.extra = extra


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    slug = "not-found"
    title = "Resource Not Found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    slug = "conflict"
    title = "Conflict"


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    slug = "permission-denied"
    title = "Permission Denied"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    slug = "unauthorized"
    title = "Authentication Required"


class QuotaExceededError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    slug = "quota-exceeded"
    title = "Plan Quota Exceeded"


class TenantSuspendedError(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    slug = "tenant-suspended"
    title = "Tenant Suspended"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    slug = "rate-limited"
    title = "Too Many Requests"


class InvalidWebhookError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    slug = "invalid-webhook"
    title = "Invalid Webhook"


class UpstreamUnavailableError(AppError):
    """A third-party dependency (e.g. an AI provider, §8.18) failed or timed
    out. Surfaced as 503 problem+json so the client can retry — never a 500 or
    a hang."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    slug = "upstream-unavailable"
    title = "Upstream Service Unavailable"


def _request_id() -> str | None:
    rid = structlog.contextvars.get_contextvars().get("request_id")
    return str(rid) if rid is not None else None


def problem_response(
    request: Request,
    *,
    status_code: int,
    slug: str,
    title: str,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{get_settings().problem_type_base}{slug}",
        "title": title,
        "status": status_code,
        "instance": request.url.path,
    }
    if detail:
        body["detail"] = detail
    if rid := _request_id():
        body["request_id"] = rid
    if extra:
        body.update(extra)
    return JSONResponse(
        body, status_code=status_code, media_type=PROBLEM_CONTENT_TYPE, headers=headers
    )


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return problem_response(
        request,
        status_code=exc.status_code,
        slug=exc.slug,
        title=exc.title,
        detail=exc.detail,
        extra=exc.extra,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return problem_response(
        request,
        status_code=exc.status_code,
        slug="http-error",
        title=str(exc.detail),
        headers=exc.headers,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Only type/loc/msg: pydantic's `input`/`ctx` may echo PII or hold
    # non-serializable exception objects.
    errors = [{key: err.get(key) for key in ("type", "loc", "msg")} for err in exc.errors()]
    return problem_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        slug="validation-error",
        title="Validation Error",
        detail="The request payload failed validation.",
        extra={"errors": errors},
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error", exc_info=exc)
    return problem_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        slug="internal-error",
        title="Internal Server Error",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
