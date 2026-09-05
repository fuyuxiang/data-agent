from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app, has_request_context, session

from ..core.database import Database


def _day_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def quota_status(database: Database, workspace_id: str) -> dict:
    limit = current_app.config["SETTINGS"].daily_token_limit
    used = database.usage_total(workspace_id, _day_start())
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "allowed": used < limit,
        "period": "UTC day",
    }


def ensure_quota(database: Database, workspace_id: str) -> dict:
    status = quota_status(database, workspace_id)
    if not status["allowed"]:
        raise PermissionError("当前工作空间今日模型 Token 额度已用尽")
    return status


def record_usage(
    database: Database,
    workspace_id: str,
    usage: dict,
    *,
    session_id: str = "",
    operation: str = "agent",
) -> dict | None:
    total = int(usage.get("total_tokens") or 0) or (
        int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    )
    if total <= 0:
        return None
    return database.put(
        "usage_events",
        {
            "id": database.new_id("usage"), "workspace_id": workspace_id,
            "session_id": session_id,
            "user_id": str(session.get("user_id") or "local-default") if has_request_context() else "system",
            "operation": operation, "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": total, "model": str(usage.get("model") or ""),
        },
        workspace_id=workspace_id,
    )


def response_usage(response, model: str) -> dict:
    usage = getattr(response, "usage", None)

    def value(name: str) -> int:
        return int(usage.get(name, 0) if isinstance(usage, dict) else getattr(usage, name, 0) or 0)

    prompt = value("prompt_tokens") or value("input_tokens")
    completion = value("completion_tokens") or value("output_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": value("total_tokens") or prompt + completion,
        "model": model,
    }
