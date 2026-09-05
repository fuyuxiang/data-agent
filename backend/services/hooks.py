from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
from typing import Any

from flask import current_app

from ..core.database import Database, utcnow
from .security import safe_http_request


SUPPORTED_EVENTS = {
    "startup",
    "session_start",
    "session_end",
    "user_prompt_submit",
    "turn_start",
    "turn_end",
    "tool_call",
    "pre_tool_use",
    "post_tool_use",
    "permission_request",
    "subagent_start",
    "subagent_stop",
    "pre_compact",
    "post_compact",
    "stop",
    "error",
    # Meridian 的异步编排事件；与对话生命周期事件使用同一引擎。
    "analysis.completed",
    "job.queued",
    "job.started",
    "job.completed",
    "job.failed",
    "job.cancelled",
    "workflow.started",
    "workflow.step_started",
    "workflow.step_completed",
    "workflow.waiting_approval",
    "workflow.completed",
    "workflow.failed",
    "workflow.cancelled",
}

_ALIASES = {
    "sessionstart": "session_start",
    "sessionend": "session_end",
    "userpromptsubmit": "user_prompt_submit",
    "turnstart": "turn_start",
    "turnend": "turn_end",
    "toolcall": "tool_call",
    "pretooluse": "pre_tool_use",
    "posttooluse": "post_tool_use",
    "permissionrequest": "permission_request",
    "subagentstart": "subagent_start",
    "subagentstop": "subagent_stop",
    "precompact": "pre_compact",
    "postcompact": "post_compact",
}
_hook_lock = threading.RLock()


def normalize_event_name(value: str) -> str:
    raw = str(value or "").strip().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(raw.replace("_", "").lower(), raw.lower())


def _nested_get(source: Any, path: str) -> Any:
    value = source
    aliases = {"event": "event_name", "tool": "tool_name", "args": "tool_args", "ok": "tool_ok"}
    parts = str(path or "").split(".")
    if parts:
        parts[0] = aliases.get(parts[0], parts[0])
    for part in filter(None, parts):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return str(value).strip().lower() not in {"", "0", "false", "no", "none", "null"}


def _compare(left: Any, operator: str, right: Any) -> bool:
    if operator == "exists":
        return _truthy(left)
    if operator in {"gt", "gte", "lt", "lte", ">", ">=", "<", "<="}:
        try:
            left_number, right_number = float(left), float(right)
        except (TypeError, ValueError):
            return False
        return {
            "gt": left_number > right_number,
            ">": left_number > right_number,
            "gte": left_number >= right_number,
            ">=": left_number >= right_number,
            "lt": left_number < right_number,
            "<": left_number < right_number,
            "lte": left_number <= right_number,
            "<=": left_number <= right_number,
        }[operator]
    if isinstance(left, bool) and str(right).lower() in {"true", "false"}:
        right = str(right).lower() == "true"
    if operator in {"equals", "=="}:
        return left == right or str(left) == str(right)
    if operator in {"not_equals", "!="}:
        return not _compare(left, "equals", right)
    left_text, right_text = str(left or "").lower(), str(right or "").lower()
    if operator == "contains":
        return right_text in left_text
    if operator == "not_contains":
        return right_text not in left_text
    if operator == "startswith":
        return left_text.startswith(right_text)
    if operator == "endswith":
        return left_text.endswith(right_text)
    return False


def _eval_clause(clause: str, payload: dict) -> bool:
    try:
        tokens = shlex.split(clause)
    except ValueError:
        return False
    if not tokens:
        return True
    if len(tokens) == 1:
        return _truthy(_nested_get(payload, tokens[0]))
    operator = tokens[1]
    if operator not in {
        "==", "!=", "contains", "not_contains", "startswith", "endswith", "exists",
        ">", ">=", "<", "<=",
    }:
        return False
    return _compare(_nested_get(payload, tokens[0]), operator, " ".join(tokens[2:]))


def _condition_matches(condition: dict | str | None, payload: dict) -> bool:
    if not condition:
        return True
    if isinstance(condition, str):
        return any(
            all(_eval_clause(clause.strip(), payload) for clause in re.split(r"\s+&&\s+", branch) if clause.strip())
            for branch in re.split(r"\s+\|\|\s+", condition)
            if branch.strip()
        )
    field = str(condition.get("field") or "")
    return _compare(
        _nested_get(payload, field),
        str(condition.get("operator") or "equals"),
        condition.get("value"),
    )


def _expand(value: Any, payload: dict) -> Any:
    if isinstance(value, dict):
        return {key: _expand(item, payload) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, payload) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        aliases = {
            "event": "event_name", "tool": "tool_name", "message": "message",
            "final_answer": "final_answer", "error": "error", "tool_error": "tool_error",
            "session_id": "session_id", "turn_id": "turn_id", "workspace_id": "workspace_id",
            "model": "model", "model_provider": "model_provider", "elapsed_seconds": "elapsed_seconds",
        }
        resolved = _nested_get(payload, aliases.get(name, name))
        if isinstance(resolved, (dict, list)):
            return json.dumps(resolved, ensure_ascii=False)
        return "" if resolved is None else str(resolved)

    return re.sub(r"\$([A-Z][A-Z0-9_]*(?:\.[A-Za-z0-9_]+)*)", replace, value)


def _once_key(hook: dict, payload: dict) -> str:
    if not hook.get("once"):
        return ""
    scope = str(hook.get("once_scope") or "session")
    if scope == "turn":
        identity = payload.get("turn_id") or payload.get("session_id") or "anonymous"
    elif scope == "session":
        identity = payload.get("session_id") or "anonymous"
    else:
        scope, identity = "global", "global"
    return f"{scope}:{identity}"


def _execute_action(action: dict, payload: dict, workspace_id: str, database: Database) -> dict:
    action_type = str(action.get("type") or "noop").lower()
    result: dict[str, Any] = {"type": action_type, "status": "completed", "output": ""}
    if action_type == "prompt":
        result["output"] = str(_expand(action.get("message") or "", payload))
        result["prompt"] = result["output"]
    elif action_type in {"http", "webhook"}:
        url = str(_expand(action.get("url") or "", payload))
        if not url.startswith(("http://", "https://")):
            raise ValueError("HTTP Hook 仅支持 http:// 或 https:// URL")
        method = str(action.get("method") or "POST").upper()
        body = _expand(action.get("body", payload), payload)
        data = None if method in {"GET", "HEAD"} else body
        headers = {"Content-Type": "application/json", **_expand(action.get("headers") or {}, payload)}
        response = safe_http_request(
            method, url, json=data, headers=headers,
            timeout=max(1, min(int(action.get("timeout", 10)), 60)),
        )
        result["http_status"] = response.status_code
        result["output"] = response.text[:4096]
        if not 200 <= response.status_code < 300:
            result["status"] = "failed"
    elif action_type == "command":
        if not current_app.config.get("ALLOW_COMMAND_HOOKS", False):
            raise PermissionError("命令 Hook 默认关闭；管理员可显式启用 ALLOW_COMMAND_HOOKS")
        arguments = shlex.split(str(_expand(action.get("command") or "", payload)))
        if not arguments:
            raise ValueError("Hook 命令不能为空")
        completed = subprocess.run(
            arguments, shell=False, capture_output=True, text=True,
            timeout=max(1, min(int(action.get("timeout", 10)), 60)), check=False,
        )
        result["output"] = (completed.stdout or completed.stderr or "")[:4000]
        result["returncode"] = completed.returncode
        if completed.returncode:
            result["status"] = "failed"
    elif action_type == "workflow":
        from .workflows import start_workflow

        workflow = database.get("workflows", str(action.get("workflow_id") or ""))
        if not workflow or workflow.get("workspace_id", "default") != workspace_id:
            raise ValueError("Hook 引用的工作流不存在或不属于当前工作空间")
        child_payload = {**payload, "hook_depth": int(payload.get("hook_depth", 0)) + 1}
        result["run"] = start_workflow(
            {**workflow, "definition": workflow.get("published_definition") or workflow["definition"]},
            child_payload,
        )
    elif action_type == "connector":
        from ..api.integration import _send_connector

        connector = database.get("connectors", str(action.get("connector_id") or ""))
        if not connector or connector.get("workspace_id", "default") != workspace_id:
            raise ValueError("Hook 引用的连接器不存在或不属于当前工作空间")
        result["delivery"] = _send_connector(
            connector,
            str(_expand(action.get("message") or payload.get("message") or payload["event_name"], payload)),
            payload,
        )
    elif action_type in {"noop", "reject"}:
        result["output"] = str(_expand(action.get("message") or "", payload))
    else:
        raise ValueError(f"不支持的 Hook 动作：{action_type}")
    return result


def dispatch_hooks(
    event: str,
    payload: dict,
    workspace_id: str,
    *,
    database: Database | None = None,
) -> list[dict]:
    """Run matching hooks synchronously and persist every attempted trigger."""
    database = database or current_app.extensions["meridian_db"]
    event = normalize_event_name(event)
    context = {**payload, "event_name": event, "workspace_id": workspace_id}
    if int(context.get("hook_depth", 0)) > 8:
        return []
    matched: list[dict] = []
    with _hook_lock:
        hooks = database.list("hooks", workspace_id=workspace_id)
        for hook in hooks:
            if not hook.get("enabled", True) or normalize_event_name(hook.get("event", "")) != event:
                continue
            if not _condition_matches(hook.get("condition") or hook.get("if"), context):
                continue
            reservation = _once_key(hook, context)
            execution_keys = list(hook.get("execution_keys") or [])
            if reservation and reservation in execution_keys:
                continue
            action = hook.get("action") if isinstance(hook.get("action"), dict) else {}
            try:
                result = _execute_action(action, context, workspace_id, database)
            except Exception as exc:
                result = {
                    "type": str(action.get("type") or "noop"),
                    "status": "failed", "output": str(exc), "error": str(exc),
                }
            rejected = bool(hook.get("reject") or action.get("type") == "reject")
            item = {
                "hook_id": hook["id"], "hook_name": hook.get("name") or hook["id"],
                "event": event, "ok": result.get("status") != "failed", "rejected": rejected,
                "output": str(result.get("output") or "")[:4000], "prompt": result.get("prompt"),
                "result": result,
            }
            hook["run_count"] = int(hook.get("run_count", 0)) + 1
            hook["last_run_at"] = utcnow()
            hook["last_result"] = result
            if reservation:
                hook["execution_keys"] = [*execution_keys[-999:], reservation]
            database.put("hooks", hook, workspace_id=workspace_id)
            database.put(
                "hook_runs",
                {
                    "id": database.new_id("hookrun"), "workspace_id": workspace_id,
                    "hook_id": hook["id"], "hook_name": item["hook_name"], "event": event,
                    "ok": item["ok"], "rejected": rejected, "output": item["output"],
                    "context": context, "result": result,
                },
                workspace_id=workspace_id,
            )
            database.audit(
                "hook.executed", workspace_id=workspace_id, object_type="hook", object_id=hook["id"],
                detail={"event": event, "ok": item["ok"], "rejected": rejected, "result": result},
            )
            matched.append(item)
    return matched
