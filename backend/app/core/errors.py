"""Stable, user-safe error taxonomy.

Every public-facing error response is shaped as
``{"error_code": <machine-readable code>, "message": <user-safe copy>,
"trace_id": <current request trace id>}``. Internal detail lives in the
admin-level trace payload only — never in the HTTP body.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Stable machine codes. Never rename: clients may branch on these."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION = "validation"
    INTERNAL = "internal"
    UPSTREAM = "upstream"
    RATE_LIMITED = "rate_limited"


_USER_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.UNAUTHORIZED: "需要登录",
    ErrorCode.FORBIDDEN: "没有访问权限",
    ErrorCode.NOT_FOUND: "记录不存在",
    ErrorCode.CONFLICT: "状态冲突，请重试",
    ErrorCode.VALIDATION: "请求参数不合法",
    ErrorCode.INTERNAL: "服务暂时不可用，请稍后重试",
    ErrorCode.UPSTREAM: "上游服务响应异常",
    ErrorCode.RATE_LIMITED: "请求过于频繁，请稍后再试",
}


def message_for(code: ErrorCode) -> str:
    return _USER_MESSAGES[code]