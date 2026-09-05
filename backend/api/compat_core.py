from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import smtplib
import threading
import time
from email.message import EmailMessage
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_from_directory

from ..services.agent_tools import BUILTIN_TOOLS, EXTRA_TOOLS
from ..services.hooks import SUPPORTED_EVENTS, normalize_event_name
from ..services.mcp import ALLOWED_STDIO_COMMANDS, get_mcp_manager
from ..services.models import public_provider, save_provider, test_provider
from ..services.security import SecretVault, validate_outbound_url
from .common import api_errors, body, current_user_id, db, require_workspace_record, workspace_id


bp = Blueprint("compat_core", __name__)


@bp.get("/login")
def login_page():
    return send_from_directory(current_app.config["SETTINGS"].frontend_dir, "index.html")


def _public_memory(record: dict) -> dict:
    return {
        "name": record.get("name", ""), "type": record.get("type", ""),
        "scope": record.get("scope", ""), "title": record.get("title", ""),
        "body": record.get("body", record.get("content", "")),
        "why": record.get("why", ""), "how_to_apply": record.get("how_to_apply", ""),
        "created_at": record.get("created_at", ""), "updated_at": record.get("updated_at", ""),
    }


def _memory_by_name(name: str, wid: str) -> dict | None:
    user_id = current_user_id()
    return next((
        item for item in db().list("memories", workspace_id=wid)
        if item.get("name") == name and (not item.get("user_id") or item.get("user_id") == user_id)
    ), None)


@bp.get("/api/memory")
def list_memory():
    wid = workspace_id()
    user_id = current_user_id()
    records = [
        _public_memory(item) for item in db().list("memories", workspace_id=wid)
        if not item.get("user_id") or item.get("user_id") == user_id
    ]
    return jsonify({"records": records, "workspace_mounted": bool((db().get("workspaces", wid) or {}).get("mounted_path"))})


@bp.get("/api/memory-activity")
def memory_activity():
    sid = str(request.args.get("session_id") or "")
    items = [
        item for item in db().list("memory_extractions", workspace_id=workspace_id(), limit=500)
        if not sid or item.get("session_id") == sid
    ]
    return jsonify({"activity": items})


@bp.get("/api/memory/<name>")
@api_errors
def get_memory(name: str):
    record = _memory_by_name(name, workspace_id())
    if not record:
        raise FileNotFoundError("记忆不存在或不属于当前作用域")
    return jsonify({"record": _public_memory(record)})


@bp.post("/api/memory")
@api_errors
def create_memory():
    payload, wid = body(), workspace_id()
    name = str(payload.get("name") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not name or not title:
        raise ValueError("记忆 name 和 title 不能为空")
    if _memory_by_name(name, wid):
        raise ValueError("同名记忆已存在")
    memory_type = str(payload.get("type") or "project")
    scope = str(payload.get("scope") or "workspace")
    content = str(payload.get("body") or payload.get("content") or "")[:12000]
    item = db().put(
        "memories",
        {
            "id": db().new_id("mem"), "workspace_id": wid, "name": name[:100],
            "type": memory_type, "scope": scope, "title": title[:120],
            "body": content, "content": content, "why": str(payload.get("why") or "")[:1000],
            "how_to_apply": str(payload.get("how_to_apply") or "")[:2000],
            "user_id": current_user_id() if scope == "user" or memory_type in {"user", "feedback"} else "",
            "enabled": True,
        },
        workspace_id=wid,
    )
    return jsonify({"record": _public_memory(item)}), 201


@bp.put("/api/memory/<name>")
@api_errors
def update_memory(name: str):
    record = _memory_by_name(name, workspace_id())
    if not record:
        raise FileNotFoundError("记忆不存在或不属于当前作用域")
    payload = body()
    changes = {key: payload[key] for key in ("type", "title", "body", "why", "how_to_apply") if key in payload}
    if "body" in changes:
        changes["content"] = changes["body"]
    item = db().patch("memories", record["id"], changes)
    return jsonify({"record": _public_memory(item or record)})


@bp.delete("/api/memory/<name>")
@api_errors
def delete_memory(name: str):
    if body().get("confirm") is not True:
        raise ValueError("归档需要确认")
    record = _memory_by_name(name, workspace_id())
    if not record:
        raise FileNotFoundError("记忆不存在或不属于当前作用域")
    db().archive("memories", record["id"])
    return jsonify({"ok": True})


@bp.put("/api/skills/<path:name>")
@api_errors
def put_skill(name: str):
    from ..services.skills import get_skill, public_skill

    current = get_skill(name, workspace_id())
    if current and current.get("builtin"):
        raise PermissionError("不能修改内置 Skill")
    if not current:
        raise FileNotFoundError("Skill not found")
    payload = body()
    new_name = str(payload.get("name") or current.get("name") or name).strip()
    description = str(payload.get("description") or "").strip()
    prompt = str(payload.get("prompt") or payload.get("instruction") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", new_name):
        raise ValueError("Invalid name format")
    if not description or not prompt:
        raise ValueError("Description and prompt required")
    updated = db().patch(
        "skills", current["id"],
        {
            "name": new_name, "slug": new_name, "description": description[:240],
            "instruction": prompt[:50000], "allowed_tools": payload.get("allowed_tools", []),
            "icon": str(payload.get("icon") or "custom"),
        },
    )
    return jsonify({"ok": True, "name": new_name, "skill": public_skill(updated or current, include_prompt=True)})


@bp.get("/api/skills/tools")
def skill_tools():
    tools = sorted(
        ({"name": item["function"]["name"], "cat": item["function"].get("category", "agent")} for item in [*BUILTIN_TOOLS, *EXTRA_TOOLS]),
        key=lambda item: item["name"],
    )
    return jsonify({"ok": True, "tools": tools})


def _hook_payload() -> dict:
    items = db().list("hooks", workspace_id=workspace_id())
    configured = [{
        **item,
        "action_type": (item.get("action") or {}).get("type", ""),
        "event_dispatched": item.get("event") in SUPPORTED_EVENTS,
    } for item in items]
    enabled = [item for item in configured if item.get("enabled", True)]
    return {
        "ok": True,
        "settings": {"enabled": True, "hooks": configured},
        "runtime": {
            "enabled": True, "enabled_count": len(enabled), "runnable_count": len(enabled),
            "pending_count": 0, "configured_count": len(configured), "active_hooks": enabled,
            "configured_hooks": configured, "internal_endpoints": [],
        },
    }


@bp.delete("/api/hooks/history")
def clear_hook_history():
    cleared = 0
    for item in db().list("hook_runs", workspace_id=workspace_id(), limit=5000):
        cleared += int(db().archive("hook_runs", item["id"]))
    return jsonify({"ok": True, "cleared": cleared})


@bp.put("/api/hooks")
@api_errors
def put_hooks():
    raw, wid = body(), workspace_id()
    hooks = raw.get("hooks")
    if not isinstance(hooks, list):
        raise ValueError("hooks 必须是数组")
    normalized = []
    for index, item in enumerate(hooks):
        if not isinstance(item, dict):
            raise ValueError(f"hooks[{index}] 必须是对象")
        event = normalize_event_name(str(item.get("event") or ""))
        action = item.get("action") or ({"type": item.get("action_type"), "message": item.get("prompt", "")})
        if event not in SUPPORTED_EVENTS or not isinstance(action, dict) or not action.get("type"):
            raise ValueError(f"hooks[{index}] 的 event/action 无效")
        normalized.append({
            **item, "id": str(item.get("id") or db().new_id("hook")),
            "workspace_id": wid, "event": event, "action": action,
            "enabled": bool(item.get("enabled", True)), "execution_keys": item.get("execution_keys", []),
        })
    for item in db().list("hooks", workspace_id=wid, limit=5000):
        db().archive("hooks", item["id"])
    for item in normalized:
        db().put("hooks", item, workspace_id=wid)
    return jsonify(_hook_payload())


@bp.post("/api/hooks/test")
@api_errors
def test_hooks_compat():
    from ..services.hooks import _condition_matches, _expand

    raw = body()
    event = normalize_event_name(str(raw.get("event") or "turn_start"))
    context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    context = {**context, "event_name": event}
    settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else _hook_payload()["settings"]
    hooks = settings.get("hooks") or []
    side_effects = [
        str(item.get("id") or "") for item in hooks
        if (item.get("action") or {}).get("type") in {"http", "webhook", "command", "workflow", "connector"}
    ]
    if side_effects:
        return jsonify({
            "ok": False, "error": "测试运行仅支持 prompt Hook；会产生副作用的动作不会执行。",
            "hook_ids": side_effects,
        }), 400
    prompts = []
    for item in hooks:
        if not item.get("enabled", True) or normalize_event_name(item.get("event", "")) != event:
            continue
        if not _condition_matches(item.get("condition"), context):
            continue
        action = item.get("action") or {}
        prompts.append(str(_expand(action.get("message") or item.get("prompt") or "", context)))
    return jsonify({"ok": True, "rejected": False, "notifications": [], "prompt_messages": prompts})


@bp.get("/api/hooks/metadata")
def hooks_metadata():
    aliases = {
        "SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
        "PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use",
        "PermissionRequest": "permission_request", "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop", "PreCompact": "pre_compact",
        "PostCompact": "post_compact", "Stop": "stop", "turn_begin": "turn_start",
        "tool_call": "tool_call",
    }
    return jsonify({
        "ok": True, "events": sorted(SUPPORTED_EVENTS), "dispatched_events": sorted(SUPPORTED_EVENTS),
        "aliases": aliases, "accepted_event_names": sorted(set(SUPPORTED_EVENTS) | set(aliases)),
        "actions": ["prompt", "http", "command", "workflow", "connector"],
        "once_scopes": ["turn", "session", "global"],
        "variables": [
            "$EVENT", "$SESSION_ID", "$TURN_ID", "$TOOL_NAME", "$TOOL_ARGS.sql",
            "$MESSAGE", "$FINAL_ANSWER", "$ERROR", "$WORKSPACE_ID", "$WORKSPACE_PATH",
        ],
    })


def _model_dict() -> dict:
    return {
        item["id"]: public_provider(item)
        for item in db().list("providers", include_archived=False, limit=5000)
        if item["id"] == "environment-default" or item.get("workspace_id", "default") == workspace_id()
    }


@bp.get("/api/models")
def models():
    return jsonify(_model_dict())


@bp.get("/api/models/defaults")
def model_defaults():
    return jsonify({
        "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini", "context_window": 1047576, "max_output_tokens": 32768},
        "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "context_window": 65536, "max_output_tokens": 8192},
        "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "context_window": 131072, "max_output_tokens": 8192},
    })


def _provider_payload(payload: dict) -> dict:
    return {
        "name": payload.get("name") or payload.get("provider") or "模型服务",
        "base_url": payload.get("base_url"), "model": payload.get("model") or payload.get("model_name"),
        "api_key": payload.get("api_key"), "enabled": payload.get("enabled", True),
        "temperature": payload.get("temperature", 0.2), "workspace_id": workspace_id(),
        "context_window": payload.get("context_window"), "max_output_tokens": payload.get("max_output_tokens"),
        "enable_thinking": payload.get("enable_thinking", False), "thinking_budget": payload.get("thinking_budget", 8000),
        "input_price_per_million": payload.get("input_price_per_million"),
        "output_price_per_million": payload.get("output_price_per_million"),
    }


@bp.post("/api/models/set-builtin")
@api_errors
def set_builtin_model():
    payload = body()
    provider = str(payload.get("provider") or "").strip()
    if not provider:
        raise ValueError("provider 不能为空")
    save_provider(_provider_payload(payload), provider)
    return jsonify({"ok": True})


@bp.post("/api/models/clear-builtin")
@api_errors
def clear_builtin_model():
    provider = str(body().get("provider") or "").strip()
    item = require_workspace_record("providers", provider)
    item.pop("credential", None)
    item["secret_source"] = "environment"
    db().put("providers", item, workspace_id=item.get("workspace_id", "default"))
    return jsonify({"ok": True, "message": "已清除模型密钥"})


@bp.post("/api/models/add")
@api_errors
def add_model():
    payload = body()
    if not payload.get("name") or not payload.get("base_url") or not (payload.get("model_name") or payload.get("model")):
        raise ValueError("模型名称、base_url 和 model_name 不能为空")
    item = save_provider(_provider_payload(payload))
    return jsonify({"ok": True, "message": "模型已添加", "provider": item["id"]})


@bp.post("/api/models/update")
@api_errors
def update_model():
    payload = body()
    provider = str(payload.get("provider") or "").strip()
    current = require_workspace_record("providers", provider)
    item = save_provider({**current, **_provider_payload(payload)}, provider)
    return jsonify({"ok": True, "message": "模型已更新", "item": item})


@bp.post("/api/models/delete")
@api_errors
def delete_model():
    provider = str(body().get("provider") or "").strip()
    require_workspace_record("providers", provider)
    if provider == "environment-default":
        raise ValueError("环境默认模型不能删除")
    db().archive("providers", provider)
    return jsonify({"ok": True, "message": "模型已删除"})


@bp.post("/api/models/test")
@api_errors
def test_model():
    payload = body()
    provider = str(payload.get("provider") or "").strip()
    if payload.get("api_key") or payload.get("base_url") or payload.get("model"):
        # Persisting test-only input would leak surprising configuration state;
        # exercise it through an encrypted ephemeral record and remove it afterwards.
        temp_id = db().new_id("model_test")
        save_provider(_provider_payload(payload), temp_id)
        try:
            result = test_provider(temp_id, workspace_id())
        finally:
            db().delete("providers", temp_id)
    else:
        result = test_provider(provider, workspace_id())
    return jsonify(result)


@bp.post("/api/session/<sid>/model")
@api_errors
def set_session_model(sid: str):
    session = require_workspace_record("sessions", sid)
    provider = str(body().get("provider") or "").strip()
    if provider != "environment-default":
        require_workspace_record("providers", provider, session["workspace_id"])
    db().patch("sessions", sid, {"provider_id": provider})
    return jsonify({"ok": True})


def _mcp_public(server: dict) -> dict:
    result = dict(server)
    result.pop("credential", None)
    result["server_id"] = server["id"]
    result["label"] = server.get("name", "")
    result.update(get_mcp_manager().status(server["id"]))
    return result


@bp.put("/api/mcp/servers/<server_id>")
@api_errors
def put_mcp(server_id: str):
    server = require_workspace_record("mcp_servers", server_id)
    payload = body()
    transport = str(payload.get("transport") or server.get("transport") or "streamable-http")
    if transport not in {"http", "streamable-http", "sse", "stdio"}:
        raise ValueError("不支持的 MCP 传输类型")
    if transport == "stdio":
        from .common import require_system_owner

        require_system_owner()
    if transport != "stdio" and payload.get("url"):
        payload["url"] = validate_outbound_url(str(payload["url"]))
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(server.get("credential", ""), {})
    headers = payload.get("headers", secret.get("headers", {}))
    environment = payload.get("env", secret.get("env", {}))
    changes = {
        "name": str(payload.get("label") or payload.get("name") or server.get("name")),
        "transport": transport, "url": str(payload.get("url", server.get("url", ""))),
        "command": str(payload.get("command", server.get("command", ""))),
        "args": payload.get("args", server.get("args", [])),
        "enabled": bool(payload.get("enabled", server.get("enabled", True))),
        "credential": SecretVault(current_app.config["VAULT_KEY"]).seal({"headers": headers, "env": environment}),
    }
    updated = db().patch("mcp_servers", server_id, changes)
    get_mcp_manager().remove_server(server_id)
    return jsonify({"ok": True, "server": _mcp_public(updated or server)})


def _set_mcp_enabled(server_id: str, enabled: bool):
    server = require_workspace_record("mcp_servers", server_id)
    item = db().patch("mcp_servers", server_id, {"enabled": enabled}) or server
    if not enabled:
        get_mcp_manager().remove_server(server_id)
    return jsonify({"ok": True, "message": "已启用" if enabled else "已停用", "server": _mcp_public(item)})


@bp.post("/api/mcp/servers/<server_id>/enable")
@api_errors
def enable_mcp(server_id: str):
    return _set_mcp_enabled(server_id, True)


@bp.post("/api/mcp/servers/<server_id>/disable")
@api_errors
def disable_mcp(server_id: str):
    return _set_mcp_enabled(server_id, False)


@bp.post("/api/mcp/servers/<server_id>/connect")
@api_errors
def connect_mcp(server_id: str):
    server = require_workspace_record("mcp_servers", server_id)
    if server.get("transport") == "stdio":
        from .common import require_system_owner

        require_system_owner()
    if not server.get("enabled", True):
        raise ValueError("MCP 服务已停用")
    app = current_app._get_current_object()

    def connect():
        get_mcp_manager(app).connect_server(server)

    threading.Thread(target=connect, daemon=True, name=f"mcp-connect-{server_id}").start()
    return jsonify({"ok": True, "message": f"正在连接 {server_id}…"})


def _sanitize_mcp_config(config: dict) -> tuple[dict, list[str]]:
    transport = str(config.get("transport") or ("sse" if config.get("url") else "stdio")).lower()
    transport = "sse" if transport in {"http", "streamable-http"} else transport
    if transport not in {"stdio", "sse"}:
        raise ValueError("transport 必须是 stdio 或 sse")
    command = Path(str(config.get("command") or "")).name
    args = [str(value) for value in config.get("args") or []]
    if transport == "stdio" and command not in ALLOWED_STDIO_COMMANDS:
        raise ValueError(f"命令不在安全白名单中：{command}")
    if any(re.search(r"[;&|`$<>()\n]", value) for value in args):
        raise ValueError("参数包含不允许的 shell 特殊字符")
    output = {
        "transport": transport, "label": str(config.get("label") or config.get("name") or command or "MCP 服务")[:100],
        "description": str(config.get("description") or "")[:500], "command": command,
        "args": args, "env": config.get("env") if isinstance(config.get("env"), dict) else {},
        "url": str(config.get("url") or ""),
        "headers": config.get("headers") if isinstance(config.get("headers"), dict) else {},
    }
    if transport == "sse":
        output["url"] = validate_outbound_url(output["url"])
    return output, []


@bp.post("/api/mcp/parse")
@api_errors
def parse_mcp():
    text = str(body().get("text") or "").strip()
    if not text or len(text) > 8000:
        raise ValueError("text 不能为空且不得超过 8000 字符")
    parsed = None
    try:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        value = json.loads(raw)
        if isinstance(value, dict) and "mcpServers" in value:
            name, value = next(iter(value["mcpServers"].items()))
            value = {"label": name, **value}
        parsed = value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    if parsed is None:
        tokens = shlex.split(text)
        if tokens and Path(tokens[0]).name in ALLOWED_STDIO_COMMANDS:
            parsed = {"transport": "stdio", "command": tokens[0], "args": tokens[1:], "label": Path(tokens[-1]).stem}
        elif re.fullmatch(r"https?://\S+", text):
            parsed = {"transport": "sse", "url": text, "label": "MCP 服务"}
        else:
            raise ValueError("无法确定 MCP 配置，请粘贴 JSON、stdio 命令或 HTTP URL")
    config, warnings = _sanitize_mcp_config(parsed)
    return jsonify({"ok": True, "config": config, "warnings": warnings})


def _allowed_scan_root(path: Path, wid: str) -> bool:
    settings = current_app.config["SETTINGS"]
    workspace = db().get("workspaces", wid) or {}
    roots = [settings.storage_dir.resolve(), settings.root.resolve()]
    if workspace.get("mounted_path"):
        roots.append(Path(workspace["mounted_path"]).resolve())
    return any(path == root or root in path.parents for root in roots)


@bp.post("/api/mcp/scan-local")
@api_errors
def scan_local_mcp():
    path = Path(str(body().get("path") or "")).expanduser().resolve()
    if not path.is_dir() or not _allowed_scan_root(path, workspace_id()):
        raise ValueError("路径不存在或不在当前工作区范围内")
    candidates = [path / "package.json"] + list((path / "node_modules").glob("*/package.json"))
    package_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if not package_path:
        raise ValueError("未找到 package.json，请确认路径指向 MCP 包目录")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    if not any(key in dependencies for key in {"@modelcontextprotocol/sdk", "mcp", "fastmcp"}) and "mcp" not in str(package.get("name", "")).lower():
        raise ValueError("该目录不像是 MCP 服务器包")
    entry = package.get("bin") or package.get("main") or "dist/index.js"
    if isinstance(entry, dict):
        entry = next(iter(entry.values()), "")
    entry_path = (package_path.parent / str(entry)).resolve()
    config = {
        "transport": "stdio", "label": package.get("name", package_path.parent.name),
        "description": package.get("description", ""), "command": "node",
        "args": [str(entry_path)], "env": {}, "url": "", "headers": {},
    }
    return jsonify({"ok": True, "config": config, "confidence": 95 if entry_path.is_file() else 50, "warnings": [] if entry_path.is_file() else ["入口文件尚不存在"]})


@bp.post("/api/auth/send-code")
@api_errors
def send_code():
    payload = body()
    email = str(payload.get("email") or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("请输入有效邮箱")
    address = request.remote_addr or "unknown"
    rate_id = "mailrate:" + hashlib.sha256(address.encode()).hexdigest()
    rate = db().get("email_delivery_limits", rate_id) or {
        "id": rate_id, "count": 0, "window_started": time.time(),
    }
    if time.time() - float(rate.get("window_started") or 0) > 900:
        rate = {"id": rate_id, "count": 0, "window_started": time.time()}
    if int(rate.get("count") or 0) >= 10:
        return jsonify({"error": "验证码发送次数过多，请稍后再试"}), 429
    rate["count"] = int(rate.get("count") or 0) + 1
    db().put("email_delivery_limits", rate, workspace_id="default")
    existing = next((item for item in db().list("email_codes", limit=5000) if item.get("email") == email), None)
    if existing:
        from datetime import datetime, timezone

        sent = datetime.fromisoformat(existing["sent_at"])
        if (datetime.now(timezone.utc) - sent).total_seconds() < 60:
            return jsonify({"error": "发送太频繁，请 60 秒后再试"}), 429
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", username).strip()
    if not host or not sender:
        return jsonify({"error": "邮件服务未配置，请联系管理员"}), 503
    import secrets
    from datetime import datetime, timedelta, timezone

    code = f"{secrets.randbelow(1_000_000):06d}"
    record_id = existing["id"] if existing else db().new_id("mail")
    db().put(
        "email_codes",
        {
            "id": record_id, "email": email,
            "code": SecretVault(current_app.config["VAULT_KEY"]).seal({"value": code}),
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds"),
            "attempts": 0,
        },
    )
    message = EmailMessage()
    message["Subject"] = "数据分析助手验证码"
    message["From"], message["To"] = sender, email
    message.set_content(f"您的验证码是 {code}，10 分钟内有效。")
    port = int(os.getenv("SMTP_PORT", "465"))
    if os.getenv("SMTP_SSL", "1") == "1":
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    response = {"ok": True, "message": "验证码已发送至邮箱"}
    if current_app.config.get("EXPOSE_TEST_CODES"):
        response["debug_code"] = code
    return jsonify(response)
