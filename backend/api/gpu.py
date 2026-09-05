from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify

from ..services.models import save_provider
from ..services.remote_gpu import (
    TunnelError,
    connection_manager,
    connection_url,
    create_connection,
    detect_all,
    inspect_host_key,
    list_connections,
    models_at,
    set_enabled,
    test_model_at,
    trust_host_key,
)
from ..services.security import SecretVault
from .common import body, db, require_workspace_record, workspace_id


log = logging.getLogger(__name__)
bp = Blueprint("gpu", __name__)


def _error(message: str, status: int):
    return jsonify({"ok": False, "message": message, "error": message}), status


@bp.get("/api/gpu/status")
def gpu_status():
    return jsonify(detect_all(db(), workspace_id()))


@bp.post("/api/gpu/enabled")
def gpu_enabled():
    enabled = body().get("enabled")
    if not isinstance(enabled, bool):
        return _error("enabled 必须是布尔值", 400)
    return jsonify({"ok": True, "enabled": set_enabled(db(), workspace_id(), enabled)})


@bp.get("/api/gpu/connections")
def gpu_connections():
    return jsonify({"ok": True, "connections": list_connections(db(), workspace_id())})


@bp.post("/api/gpu/connections")
def gpu_connection_create():
    try:
        item = create_connection(db(), workspace_id(), body())
        return jsonify({"ok": True, "connection": item}), 201
    except (ValueError, TypeError) as exc:
        return _error(str(exc), 400)


def _connection(connection_id: str) -> dict | None:
    try:
        return require_workspace_record("gpu_connections", connection_id)
    except FileNotFoundError:
        return None


@bp.delete("/api/gpu/connections/<connection_id>")
def gpu_connection_delete(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    connection_manager.close(connection_id)
    db().archive("ssh_host_keys", f"hostkey_{connection_id}")
    db().archive("gpu_connections", connection_id)
    return jsonify({"ok": True})


@bp.post("/api/gpu/connections/<connection_id>/host-key")
def gpu_connection_host_key(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    if connection.get("connection_type") == "direct":
        return _error("公网直连不使用 SSH 主机指纹", 409)
    try:
        return jsonify({"ok": True, "host_key": inspect_host_key(connection)})
    except (TunnelError, ValueError) as exc:
        return _error(str(exc), 400)


@bp.post("/api/gpu/connections/<connection_id>/trust-host-key")
def gpu_connection_trust_host_key(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    if connection.get("connection_type") == "direct":
        return _error("公网直连不使用 SSH 主机指纹", 409)
    payload = body()
    try:
        trusted = trust_host_key(
            db(), connection, str(payload.get("key_type") or ""), str(payload.get("key_base64") or ""),
        )
        return jsonify({"ok": True, "fingerprint": trusted["fingerprint"]})
    except (TunnelError, ValueError) as exc:
        return _error(str(exc), 400)


@bp.post("/api/gpu/connections/<connection_id>/connect")
def gpu_connection_connect(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    try:
        if connection.get("connection_type") == "direct":
            models_at(connection)
            return jsonify({"ok": True, "connected": True, "local_url": connection["base_url"]})
        secret = SecretVault(current_app.config["SECRET_KEY"]).open(
            connection.get("credential", ""), {},
        )
        trusted = db().get("ssh_host_keys", f"hostkey_{connection_id}")
        if trusted and trusted.get("workspace_id") != connection["workspace_id"]:
            trusted = None
        tunnel = connection_manager.connect(connection, trusted, (secret or {}).get("password"))
        return jsonify({"ok": True, "connected": True, "local_url": tunnel.local_url})
    except (TunnelError, ValueError) as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        log.info("GPU direct connection failed: %s", type(exc).__name__)
        return _error("连接失败；请检查公网端点与 /v1/models 服务", 502)


@bp.post("/api/gpu/connections/<connection_id>/disconnect")
def gpu_connection_disconnect(connection_id: str):
    if not _connection(connection_id):
        return _error("连接不存在", 404)
    connection_manager.close(connection_id)
    return jsonify({"ok": True, "connected": False})


@bp.get("/api/gpu/connections/<connection_id>/status")
def gpu_connection_status(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    if connection.get("connection_type") == "direct":
        try:
            models_at(connection)
            return jsonify({"ok": True, "connected": True, "local_url": connection["base_url"]})
        except Exception:
            return jsonify({"ok": True, "connected": False})
    return jsonify({"ok": True, **connection_manager.status(connection_id)})


@bp.post("/api/gpu/connections/<connection_id>/training/preflight")
def gpu_training_preflight(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    if connection.get("connection_type") != "ssh":
        return _error("远程训练器预检仅支持 SSH 连接", 409)
    try:
        status = connection_manager.training_preflight(connection_id)
        connection["training_runner"] = status
        db().put("gpu_connections", connection, workspace_id=connection["workspace_id"])
        return jsonify({"ok": True, "training_runner": status})
    except TunnelError as exc:
        return _error(str(exc), 409)


@bp.get("/api/gpu/connections/<connection_id>/models")
def gpu_connection_models(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    if connection.get("connection_type") == "ssh" and not connection_manager.status(connection_id).get("connected"):
        return _error("连接尚未建立", 409)
    try:
        return jsonify({"ok": True, "models": models_at(connection)})
    except Exception as exc:
        log.info("GPU model discovery failed: %s", type(exc).__name__)
        return _error("远端模型列表不可用；请确认服务提供 OpenAI /v1/models", 502)


@bp.post("/api/gpu/connections/<connection_id>/models/register")
def gpu_model_register(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    base_url = connection_url(connection)
    if not base_url:
        return _error("连接尚未建立", 409)
    model = str(body().get("model") or "").strip()
    if not model:
        return _error("model 不能为空", 400)
    item = save_provider({
        "workspace_id": connection["workspace_id"], "name": f"{connection['name']} · {model}",
        "base_url": base_url.rstrip("/") + "/v1", "model": model, "api_key": "no-key",
    })
    return jsonify({"ok": True, "message": "远端模型已注册", "provider": item})


@bp.post("/api/gpu/connections/<connection_id>/models/test")
def gpu_model_test(connection_id: str):
    connection = _connection(connection_id)
    if not connection:
        return _error("连接不存在", 404)
    if not connection_url(connection):
        return _error("连接尚未建立", 409)
    model = str(body().get("model") or "").strip()
    if not model:
        return _error("model 不能为空", 400)
    try:
        reply = test_model_at(connection, model)
        return jsonify({"ok": True, "message": "模型推理连通性验证成功", "reply": reply})
    except Exception as exc:
        log.info("GPU model inference test failed: %s", type(exc).__name__)
        return _error("模型推理测试失败；请检查模型 ID、端点和服务日志", 502)
