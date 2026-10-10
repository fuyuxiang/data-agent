from __future__ import annotations

import pytest

from backend.api import integration


@pytest.mark.parametrize("channel", ["teams", "wecom"])
def test_webhook_channel_survives_custom_name_reload_and_restore(app, client, monkeypatch, channel):
    monkeypatch.setattr(integration, "validate_outbound_url", lambda value: value)
    created = client.post("/api/connectors", json={
        "name": "销售分析群", "type": "webhook", "channel": channel,
        "url": "https://example.com/hook",
    })
    assert created.status_code == 201
    item = created.get_json()["item"]
    assert item["channel"] == channel
    assert "credential" not in item
    loaded = next(row for row in client.get("/api/connectors").get_json()["items"] if row["id"] == item["id"])
    assert loaded["name"] == "销售分析群"
    assert loaded["channel"] == channel

    assert client.delete(f"/api/connectors/{item['id']}").status_code == 200
    assert client.post(f"/api/trash/connectors/{item['id']}/restore").status_code == 200
    restored = next(row for row in client.get("/api/connectors").get_json()["items"] if row["id"] == item["id"])
    assert restored["channel"] == channel
    assert client.delete(f"/api/connectors/{item['id']}").status_code == 200
    assert client.delete(f"/api/trash/connectors/{item['id']}", json={"confirm": False}).status_code == 400
    assert client.delete(f"/api/trash/connectors/{item['id']}", json={"confirm": True}).status_code == 200
    assert client.post(f"/api/trash/connectors/{item['id']}/restore").status_code == 404


@pytest.mark.parametrize("channel,connector_type", [("teams", "lark"), ("wecom", "email"), ("unknown", "webhook")])
def test_connector_channel_cannot_claim_a_different_protocol(client, channel, connector_type):
    result = client.post("/api/connectors", json={
        "name": "用途校验", "type": connector_type, "channel": channel,
        "url": "https://example.com/hook", "host": "smtp.example.com", "recipient": "review@example.com",
    })
    assert result.status_code == 400
    assert result.get_json()["error"] == "连接用途与连接器类型不匹配"
    assert not client.get("/api/connectors").get_json()["items"]


def test_legacy_generic_webhook_stays_generic(client, monkeypatch):
    monkeypatch.setattr(integration, "validate_outbound_url", lambda value: value)
    response = client.post("/api/connectors", json={
        "name": "旧 Teams 通知", "type": "webhook", "url": "https://example.com/hook",
    })
    assert response.status_code == 201
    item = response.get_json()["item"]
    assert item["channel"] == ""
    reloaded = client.get("/api/connectors").get_json()["items"][0]
    assert reloaded["channel"] == ""


@pytest.mark.parametrize("preset,days", [("7", 30), ("14", 30), ("forever", 30), ("custom", 30), ("custom", 90), ("custom", 180), ("custom", 45)])
def test_retention_wire_values_round_trip_through_the_real_put_route(client, preset, days):
    settings = {"retention_preset": preset, "retention_custom_days": days}
    response = client.put("/api/lifecycle/settings", json=settings)
    assert response.status_code == 200
    assert response.get_json()["settings"] == settings
    assert client.get("/api/lifecycle/settings").get_json()["settings"] == settings


@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_mcp_trash_lists_restores_and_requires_confirmation_to_permanently_delete(client, monkeypatch, transport):
    monkeypatch.setattr(integration, "validate_outbound_url", lambda value: value)
    created = client.post("/api/mcp/servers", json={
        "name": "可恢复 MCP", "transport": transport, "url": "https://example.com/mcp",
        "command": "python3", "args": ["server.py"],
    })
    assert created.status_code == 201
    item = created.get_json()["item"]
    assert client.delete(f"/api/mcp/servers/{item['id']}").status_code == 200
    listed = client.get("/api/trash?collection=mcp_servers").get_json()["items"]
    assert len(listed) == 1
    assert listed[0]["id"] == item["id"]
    assert listed[0]["can_restore"] is True
    assert not {"credential", "command", "args"}.intersection(listed[0])
    assert client.post(f"/api/trash/mcp_servers/{item['id']}/restore").status_code == 200
    restored = next(row for row in client.get("/api/mcp/servers").get_json()["items"] if row["id"] == item["id"])
    assert restored["transport"] == transport
    assert restored["args"] == ["server.py"]
    assert client.delete(f"/api/mcp/servers/{item['id']}").status_code == 200
    assert client.delete(f"/api/trash/mcp_servers/{item['id']}", json={"confirm": False}).status_code == 400
    assert client.delete(f"/api/trash/mcp_servers/{item['id']}", json={"confirm": True}).status_code == 200
    assert client.post(f"/api/trash/mcp_servers/{item['id']}/restore").status_code == 404


def test_mcp_permanent_delete_preserves_draft_and_published_dependencies(app, client, monkeypatch):
    monkeypatch.setattr(integration, "validate_outbound_url", lambda value: value)
    created = client.post("/api/mcp/servers", json={"name": "被引用的 MCP", "transport": "http", "url": "https://example.com/mcp"})
    item = created.get_json()["item"]
    assert client.delete(f"/api/mcp/servers/{item['id']}").status_code == 200
    database = app.extensions["meridian_db"]
    database.put("agent_definitions", {
        "id": "legacy-dependent-agent", "name": "引用保留", "workspace_id": "default",
        "mcp_server_ids": [item["id"]], "status": "draft",
    }, workspace_id="default")
    denied = client.delete(f"/api/trash/mcp_servers/{item['id']}", json={"confirm": True})
    assert denied.status_code == 400
    assert "智能体草稿或发布版本引用" in denied.get_json()["error"]
    assert database.get("mcp_servers", item["id"], workspace_id="default", include_archived=True)


def test_integration_trash_retains_workspace_owner_permissions_and_stdio_system_owner_gate(app, monkeypatch):
    monkeypatch.setattr(integration, "validate_outbound_url", lambda value: value)
    owner = app.test_client()
    assert owner.post("/api/auth/register", json={"email": "trash-owner@example.com", "password": "correct-horse", "name": "Owner"}).status_code == 201
    wid = owner.post("/api/workspaces", json={"name": "连接恢复权限"}).get_json()["item"]["id"]
    headers = {"X-Workspace-Id": wid}
    connector = owner.post("/api/connectors", headers=headers, json={
        "name": "权限连接", "type": "webhook", "channel": "teams", "url": "https://example.com/hook",
    }).get_json()["item"]
    servers = []
    for transport in ("http", "stdio"):
        response = owner.post("/api/mcp/servers", headers=headers, json={
            "name": f"权限 MCP {transport}", "transport": transport, "url": "https://example.com/mcp", "command": "python3",
        })
        assert response.status_code == 201
        servers.append(response.get_json()["item"])
    records = [("connectors", connector), *(('mcp_servers', server) for server in servers)]
    for collection, item in records:
        endpoint = f"/api/{'connectors' if collection == 'connectors' else 'mcp/servers'}/{item['id']}"
        assert owner.delete(endpoint, headers=headers).status_code == 200

    for role in ("viewer", "analyst", "editor", "owner"):
        member = app.test_client()
        email = f"trash-member-{role}@example.com"
        assert member.post("/api/auth/register", json={"email": email, "password": "correct-horse", "name": role}).status_code == 201
        assert owner.post(f"/api/workspaces/{wid}/members", headers=headers, json={"email": email, "role": role}).status_code == 201
        assert member.post(f"/api/workspaces/{wid}/activate").status_code == 200
        listed = member.get("/api/trash").get_json()["items"]
        if role != "owner":
            assert not listed
            for collection, item in records:
                assert member.post(f"/api/trash/{collection}/{item['id']}/restore").status_code in {403, 404}
                assert member.delete(f"/api/trash/{collection}/{item['id']}", json={"confirm": True}).status_code == 403
        else:
            assert len(listed) == 3
            for collection, item in records:
                listed_item = next(row for row in listed if row["id"] == item["id"])
                if item.get("transport") == "stdio":
                    assert listed_item["can_restore"] is False
                    assert "系统所有者" in listed_item["restore_block_reason"]
                    assert member.post(f"/api/trash/{collection}/{item['id']}/restore").status_code == 403
                else:
                    assert listed_item["can_restore"] is True
                    assert member.post(f"/api/trash/{collection}/{item['id']}/restore").status_code == 200
