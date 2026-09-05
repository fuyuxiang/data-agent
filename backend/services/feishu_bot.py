from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Mapping
from typing import Any

from flask import Flask, current_app

from ..core.database import Database
from .feishu import request_feishu
from .security import SecretVault, mask_secret


log = logging.getLogger(__name__)
_TURN_LOCKS: dict[str, threading.RLock] = {}
_TURN_LOCKS_GUARD = threading.RLock()


def connector_secret(connector: dict) -> dict:
    return SecretVault(current_app.config["VAULT_KEY"]).open(connector.get("credential", ""), {})


def bot_connector(database: Database, workspace_id: str) -> dict | None:
    connectors = database.list("connectors", workspace_id=workspace_id, limit=5000)
    return next(
        (item for item in connectors if item.get("type") == "lark_app" and item.get("purpose") == "feishu_bot"),
        next((item for item in connectors if item.get("type") == "lark_app"), None),
    )


def public_status(database: Database, workspace_id: str) -> dict[str, Any]:
    connector = bot_connector(database, workspace_id)
    if not connector:
        return {
            "enabled": False, "configured": False, "app_id": "", "app_id_masked": "",
            "app_secret_configured": False, "event_verification_token_configured": False,
            "inbound_transport": "long_connection", "receive_id_type": "chat_id",
            "receive_id": "", "receive_id_masked": "", "updated_at": "",
            "long_connection_status": "idle", "connector_id": "",
        }
    secret = connector_secret(connector)
    transport = str(connector.get("inbound_transport") or "long_connection")
    result = {
        "enabled": bool(connector.get("enabled", True)),
        "configured": bool(secret.get("app_id") and secret.get("app_secret")),
        "app_id": str(secret.get("app_id") or ""), "app_id_masked": mask_secret(str(secret.get("app_id") or "")),
        "app_secret_configured": bool(secret.get("app_secret")),
        "event_verification_token_configured": bool(secret.get("verification_token")),
        "inbound_transport": transport, "receive_id_type": str(secret.get("receive_id_type") or "chat_id"),
        "receive_id": str(secret.get("receive_id") or ""),
        "receive_id_masked": mask_secret(str(secret.get("receive_id") or "")),
        "updated_at": connector.get("updated_at", ""), "connector_id": connector["id"],
    }
    result.update(long_connection_status(workspace_id))
    return result


def list_joined_chats(connector: dict) -> list[dict[str, str]]:
    secret = connector_secret(connector)
    chats, page_token = [], ""
    for _ in range(3):
        params: dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = request_feishu("GET", "/im/v1/chats", secret, params=params)
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("chat_id"):
                chats.append({"chat_id": str(item["chat_id"]), "name": str(item.get("name") or "未命名群")})
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            break
    return chats


def event_prompt(event: Mapping[str, Any]) -> tuple[str, str] | None:
    sender = event.get("sender") if isinstance(event.get("sender"), Mapping) else {}
    message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
    if sender.get("sender_type") != "user" or message.get("chat_type") != "group":
        return None
    if not message.get("mentions") or message.get("message_type") != "text":
        return None
    try:
        content = json.loads(str(message.get("content") or "{}"))
    except (TypeError, ValueError):
        return None
    text = str(content.get("text") or "").strip() if isinstance(content, dict) else ""
    text = re.sub(r"@_user_\d+\s*", "", text).strip()
    chat_id = str(message.get("chat_id") or "").strip()
    return (chat_id, text) if chat_id and text else None


def _session_lock(session_id: str) -> threading.RLock:
    with _TURN_LOCKS_GUARD:
        return _TURN_LOCKS.setdefault(session_id, threading.RLock())


def record_inbound_event(database: Database, session: dict, role: str, content: str) -> dict:
    rows = [
        item for item in database.list("feishu_inbound_events", workspace_id=session["workspace_id"], limit=5000)
        if item.get("session_id") == session["id"]
    ]
    revision = max([int(item.get("revision") or 0) for item in rows] or [0]) + 1
    return database.put(
        "feishu_inbound_events",
        {
            "id": database.new_id("fsevt"), "workspace_id": session["workspace_id"],
            "session_id": session["id"], "revision": revision, "role": role,
            "content": str(content or "")[:100_000],
        },
        workspace_id=session["workspace_id"],
    )


def sync_web_turn(database: Database, session: dict, user_message: str, assistant_message: str) -> None:
    if not session.get("feishu_bot_enabled") or not session.get("feishu_chat_id"):
        return
    connector = database.get("connectors", str(session.get("feishu_connector_id") or ""))
    if not connector or connector.get("workspace_id") != session.get("workspace_id"):
        raise ValueError("会话绑定的飞书连接器不存在")
    from ..api.integration import _send_connector

    _send_connector(connector, "🧑 Web 对话\n" + str(user_message or "")[:3900])
    answer = str(assistant_message or "")
    prefix = "🤖 智析 Agent\n"
    limit = 4000 - len(prefix)
    while answer:
        _send_connector(connector, prefix + answer[:limit])
        answer = answer[limit:]


def run_inbound_turn(app: Flask, session_id: str, connector_id: str, question: str) -> None:
    try:
        with _session_lock(session_id), app.app_context(), app.test_request_context("/feishu-internal"):
            database: Database = app.extensions["meridian_db"]
            session = database.get("sessions", session_id)
            connector = database.get("connectors", connector_id)
            if not session or not connector:
                return
            from .agent_runtime import run_conversation

            before = len(database.messages(session_id, 5000))
            for _event in run_conversation(
                session_id=session_id, workspace_id=session["workspace_id"], question=question,
                source_ids=[str(value) for value in session.get("source_ids") or []],
                provider_id=session.get("provider_id"), should_cancel=lambda: False,
            ):
                pass
            new_messages = database.messages(session_id, 5000)[before:]
            assistant = next((item for item in reversed(new_messages) if item.get("role") == "assistant"), None)
            if not assistant:
                return
            record_inbound_event(database, session, "user", question)
            record_inbound_event(database, session, "assistant", assistant["content"])
            from ..api.integration import _send_connector

            _send_connector(connector, "🤖 智析 Agent\n" + str(assistant["content"]))
    except Exception as exc:
        log.warning("Feishu inbound turn failed for session %s: %s", session_id, type(exc).__name__)


def dispatch_inbound_event(app: Flask, connector: dict, event: Mapping[str, Any], event_id: str = "") -> bool:
    parsed = event_prompt(event)
    if parsed is None:
        return False
    database: Database = app.extensions["meridian_db"]
    if event_id:
        receipt_id = "fs_" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:40]
        _receipt, created = database.put_if_absent(
            "feishu_event_receipts",
            {"id": receipt_id, "workspace_id": connector["workspace_id"], "event_id_hash": receipt_id},
            workspace_id=connector["workspace_id"],
        )
        if not created:
            return False
    chat_id, question = parsed
    session = next(
        (
            item for item in database.list("sessions", workspace_id=connector["workspace_id"], limit=5000)
            if item.get("feishu_bot_enabled") and item.get("feishu_chat_id") == chat_id
            and item.get("feishu_connector_id") == connector["id"]
        ),
        None,
    )
    if not session:
        return False
    threading.Thread(
        target=run_inbound_turn, args=(app, session["id"], connector["id"], question),
        daemon=True, name="feishu-inbound-turn",
    ).start()
    return True


_LONG_CONNECTIONS: dict[str, dict[str, Any]] = {}
_LONG_LOCK = threading.RLock()


def long_connection_status(workspace_id: str) -> dict[str, str]:
    with _LONG_LOCK:
        state = _LONG_CONNECTIONS.get(workspace_id, {})
        return {"long_connection_status": str(state.get("status") or "idle"),
                "long_connection_error": str(state.get("error") or "")}


def start_long_connection(app: Flask, connector: dict) -> bool:
    if not connector.get("enabled", True) or connector.get("inbound_transport") != "long_connection":
        return False
    with app.app_context():
        secret = connector_secret(connector)
    if not secret.get("app_id") or not secret.get("app_secret"):
        return False
    wid = connector["workspace_id"]
    fingerprint = hashlib.sha256((secret["app_id"] + connector["credential"]).encode()).hexdigest()
    with _LONG_LOCK:
        state = _LONG_CONNECTIONS.get(wid)
        if state and state.get("thread") and state["thread"].is_alive() and state.get("fingerprint") == fingerprint:
            return True
        _LONG_CONNECTIONS[wid] = {"status": "starting", "error": "", "fingerprint": fingerprint}

    def run() -> None:
        try:
            import lark_oapi as lark

            def on_message(data):
                raw = json.loads(lark.JSON.marshal(data))
                header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
                event = raw.get("event") if isinstance(raw.get("event"), dict) else raw
                dispatch_inbound_event(app, connector, event, str(header.get("event_id") or ""))
                return {}

            handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
            with _LONG_LOCK:
                _LONG_CONNECTIONS[wid].update({"status": "connected", "error": ""})
            lark.ws.Client(
                secret["app_id"], secret["app_secret"], event_handler=handler, log_level=lark.LogLevel.INFO,
            ).start()
        except Exception as exc:
            with _LONG_LOCK:
                _LONG_CONNECTIONS[wid].update({"status": "error", "error": type(exc).__name__})
            log.warning("Feishu long connection stopped: %s", type(exc).__name__)

    thread = threading.Thread(target=run, daemon=True, name=f"feishu-long-{wid[:20]}")
    with _LONG_LOCK:
        _LONG_CONNECTIONS[wid]["thread"] = thread
    thread.start()
    return True
