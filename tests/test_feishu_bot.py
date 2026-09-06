from __future__ import annotations

import json

from backend.services.feishu_bot import record_inbound_event, sync_web_turn


def _configure(client):
    response = client.put(
        "/api/feishu-bot",
        json={
            "enabled": True,
            "app_id": "cli_test_application",
            "app_secret": "super-secret",
            "event_verification_token": "verify-me",
            "inbound_transport": "webhook",
            "receive_id_type": "chat_id",
            "receive_id": "oc_group_1",
        },
    )
    assert response.status_code == 200
    return response.get_json()["connection"]


def test_bot_configuration_masks_secrets_and_binds_visible_group(client, monkeypatch):
    status = _configure(client)
    assert status["configured"] is True
    assert status["app_secret_configured"] is True
    assert "super-secret" not in json.dumps(status)

    monkeypatch.setattr(
        "backend.api.feishu_bot.list_joined_chats",
        lambda _connector: [{"chat_id": "oc_group_1", "name": "销售经营群"}],
    )
    chats = client.get("/api/feishu-bot/chats").get_json()["chats"]
    assert chats == [{"chat_id": "oc_group_1", "name": "销售经营群"}]

    session = client.post("/api/sessions", json={"name": "飞书联动"}).get_json()["item"]
    linked = client.put(
        f"/api/sessions/{session['id']}/feishu-bot",
        json={"enabled": True, "chat_id": "oc_group_1"},
    ).get_json()
    assert linked["connected"] is True
    assert linked["chat_name"] == "销售经营群"

    unlinked = client.put(
        f"/api/sessions/{session['id']}/feishu-bot", json={"enabled": False},
    ).get_json()
    assert unlinked["connected"] is False


def test_webhook_challenge_mentions_and_event_deduplication(client, monkeypatch):
    _configure(client)
    monkeypatch.setattr(
        "backend.api.feishu_bot.list_joined_chats",
        lambda _connector: [{"chat_id": "oc_group_1", "name": "管理驾驶舱"}],
    )
    session = client.post("/api/sessions", json={"name": "飞书入站"}).get_json()["item"]
    assert client.put(
        f"/api/sessions/{session['id']}/feishu-bot",
        json={"enabled": True, "chat_id": "oc_group_1"},
    ).status_code == 200

    challenge = client.post(
        "/api/feishu-bot/events",
        json={"type": "url_verification", "token": "verify-me", "challenge": "challenge-123"},
    )
    assert challenge.status_code == 200
    assert challenge.get_json() == {"challenge": "challenge-123"}
    assert client.post(
        "/api/feishu-bot/events",
        json={"type": "url_verification", "token": "wrong", "challenge": "leak"},
    ).status_code == 401

    started = []

    class FakeThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            started.append(self.args)

    monkeypatch.setattr("backend.services.feishu_bot.threading.Thread", FakeThread)
    payload = {
        "header": {
            "token": "verify-me", "event_type": "im.message.receive_v1",
            "event_id": "evt-once",
        },
        "event": {
            "sender": {"sender_type": "user"},
            "message": {
                "chat_type": "group", "chat_id": "oc_group_1", "message_type": "text",
                "mentions": [{"key": "@_user_1"}],
                "content": json.dumps({"text": "@_user_1 分析本月销售变化"}, ensure_ascii=False),
            },
        },
    }
    first = client.post("/api/feishu-bot/events", json=payload).get_json()
    second = client.post("/api/feishu-bot/events", json=payload).get_json()
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert len(started) == 1
    assert started[0][-1] == "分析本月销售变化"

    ordinary = json.loads(json.dumps(payload))
    ordinary["header"]["event_id"] = "evt-no-mention"
    ordinary["event"]["message"]["mentions"] = []
    assert client.post("/api/feishu-bot/events", json=ordinary).get_json()["accepted"] is False


def test_incremental_events_and_web_turn_mirroring(app, client, monkeypatch):
    _configure(client)
    session = client.post("/api/sessions", json={"name": "双向同步"}).get_json()["item"]
    sent = []
    monkeypatch.setattr(
        "backend.api.integration._send_connector",
        lambda connector, message, extra=None: sent.append((connector["id"], message)) or {"status": 200},
    )
    with app.app_context():
        database = app.extensions["meridian_db"]
        connector = next(item for item in database.list("connectors") if item["type"] == "lark_app")
        session.update({
            "feishu_bot_enabled": True, "feishu_chat_id": "oc_group_1",
            "feishu_connector_id": connector["id"],
        })
        session = database.put("sessions", session)
        first = record_inbound_event(database, session, "user", "群内问题")
        second = record_inbound_event(database, session, "assistant", "群内答案")
        sync_web_turn(database, session, "Web 问题", "A" * 8100)

    events = client.get(
        f"/api/sessions/{session['id']}/feishu-bot/events?after={first['revision']}"
    ).get_json()
    assert events["revision"] == second["revision"]
    assert [item["content"] for item in events["events"]] == ["群内答案"]
    assert sent[0][1].startswith("🧑 Web 对话")
    assert len(sent) == 4  # 用户消息 + 3 个受飞书长度限制的回答分片
    assert all(message.startswith("🤖 智析 Agent") for _, message in sent[1:])
