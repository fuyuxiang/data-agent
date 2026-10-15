"""Library follow-up keeps its source identity after analysis archiving."""

from backend.agent.store import RunStore


def test_library_detail_preserves_the_exact_analysis_and_archive_recovery(app, client):
    created = client.post("/api/analyses", json={"objective": "检查资料来源", "source_ids": []})
    run = created.get_json()["item"]
    database = app.extensions["meridian_db"]
    path = app.config["SETTINGS"].export_dir / "journey-source.txt"
    path.write_text("已保存的分析资料", encoding="utf-8")
    database.put("artifacts", {
        "id": "journey-source", "workspace_id": "default", "actor_id": "local-default",
        "run_id": run["id"], "kind": "report_html", "filename": path.name, "path": str(path),
    }, workspace_id="default")
    detail_url = "/api/library/journey-source"
    assert client.get(detail_url).get_json()["item"]["analysis"] == {
        "id": run["id"], "session_id": run["session_id"], "archived": False, "session_archived": False,
    }
    RunStore(database).update_status(run["id"], "failed", stop_reason="fixture_complete")
    assert client.delete(f"/api/analyses/{run['id']}").status_code == 200
    detail = client.get(detail_url)
    assert detail.status_code == 200
    assert detail.get_json()["item"]["analysis"]["archived"] is True
    # Archiving hides the analysis but retains its existing deliverable.
    assert client.get(f"/api/analyses/{run['id']}").status_code == 404
    assert client.delete(f"/api/sessions/{run['session_id']}").status_code == 200
    assert client.get(detail_url).get_json()["item"]["analysis"]["session_archived"] is True
    download = client.get(f"{detail_url}/download")
    assert download.status_code == 200
    assert download.data.decode() == "已保存的分析资料"
