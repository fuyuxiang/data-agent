from __future__ import annotations

import hmac

from flask import Blueprint, current_app, jsonify, request

from ..services.feishu_bot import (
    bot_connector,
    connector_secret,
    dispatch_inbound_event,
    list_joined_chats,
    public_status,
    start_long_connection,
)
from ..services.security import SecretVault
from .common import api_errors, body, db, ok, require_workspace_record, workspace_id


bp = Blueprint("feishu_bot", __name__)


def _validate_app_id(value: object) -> str:
    app_id = str(value or "").strip()
    if not app_id or len(app_id) > 128 or any(character.isspace() for character in app_id):
        raise ValueError("请填写有效的飞书 App ID")
    return app_id


@bp.get("/api/feishu-bot")
@api_errors
def get_feishu_bot():
    return ok(connection=public_status(db(), workspace_id()))


@bp.put("/api/feishu-bot")
@api_errors
def put_feishu_bot():
    payload, wid = body(), workspace_id()
    current = bot_connector(db(), wid)
    old_secret = connector_secret(current) if current else {}
    enabled = payload.get("enabled", current.get("enabled", False) if current else False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值")
    transport = str(payload.get("inbound_transport") or (current or {}).get("inbound_transport") or "long_connection")
    if transport not in {"long_connection", "webhook"}:
        raise ValueError("入站方式仅支持 long_connection 或 webhook")
    receive_type = str(payload.get("receive_id_type") or old_secret.get("receive_id_type") or "chat_id")
    if receive_type not in {"chat_id", "open_id"}:
        raise ValueError("接收对象类型仅支持 chat_id 或 open_id")
    app_id = _validate_app_id(payload.get("app_id", old_secret.get("app_id")))
    app_secret = payload.get("app_secret")
    if app_secret is None:
        app_secret = old_secret.get("app_secret", "")
    app_secret = str(app_secret or "").strip()
    if not app_secret:
        raise ValueError("飞书 App Secret 不能为空")
    verification = payload.get("event_verification_token", payload.get("verification_token"))
    if verification is None:
        verification = old_secret.get("verification_token", "")
    secret = {
        **old_secret, "app_id": app_id, "app_secret": app_secret,
        "verification_token": str(verification or "").strip(),
        "receive_id_type": receive_type,
        "receive_id": str(payload.get("receive_id", old_secret.get("receive_id", "")) or "").strip(),
    }
    item = db().put(
        "connectors",
        {
            **(current or {}), "id": (current or {}).get("id") or db().new_id("conn"),
            "workspace_id": wid, "name": str(payload.get("name") or (current or {}).get("name") or "飞书应用机器人")[:100],
            "type": "lark_app", "purpose": "feishu_bot", "enabled": enabled,
            "status": "configured", "inbound_transport": transport,
            "credential": SecretVault(current_app.config["VAULT_KEY"]).seal(secret),
        },
        workspace_id=wid,
    )
    if enabled and transport == "long_connection":
        start_long_connection(current_app._get_current_object(), item)
    return ok(connection=public_status(db(), wid))


@bp.get("/api/feishu-bot/chats")
@api_errors
def get_feishu_chats():
    connector = bot_connector(db(), workspace_id())
    if not connector:
        raise ValueError("请先配置飞书应用机器人")
    return ok(chats=list_joined_chats(connector))


@bp.post("/api/feishu-bot/test")
@api_errors
def test_feishu_bot():
    connector = bot_connector(db(), workspace_id())
    if not connector or not connector.get("enabled", True):
        raise ValueError("请先启用飞书应用机器人")
    from .integration import _send_connector

    result = _send_connector(connector, "智析 Agent 已成功连接飞书应用机器人。")
    return ok(message="测试消息已发送到飞书。", result=result)


def _conversation_payload(session: dict) -> dict:
    status = public_status(db(), session["workspace_id"])
    return {
        "configured": status["configured"], "application_enabled": status["enabled"],
        "connected": bool(session.get("feishu_bot_enabled") and session.get("feishu_chat_id")),
        "chat_id": str(session.get("feishu_chat_id") or ""),
        "chat_name": str(session.get("feishu_chat_name") or ""),
        "connector_id": str(session.get("feishu_connector_id") or ""),
    }


@bp.get("/api/session/<session_id>/feishu-bot")
@api_errors
def get_session_feishu_bot(session_id: str):
    return ok(**_conversation_payload(require_workspace_record("sessions", session_id)))


@bp.put("/api/session/<session_id>/feishu-bot")
@api_errors
def put_session_feishu_bot(session_id: str):
    session = require_workspace_record("sessions", session_id)
    payload = body()
    if not isinstance(payload.get("enabled"), bool):
        raise ValueError("enabled 必须是布尔值")
    if not payload["enabled"]:
        session.update({"feishu_bot_enabled": False, "feishu_chat_id": "", "feishu_chat_name": "", "feishu_connector_id": ""})
        session = db().put("sessions", session, workspace_id=session["workspace_id"])
        return ok(**_conversation_payload(session))
    connector = bot_connector(db(), session["workspace_id"])
    if not connector or not connector.get("enabled", True):
        raise ValueError("请先在设置中启用飞书应用机器人")
    chat_id = str(payload.get("chat_id") or connector_secret(connector).get("receive_id") or "").strip()
    chats = list_joined_chats(connector)
    selected = next((item for item in chats if item["chat_id"] == chat_id), None)
    if not selected:
        raise ValueError("目标群不在当前机器人可见范围内")
    session.update({
        "feishu_bot_enabled": True, "feishu_chat_id": selected["chat_id"],
        "feishu_chat_name": selected["name"][:120], "feishu_connector_id": connector["id"],
    })
    session = db().put("sessions", session, workspace_id=session["workspace_id"])
    return ok(**_conversation_payload(session))


@bp.get("/api/session/<session_id>/feishu-bot/events")
@api_errors
def get_session_feishu_events(session_id: str):
    session = require_workspace_record("sessions", session_id)
    after = max(0, int(request.args.get("after", "0")))
    events = sorted(
        [
            item for item in db().list("feishu_inbound_events", workspace_id=session["workspace_id"], limit=5000)
            if item.get("session_id") == session_id and int(item.get("revision") or 0) > after
        ],
        key=lambda item: int(item.get("revision") or 0),
    )
    revision = max([int(item.get("revision") or 0) for item in events] or [after])
    return ok(revision=revision, events=events)


def _event_connector(payload: dict) -> dict | None:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    supplied = str(header.get("token") or payload.get("token") or "")
    if not supplied:
        return None
    for connector in db().list("connectors", limit=5000):
        if connector.get("type") != "lark_app" or not connector.get("enabled", True):
            continue
        expected = str(connector_secret(connector).get("verification_token") or "")
        if expected and hmac.compare_digest(expected, supplied):
            return connector
    return None


@bp.post("/api/feishu-bot/events")
def receive_feishu_event():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"code": 400, "message": "invalid JSON"}), 400
    connector = _event_connector(payload)
    if not connector:
        return jsonify({"code": 401, "message": "invalid event token"}), 401
    if payload.get("type") == "url_verification":
        return jsonify({"challenge": str(payload.get("challenge") or "")})
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    if header.get("event_type") != "im.message.receive_v1":
        return ok(accepted=False)
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    accepted = dispatch_inbound_event(
        current_app._get_current_object(), connector, event, str(header.get("event_id") or ""),
    )
    return ok(accepted=accepted)
