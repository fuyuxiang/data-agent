from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Callable

from flask import current_app, jsonify, request, session

from ..core.database import Database
from ..services.authorization import require_result_access as require_result_policy
from ..services.authorization import require_job_access as require_job_policy
from ..services.authorization import require_session_access as require_session_policy
from ..services.authorization import require_source_access as require_source_policy


def db() -> Database:
    return current_app.extensions["meridian_db"]


def body() -> dict:
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def workspace_id() -> str:
    return str(
        request.args.get("workspace_id") or request.headers.get("X-Workspace-Id")
        or body().get("workspace_id") or request.form.get("workspace_id")
        or session.get("active_workspace_id") or "default"
    )[:128]


def ok(**payload):
    return jsonify({"ok": True, **payload})


def api_errors(function: Callable):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, KeyError, TypeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except PermissionError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 403
        except ConnectionError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    return wrapped


def require_record(collection: str, record_id: str) -> dict:
    record = db().get(collection, record_id)
    if not record:
        raise FileNotFoundError(f"{collection} 记录不存在：{record_id}")
    return record


def require_workspace_record(collection: str, record_id: str, expected_workspace_id: str | None = None) -> dict:
    expected = expected_workspace_id or workspace_id()
    record = db().get(collection, record_id, workspace_id=expected)
    if not record:
        # 不向请求方暴露其他工作空间中的记录是否存在。
        raise FileNotFoundError(f"{collection} 记录不存在：{record_id}")
    return record


def current_user_id() -> str:
    return str(session.get("user_id") or "local-default")


def require_source_access(
    source_id: str, expected_workspace_id: str | None = None, *, action: str = "read",
) -> dict:
    return require_source_policy(
        db(), source_id, workspace_id=expected_workspace_id or workspace_id(),
        actor_id=current_user_id(), action=action,
    )


def require_query_result_access(
    result_id: str, expected_workspace_id: str | None = None, *, action: str = "read",
) -> dict:
    wid = expected_workspace_id or workspace_id()
    result = db().get("query_results", result_id, workspace_id=wid)
    return require_result_policy(
        db(), result, workspace_id=wid, actor_id=current_user_id(), action=action,
    )


def require_job_access(job_id: str, expected_workspace_id: str | None = None) -> dict:
    wid = expected_workspace_id or workspace_id()
    job = db().get("jobs", job_id, workspace_id=wid)
    return require_job_policy(
        db(), job, workspace_id=wid, actor_id=current_user_id(),
    )


def require_session_access(
    session_id: str, expected_workspace_id: str | None = None,
) -> dict:
    wid = expected_workspace_id or workspace_id()
    return require_session_policy(
        db(), session_id, workspace_id=wid, actor_id=current_user_id(),
    )


def require_system_owner() -> dict:
    users = db().list("users", include_archived=True)
    if not users and current_user_id() == "local-default":
        return {"id": "local-default", "role": "owner"}
    user = db().get("users", current_user_id())
    if not user or not user.get("enabled", True) or user.get("role") != "owner":
        raise PermissionError("该操作仅限系统所有者")
    return user


def workspace_membership(wid: str, user_id: str | None = None) -> dict | None:
    user_id = user_id or current_user_id()
    if user_id == "local-default" and not db().list("users", include_archived=True):
        return {"workspace_id": wid, "user_id": user_id, "role": "owner"}
    return next(
        (
            member for member in db().list("workspace_members", workspace_id=wid)
            if member.get("user_id") == user_id and member.get("enabled", True)
        ),
        None,
    )


def require_workspace_access(wid: str, *, write: bool = False, owner: bool = False) -> dict:
    workspace = require_record("workspaces", wid)
    membership = workspace_membership(wid)
    if not membership:
        raise PermissionError("无权访问该工作空间")
    role = membership.get("role", "viewer")
    if owner and role != "owner":
        raise PermissionError("该操作仅限工作空间所有者")
    if write and role not in {"owner", "editor"}:
        raise PermissionError("当前成员只有只读权限")
    return workspace


def safe_child(base: Path, candidate: Path) -> Path:
    base = base.resolve()
    candidate = candidate.resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("路径超出允许范围")
    return candidate
