"""Unified error envelope.

Every public-facing error response is shaped as
``{"error_code", "message", "trace_id"}``. The internal FastAPI / Pydantic
``detail`` and any exception traceback never reach the client. The same
handler is the single source of truth for status code → error_code mapping.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import ErrorCode, message_for


logger = logging.getLogger(__name__)


def _trace_id(request: Request) -> str:
    """Return a per-request trace id, reusing the inbound header if the
    gateway already minted one so cross-service log lines correlate."""
    incoming = request.headers.get("X-Trace-Id")
    return incoming or uuid.uuid4().hex


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    status.HTTP_400_BAD_REQUEST: ErrorCode.VALIDATION,
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_409_CONFLICT: ErrorCode.CONFLICT,
    status.HTTP_422_UNPROCESSABLE_ENTITY: ErrorCode.VALIDATION,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
}


def _envelope(
    *,
    code: ErrorCode,
    trace_id: str,
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error_code": code.value,
            "message": message_for(code),
            "trace_id": trace_id,
        },
        headers={"X-Trace-Id": trace_id},
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    trace_id = _trace_id(request)
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL)
    # Log the raw detail server-side only — never echo it to the client.
    logger.warning(
        "http_error trace_id=%s status=%s code=%s detail=%s",
        trace_id,
        exc.status_code,
        code.value,
        exc.detail,
    )
    return _envelope(code=code, trace_id=trace_id, status_code=exc.status_code)


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    trace_id = _trace_id(request)
    # Pydantic validation errors are noisy and frequently leak schema details;
    # log them server-side, return the safe envelope.
    logger.info(
        "validation_error trace_id=%s errors=%s", trace_id, exc.errors()
    )
    return _envelope(
        code=ErrorCode.VALIDATION,
        trace_id=trace_id,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    trace_id = _trace_id(request)
    # Last-resort handler. Logs the full exception server-side; the client
    # only learns that the system is unavailable.
    logger.exception("unhandled trace_id=%s", trace_id)
    return _envelope(
        code=ErrorCode.INTERNAL,
        trace_id=trace_id,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def register(app: FastAPI) -> None:
    """Wire the unified error handlers into the FastAPI app."""
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)