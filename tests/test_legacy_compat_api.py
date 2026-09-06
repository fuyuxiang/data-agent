from __future__ import annotations


def test_legacy_session_chat_lifecycle_and_metrics(client):
    created = client.post("/api/session/new", json={"name": "legacy"}).get_json()
    sid = created["session_id"]

    ping = client.get(f"/api/session/{sid}/ping")
    assert ping.status_code == 200
    assert ping.get_json()["alive"] is True

    stream = client.post(f"/api/session/{sid}/chat", json={"message": "你好"})
    assert stream.status_code == 200
    text = stream.data.decode("utf-8")
    assert "event: contract" in text
    assert '"requires_confirmation": true' in text
    assert "event: done" in text

    current = client.get(f"/api/session/{sid}/load-current")
    assert current.status_code == 200
    payload = current.get_json()
    assert payload["msg_count"] == 1
    assert [item["role"] for item in payload["history"]] == ["user"]

    suggestion = client.post(f"/api/session/{sid}/prompt-suggestion", json={"lang": "zh"})
    assert suggestion.status_code == 200
    assert "suggestion" in suggestion.get_json()

    metrics = client.get(f"/api/session/{sid}/token-metrics")
    assert metrics.status_code == 200
    assert metrics.get_json()["ok"] is True

    assert client.post(f"/api/session/{sid}/stop").status_code == 200
    cleared = client.post(f"/api/session/{sid}/clear")
    assert cleared.status_code == 200
    assert client.get(f"/api/session/{sid}/ping").get_json()["msg_count"] == 0


def test_legacy_saved_session_autosave_load_rename_delete(client):
    sid = client.post("/api/session/new", json={"name": "legacy save"}).get_json()["session_id"]
    response = client.post(f"/api/session/{sid}/chat", json={"message": "保存测试"})
    assert response.status_code == 200
    assert b"event: done" in response.data

    autosave = client.post(f"/api/session/{sid}/autosave", json={"name": "自动保存测试"})
    assert autosave.status_code == 200
    autosave_file = autosave.get_json()["filename"]
    autosave_status = client.get(f"/api/session/{sid}/autosave").get_json()
    assert autosave_status["exists"] is True
    assert autosave_status["filename"] == autosave_file

    saved = client.post(f"/api/session/{sid}/save", json={"name": "手动保存测试"})
    assert saved.status_code == 200
    filename = saved.get_json()["filename"]

    listed = client.get("/api/saved-sessions").get_json()["items"]
    assert any(item["filename"] == filename and item["msg_count"] == 1 for item in listed)

    renamed = client.post(f"/api/saved-sessions/{filename}/rename", json={"name": "已重命名"})
    assert renamed.status_code == 200
    assert renamed.get_json()["name"] == "已重命名"

    target_sid = client.post("/api/session/new", json={"name": "load target"}).get_json()["session_id"]
    loaded = client.post(f"/api/session/{target_sid}/load", json={"filename": filename})
    assert loaded.status_code == 200
    assert loaded.get_json()["ok"] is True
    assert loaded.get_json()["history"][0]["content"] == "保存测试"

    deleted = client.delete(f"/api/saved-sessions/{filename}")
    assert deleted.status_code == 200
    assert deleted.get_json()["ok"] is True


def test_legacy_chart_dashboard_and_export_routes(client, source):
    query = client.post(
        "/api/query",
        json={
            "source_ids": [source["id"]],
            "sql": "SELECT region, SUM(sales) AS sales FROM data GROUP BY region",
        },
    ).get_json()["result"]
    chart = client.post(
        "/api/charts/spec",
        json={"result_id": query["id"], "type": "bar", "title": "区域销售</script><script>alert(1)"},
    ).get_json()["item"]

    chart_page = client.get(f"/api/chart/{chart['id']}")
    assert chart_page.status_code == 200
    assert b"echarts.init" in chart_page.data
    assert b"<script>alert(1)" not in chart_page.data
    assert b"\\u003c/script\\u003e" in chart_page.data

    dashboard = client.post(
        "/api/dashboard/generate",
        json={
            "name": "旧版看板",
            "widgets": [{"id": "sales", "title": "销售", "chart_id": chart["id"]}],
        },
    )
    assert dashboard.status_code == 200
    dashboard_id = dashboard.get_json()["dashboard_id"]

    raw = client.get(f"/api/dashboard/{dashboard_id}")
    assert raw.status_code == 200
    assert raw.get_json()["id"] == dashboard_id

    page = client.get(f"/dashboard/{dashboard_id}")
    assert page.status_code == 200
    assert b"dashboard-data" in page.data

    assert client.put(
        f"/api/dashboard/{dashboard_id}",
        json={"widgets": [{"id": "sales", "grid": {"x": 0, "y": 0, "w": 12, "h": 4}}]},
    ).status_code == 200
    assert client.post(f"/api/dashboard/{dashboard_id}/refresh").status_code == 200
    assert client.post(f"/api/dashboard/{dashboard_id}/widget/sales/refresh").status_code == 200

    exported = client.get(f"/api/dashboard/{dashboard_id}/export-html")
    assert exported.status_code == 200
    assert b"echarts.init" in exported.data


def test_legacy_workspace_workflow_and_team_routes(client, tmp_path):
    sid = client.post("/api/session/new", json={"name": "legacy workspace"}).get_json()["session_id"]
    workdir = tmp_path / "legacy-workspace"
    workdir.mkdir()
    (workdir / "sales.csv").write_text("region,sales\nNorth,10\nSouth,8\n", encoding="utf-8")

    mounted = client.post(
        f"/api/session/{sid}/workspace/mount",
        json={"path": str(workdir), "permission": "read_only"},
    )
    assert mounted.status_code == 200
    mount_payload = mounted.get_json()
    assert mount_payload["ok"] is True
    assert mount_payload["workspace"]["mounted"] is True
    assert mount_payload["added"]

    assert client.get(f"/api/session/{sid}/workspace").get_json()["workspace"]["mounted"] is True
    assert client.get(f"/api/session/{sid}/workspaces").status_code == 200
    assert client.post(
        f"/api/session/{sid}/workspace/permission",
        json={"permission": "read_write"},
    ).get_json()["workspace"]["permission"] == "read_write"
    assert client.get(f"/api/session/{sid}/workspaces/default/remove-preview").status_code == 200
    assert client.get(f"/api/session/{sid}/workspaces/default/storage-cleanup-preview").status_code == 200
    assert client.post(
        f"/api/session/{sid}/workspaces/default/storage-cleanup",
        json={"confirmed": True},
    ).status_code == 200
    assert client.get(f"/api/session/{sid}/workspace/checkpoints").status_code == 200

    profiles = client.get(f"/api/session/{sid}/agent-profiles")
    assert profiles.status_code == 200
    assert profiles.get_json()["profiles"]
    profile = client.post(
        f"/api/session/{sid}/agent-profiles",
        json={"key": "legacy-profile", "name": "旧版顾问", "role": "分析", "allowed_tools": ["query"]},
    )
    assert profile.status_code == 201

    workflow = client.post(
        f"/api/session/{sid}/workflows",
        json={
            "name": "旧版工作流",
            "graph": {"steps": [{"id": "notify", "type": "notification", "config": {"message": "ok"}}]},
        },
    )
    assert workflow.status_code == 201
    workflow_id = workflow.get_json()["workflow"]["id"]
    assert client.get(f"/api/session/{sid}/workflows").get_json()["workflows"]
    assert client.get(f"/api/session/{sid}/workflows/{workflow_id}").status_code == 200
    assert client.post(f"/api/session/{sid}/workflows/{workflow_id}/validate").get_json()["validation"]["valid"] is True
    assert client.post(f"/api/session/{sid}/workflows/{workflow_id}/publish").status_code == 200

    team = client.post(
        "/api/teams",
        json={
            "name": "旧版团队",
            "members": [{"name": "成员A", "role": "分析顾问", "tools": ["query"]}],
        },
    )
    assert team.status_code == 201
    team_id = team.get_json()["item"]["id"]
    assert client.get(f"/api/session/{sid}/teams").status_code == 200
    assert client.get(f"/api/session/{sid}/teams/{team_id}").status_code == 200
    assert client.delete(
        f"/api/session/{sid}/teams/{team_id}/messages",
        json={"confirm": True},
    ).status_code == 200

    assert client.post(f"/api/session/{sid}/workspace/unmount").status_code == 200
