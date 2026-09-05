from __future__ import annotations

import json
import hashlib
import hmac
import os
import platform
import re
import shutil
import smtplib
import subprocess
import time
from email.message import EmailMessage
from flask import Blueprint, current_app, request

from ..core.database import utcnow
from ..services.mcp import get_mcp_manager
from ..services.models import public_provider, save_provider, test_provider
from ..services.security import SecretVault, safe_http_request, validate_outbound_url
from .common import (
    api_errors, body, current_user_id, db, ok, require_system_owner,
    require_workspace_record, workspace_id,
)


bp = Blueprint("integration", __name__)


@bp.get("/api/providers")
def providers():
    wid = workspace_id()
    items = [
        item for item in db().list("providers")
        if item["id"] == "environment-default" or item.get("workspace_id", "default") == wid
    ]
    return ok(items=[public_provider(item) for item in items])


@bp.post("/api/providers")
@api_errors
def create_provider():
    return ok(item=save_provider({**body(), "workspace_id": workspace_id()})), 201


@bp.patch("/api/providers/<provider_id>")
@api_errors
def update_provider(provider_id: str):
    wid = workspace_id()
    if provider_id == "environment-default":
        user = db().get("users", current_user_id())
        if user and user.get("role") != "owner":
            raise PermissionError("只有系统所有者可修改环境模型配置")
        wid = "default"
    else:
        require_workspace_record("providers", provider_id)
    return ok(item=save_provider({**body(), "workspace_id": wid}, provider_id))


@bp.delete("/api/providers/<provider_id>")
@api_errors
def delete_provider(provider_id: str):
    if provider_id == "environment-default":
        raise ValueError("环境变量模型配置不能删除")
    require_workspace_record("providers", provider_id)
    if not db().archive("providers", provider_id):
        raise FileNotFoundError("模型配置不存在")
    return ok(archived=True)


@bp.post("/api/providers/<provider_id>/test")
@api_errors
def provider_test(provider_id: str):
    if provider_id != "environment-default":
        require_workspace_record("providers", provider_id)
    return ok(result=test_provider(provider_id, workspace_id()))


def _public_mcp(item: dict) -> dict:
    value = dict(item)
    value.pop("credential", None)
    value["has_headers"] = bool(item.get("credential"))
    return value


@bp.get("/api/mcp/servers")
def mcp_servers():
    items = []
    manager = get_mcp_manager()
    for item in db().list("mcp_servers", workspace_id=workspace_id()):
        public = _public_mcp(item)
        public["server_id"] = item["id"]
        public["label"] = item.get("name", "")
        public.update(manager.status(item["id"]))
        items.append(public)
    return ok(
        items=items, servers=items, bundled_resources_available=False,
        bundled_message="内置 MCP 资源需通过服务配置",
    )


@bp.post("/api/mcp/servers")
@api_errors
def create_mcp_server():
    payload = body()
    transport = str(payload.get("transport") or "streamable-http")
    if transport not in {"http", "streamable-http", "sse", "stdio"}:
        raise ValueError("不支持的 MCP 传输类型")
    if transport != "stdio" and not str(payload.get("url") or "").startswith(("http://", "https://")):
        raise ValueError("HTTP MCP 服务需要有效 URL")
    if transport == "stdio" and not payload.get("command"):
        raise ValueError("stdio MCP 服务需要 command")
    if transport == "stdio":
        require_system_owner()
        if not current_app.config.get("TESTING") and os.getenv("MERIDIAN_ENABLE_STDIO_MCP", "0") != "1":
            raise PermissionError("stdio MCP 未在服务端启用")
    if transport != "stdio":
        payload["url"] = validate_outbound_url(str(payload["url"]))
    wid = workspace_id()
    secrets = {"headers": payload.get("headers", {}), "env": payload.get("env", {})}
    credential = SecretVault(current_app.config["VAULT_KEY"]).seal(secrets) if any(secrets.values()) else ""
    requested_id = str(payload.get("server_id") or "").strip()
    if requested_id and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", requested_id):
        raise ValueError("server_id 只能包含字母、数字、点、下划线和连字符")
    item = db().put(
        "mcp_servers",
        {
            "id": requested_id or db().new_id("mcp"), "workspace_id": wid,
            "name": str(payload.get("label") or payload.get("name") or requested_id or "工具服务")[:100],
            "transport": transport, "url": str(payload.get("url") or ""),
            "command": str(payload.get("command") or ""), "args": payload.get("args", []),
            "credential": credential, "enabled": bool(payload.get("enabled", True)),
            "status": "configured", "tools": [],
        },
        workspace_id=wid,
    )
    public = _public_mcp(item)
    return ok(item=public, server_id=item["id"], message="MCP 服务已添加"), 201


@bp.patch("/api/mcp/servers/<server_id>")
@api_errors
def update_mcp_server(server_id: str):
    server = require_workspace_record("mcp_servers", server_id)
    payload = body()
    resulting_transport = str(payload.get("transport") or server.get("transport") or "streamable-http")
    if resulting_transport == "stdio":
        require_system_owner()
    headers = payload.pop("headers", None)
    environment = payload.pop("env", None)
    if payload.get("url"):
        payload["url"] = validate_outbound_url(str(payload["url"]))
    if headers is not None or environment is not None:
        current_secret = SecretVault(current_app.config["VAULT_KEY"]).open(server.get("credential", ""), {})
        payload["credential"] = SecretVault(current_app.config["VAULT_KEY"]).seal({
            "headers": headers if headers is not None else current_secret.get("headers", {}),
            "env": environment if environment is not None else current_secret.get("env", {}),
        })
    server.update({key: value for key, value in payload.items() if key in {"name", "url", "transport", "command", "args", "enabled", "credential"}})
    item = db().put("mcp_servers", server, workspace_id=server["workspace_id"])
    get_mcp_manager().remove_server(server_id)
    return ok(item=_public_mcp(item))


@bp.delete("/api/mcp/servers/<server_id>")
@api_errors
def delete_mcp_server(server_id: str):
    require_workspace_record("mcp_servers", server_id)
    get_mcp_manager().remove_server(server_id)
    if not db().archive("mcp_servers", server_id):
        raise FileNotFoundError("MCP 服务不存在")
    return ok(archived=True)


@bp.post("/api/mcp/servers/<server_id>/test")
@api_errors
def test_mcp(server_id: str):
    server = require_workspace_record("mcp_servers", server_id)
    if server.get("transport") == "stdio":
        require_system_owner()
    started = time.perf_counter()
    status = get_mcp_manager().connect_server(server)
    if status["status"] != "connected":
        raise ConnectionError(status.get("last_error") or "MCP 连接失败")
    refreshed = db().get("mcp_servers", server_id) or server
    return ok(result={
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "initialize": status.get("server_info", {}), "tools": refreshed.get("tools", []), "status": status,
    })


@bp.get("/api/mcp/servers/<server_id>/tools")
@api_errors
def mcp_tools(server_id: str):
    server = require_workspace_record("mcp_servers", server_id)
    items = server.get("tools", [])
    return ok(items=items, server_id=server_id, tools=items)


@bp.post("/api/mcp/servers/<server_id>/tools/<tool_name>/call")
@api_errors
def call_mcp_tool(server_id: str, tool_name: str):
    server = require_workspace_record("mcp_servers", server_id)
    if server.get("transport") == "stdio":
        require_system_owner()
    if not server.get("enabled", True):
        raise PermissionError("MCP 服务已禁用")
    arguments = body().get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("MCP 工具参数必须是对象")
    result = get_mcp_manager().call_tool(server, tool_name, arguments)
    db().audit("mcp.tool_called", workspace_id=server["workspace_id"], object_type="mcp_server", object_id=server_id, detail={"tool": tool_name})
    return ok(result=result)


def _public_connector(item: dict) -> dict:
    value = dict(item)
    value.pop("credential", None)
    value["configured"] = bool(item.get("credential") or item.get("url"))
    return value


@bp.get("/api/connectors")
def connectors():
    return ok(items=[_public_connector(item) for item in db().list("connectors", workspace_id=workspace_id())])


@bp.post("/api/connectors")
@api_errors
def create_connector():
    payload = body()
    connector_type = str(payload.get("type") or "webhook")
    if connector_type not in {"webhook", "lark", "lark_app", "dingtalk", "slack", "email"}:
        raise ValueError("连接器类型不受支持")
    url = str(payload.get("url") or "")
    if connector_type not in {"email", "lark_app"} and not url.startswith(("http://", "https://")):
        raise ValueError("通知连接器需要有效 Webhook URL")
    if connector_type not in {"email", "lark_app"}:
        url = validate_outbound_url(url)
    if connector_type == "email" and (not payload.get("host") or not payload.get("recipient")):
        raise ValueError("邮件连接器需要 SMTP 主机和收件人")
    if connector_type == "lark_app" and any(
        not payload.get(key) for key in ("app_id", "app_secret", "receive_id")
    ):
        raise ValueError("飞书应用连接需要 app_id、app_secret 和 receive_id")
    wid = workspace_id()
    credential = SecretVault(current_app.config["VAULT_KEY"]).seal({
        "url": url, "token": payload.get("token", ""),
        "host": payload.get("host", ""), "port": int(payload.get("port", 587)),
        "username": payload.get("username", ""), "password": payload.get("password", ""),
        "sender": payload.get("sender", ""), "recipient": payload.get("recipient", ""),
        "use_tls": bool(payload.get("use_tls", True)),
        "app_id": payload.get("app_id", ""), "app_secret": payload.get("app_secret", ""),
        "receive_id": payload.get("receive_id", ""),
        "receive_id_type": payload.get("receive_id_type", "chat_id"),
        "verification_token": payload.get("verification_token", ""),
    })
    item = db().put(
        "connectors",
        {
            "id": db().new_id("conn"), "workspace_id": wid, "name": str(payload.get("name") or connector_type)[:100],
            "type": connector_type, "credential": credential, "enabled": bool(payload.get("enabled", True)), "status": "configured",
        },
        workspace_id=wid,
    )
    return ok(item=_public_connector(item)), 201


def _send_connector(connector: dict, message: str, extra: dict | None = None) -> dict:
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(connector.get("credential", ""), {})
    connector_type = connector.get("type")
    if connector_type == "email":
        mail = EmailMessage()
        mail["Subject"] = str((extra or {}).get("subject") or "经纬分析结果")[:160]
        mail["From"] = secret.get("sender") or secret.get("username")
        mail["To"] = secret["recipient"]
        mail.set_content(message)
        with smtplib.SMTP(secret["host"], int(secret.get("port", 587)), timeout=20) as smtp:
            if secret.get("use_tls", True):
                smtp.starttls()
            if secret.get("username"):
                smtp.login(secret["username"], secret.get("password", ""))
            smtp.send_message(mail)
        return {"status": 250, "recipient": secret["recipient"]}
    if connector_type == "lark_app":
        from ..services.feishu import API_ROOT, tenant_token

        receive_type = str(secret.get("receive_id_type") or "chat_id")
        if receive_type not in {"chat_id", "open_id", "user_id", "union_id", "email"}:
            raise ValueError("飞书 receive_id_type 无效")
        response = safe_http_request(
            "POST", f"{API_ROOT}/im/v1/messages", params={"receive_id_type": receive_type},
            headers={"Authorization": f"Bearer {tenant_token(secret)}"},
            json={
                "receive_id": secret["receive_id"], "msg_type": "text",
                "content": json.dumps({"text": message}, ensure_ascii=False),
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (None, 0):
            raise ConnectionError(payload.get("msg") or "飞书应用消息发送失败")
        return {"status": response.status_code, "message_id": (payload.get("data") or {}).get("message_id")}
    if connector_type == "lark":
        payload = {"msg_type": "text", "content": {"text": message}}
    elif connector_type == "dingtalk":
        payload = {"msgtype": "text", "text": {"content": message}}
    elif connector_type == "slack":
        payload = {"text": message}
    else:
        payload = {"message": message, "metadata": extra or {}}
    response = safe_http_request("POST", secret["url"], json=payload, timeout=15)
    response.raise_for_status()
    return {"status": response.status_code, "body": response.text[:1000]}


@bp.post("/api/connectors/<connector_id>/test")
@api_errors
def test_connector(connector_id: str):
    connector = require_workspace_record("connectors", connector_id)
    return ok(result=_send_connector(connector, "经纬分析工作台连接测试成功"))


@bp.post("/api/connectors/<connector_id>/send")
@api_errors
def send_connector(connector_id: str):
    connector = require_workspace_record("connectors", connector_id)
    message = str(body().get("message") or "").strip()
    if not message:
        raise ValueError("消息不能为空")
    return ok(result=_send_connector(connector, message, body().get("metadata")))


@bp.delete("/api/connectors/<connector_id>")
@api_errors
def delete_connector(connector_id: str):
    require_workspace_record("connectors", connector_id)
    if not db().archive("connectors", connector_id):
        raise FileNotFoundError("连接器不存在")
    return ok(archived=True)


@bp.post("/api/integrations/events")
@api_errors
def receive_integration_event():
    payload = body()
    wid = str(payload.get("workspace_id") or "default")[:128]
    if not db().get("workspaces", wid):
        raise FileNotFoundError("工作空间不存在")
    token = str(payload.get("token") or request.headers.get("X-Integration-Token") or "")
    integration = db().get("integration_credentials", f"integration_{wid}")
    expected = ""
    if integration and integration.get("workspace_id") == wid:
        expected = str(
            SecretVault(current_app.config["VAULT_KEY"])
            .open(integration.get("credential", ""), {}).get("token") or ""
        )
    if not expected and (
        current_app.config.get("TESTING") or os.getenv("MERIDIAN_ALLOW_GLOBAL_INTEGRATION_TOKEN", "0") == "1"
    ):
        expected = os.getenv("MERIDIAN_INTEGRATION_TOKEN", "")
    if not expected:
        return {"ok": False, "error": "未配置入站集成令牌"}, 503
    if not token or not hmac.compare_digest(token, expected):
        return ok(challenge=payload.get("challenge")) if payload.get("challenge") else ({"ok": False, "error": "invalid token"}, 401)
    if payload.get("challenge"):
        return {"challenge": payload["challenge"]}
    sanitized = {key: value for key, value in payload.items() if key != "token"}
    external_id = str(payload.get("event_id") or "")[:256]
    event_id = (
        "evt_" + hashlib.sha256(f"{wid}\0{external_id}".encode()).hexdigest()[:24]
        if external_id else db().new_id("evt")
    )
    event, created = db().put_if_absent(
        "integration_events",
        {
            "id": event_id, "workspace_id": wid,
            "source": str(payload.get("source") or "external")[:100],
            "external_id": external_id, "payload": sanitized, "status": "received",
        },
        workspace_id=wid,
    )
    return ok(event_id=event["id"], duplicate=not created), 202


def _local_compute() -> dict:
    cpu = os.cpu_count() or 1
    memory = None
    try:
        import resource

        memory = resource.getrlimit(resource.RLIMIT_AS)[0]
    except Exception:
        pass
    gpu = {"available": False, "backend": None, "devices": []}
    if shutil.which("nvidia-smi"):
        try:
            output = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()
            gpu = {"available": bool(output), "backend": "cuda", "devices": output.splitlines()}
        except Exception:
            pass
    elif platform.system() == "Darwin" and platform.machine() == "arm64":
        gpu = {"available": True, "backend": "metal", "devices": ["Apple Silicon"]}
    return {"cpu_count": cpu, "platform": platform.platform(), "python": platform.python_version(), "memory_limit": memory, "gpu": gpu}


@bp.get("/api/compute/status")
def compute_status():
    return ok(local=_local_compute(), nodes=[_public_compute(item) for item in db().list("compute_nodes", workspace_id=workspace_id())])


def _public_compute(item: dict) -> dict:
    value = dict(item)
    value.pop("credential", None)
    value["has_credential"] = bool(item.get("credential"))
    return value


@bp.post("/api/compute/nodes")
@api_errors
def create_compute_node():
    payload = body()
    if not payload.get("host") or not payload.get("username"):
        raise ValueError("远程计算节点需要 host 和 username")
    validate_outbound_url(f"https://{payload['host']}:{int(payload.get('port', 22))}")
    wid = workspace_id()
    credential = SecretVault(current_app.config["VAULT_KEY"]).seal({"password": payload.get("password", ""), "private_key": payload.get("private_key", "")})
    item = db().put(
        "compute_nodes",
        {
            "id": db().new_id("node"), "workspace_id": wid, "name": str(payload.get("name") or payload["host"])[:100],
            "host": str(payload["host"]), "port": int(payload.get("port", 22)), "username": str(payload["username"]),
            "credential": credential, "status": "configured", "enabled": True,
        },
        workspace_id=wid,
    )
    return ok(item=_public_compute(item)), 201


@bp.post("/api/compute/nodes/<node_id>/test")
@api_errors
def test_compute_node(node_id: str):
    node = require_workspace_record("compute_nodes", node_id)
    import paramiko

    secret = SecretVault(current_app.config["VAULT_KEY"]).open(node.get("credential", ""), {})
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    kwargs = {"hostname": node["host"], "port": node["port"], "username": node["username"], "timeout": 10, "look_for_keys": True}
    if secret.get("password"):
        kwargs["password"] = secret["password"]
    client.connect(**kwargs)
    _, stdout, _ = client.exec_command("python3 --version && uname -srm", timeout=10)
    output = stdout.read().decode("utf-8", errors="replace").strip()
    client.close()
    node.update({"status": "online", "last_tested_at": utcnow(), "system": output})
    db().put("compute_nodes", node, workspace_id=node["workspace_id"])
    return ok(result={"status": "online", "system": output})


@bp.delete("/api/compute/nodes/<node_id>")
@api_errors
def delete_compute_node(node_id: str):
    require_workspace_record("compute_nodes", node_id)
    if not db().archive("compute_nodes", node_id):
        raise FileNotFoundError("计算节点不存在")
    return ok(archived=True)
