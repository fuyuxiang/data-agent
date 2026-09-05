from __future__ import annotations


def test_lifecycle_settings_report_and_upload_trash_roundtrip(app, client):
    defaults = client.get("/api/lifecycle/settings").get_json()["settings"]
    assert defaults == {"retention_preset": "forever", "retention_custom_days": 30}
    changed = client.put(
        "/api/lifecycle/settings",
        json={"retention_preset": "custom", "retention_custom_days": 45},
    ).get_json()["settings"]
    assert changed["retention_custom_days"] == 45
    assert client.put(
        "/api/lifecycle/settings",
        json={"retention_preset": "custom", "retention_custom_days": 4000},
    ).status_code == 400

    upload = app.config["SETTINGS"].upload_dir / "legacy-cache.csv"
    upload.write_text("a,b\n1,2\n", encoding="utf-8")
    preview = client.get("/api/lifecycle/uploads/preview").get_json()["preview"]
    candidate = next(item for item in preview["samples"] if item["filename"] == upload.name)
    recycled = client.post(
        "/api/lifecycle/uploads/recycle",
        json={"category": "unknown_uploads", "relative_path": candidate["relative_path"]},
    ).get_json()["summary"]
    assert not upload.exists()
    trash = client.get("/api/lifecycle/upload-trash").get_json()["items"]
    assert trash[0]["id"] == recycled["trash_id"]
    assert "trash_path" not in trash[0] and "original_path" not in trash[0]

    restored = client.post(
        f"/api/lifecycle/upload-trash/{recycled['trash_id']}/restore"
    ).get_json()["summary"]
    assert restored["restored"] == [upload.name]
    assert upload.read_text(encoding="utf-8").startswith("a,b")

    report = client.get("/api/lifecycle/report").get_json()["report"]
    assert report["total_files"] >= 1
    assert "uploads" in report["locations"]
    assert client.get("/api/lifecycle/workspaces/preview").get_json()["preview"]["dry_run"] is True


def test_registered_artifact_recycle_restore_and_missing_reconciliation(app, client):
    artifact = client.post(
        "/api/exports/data",
        json={"rows": [{"region": "North", "sales": 120}], "format": "csv", "title": "回收测试"},
    ).get_json()["artifact"]
    with app.app_context():
        stored = app.extensions["meridian_db"].get("artifacts", artifact["id"])
        path = stored["path"]
    recycled = client.post(
        "/api/lifecycle/artifacts/registered/recycle", json={"artifact_id": artifact["id"]},
    )
    assert recycled.status_code == 200
    trash_id = recycled.get_json()["summary"]["trash_id"]
    assert client.get(artifact["download_url"]).status_code == 404
    assert any(item["id"] == trash_id for item in client.get("/api/lifecycle/artifact-trash").get_json()["items"])

    restored = client.post(f"/api/lifecycle/artifact-trash/{trash_id}/restore")
    assert restored.status_code == 200
    assert client.get(artifact["download_url"]).status_code == 200

    # A missing registered file is reported by dry-run and only removed by explicit reconciliation.
    from pathlib import Path

    Path(path).unlink()
    missing = client.get("/api/lifecycle/artifacts/preview").get_json()["preview"]
    assert artifact["id"] in missing["missing_registered_ids"]
    pruned = client.post("/api/lifecycle/artifacts/prune-missing").get_json()["summary"]
    assert pruned["removed"] == 1


def test_session_and_memory_trash_restore_then_retention_reclaim(app, client):
    session = client.post("/api/sessions", json={"name": "待恢复会话"}).get_json()["item"]
    with app.app_context():
        app.extensions["meridian_db"].add_message(session["id"], "user", "需要保留的消息")
    memory = client.post(
        "/api/memories", json={"title": "可恢复记忆", "content": "财年从四月开始"},
    ).get_json()["item"]
    assert client.delete(f"/api/sessions/{session['id']}").status_code == 200
    assert client.delete(f"/api/memories/{memory['id']}", json={"confirm": True}).status_code == 200
    assert any(item["id"] == session["id"] for item in client.get("/api/lifecycle/session-trash").get_json()["items"])
    assert any(item["id"] == memory["id"] for item in client.get("/api/lifecycle/memory-trash").get_json()["items"])

    assert client.post(f"/api/lifecycle/session-trash/{session['id']}/restore").status_code == 200
    assert client.post(f"/api/lifecycle/memory-trash/{memory['id']}/restore").status_code == 200
    assert client.get(f"/api/sessions/{session['id']}").status_code == 200

    client.delete(f"/api/sessions/{session['id']}")
    client.delete(f"/api/memories/{memory['id']}", json={"confirm": True})
    assert client.post("/api/lifecycle/session-trash/reclaim", json={"retention_days": 0}).get_json()["summary"]["items"] == 1
    assert client.post("/api/lifecycle/memory-trash/reclaim", json={"retention_days": 0}).get_json()["summary"]["items"] == 1
    with app.app_context():
        database = app.extensions["meridian_db"]
        assert database.get("sessions", session["id"], include_archived=True) is None
        assert database.messages(session["id"]) == []
        assert database.get("memories", memory["id"], include_archived=True) is None
    assert client.get("/api/lifecycle/audit").get_json()["items"]
