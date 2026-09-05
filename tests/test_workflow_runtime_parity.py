from __future__ import annotations

import threading
import time

from backend.services import workflows


def test_independent_workflow_nodes_run_in_parallel_with_manifests_lineage_and_fork(app, client, monkeypatch):
    session = client.post("/api/sessions", json={"name": "并行工作流"}).get_json()["item"]
    database = app.extensions["meridian_db"]
    workflow = database.put(
        "workflows",
        {"id": "flow_parallel", "workspace_id": "default", "name": "并行流", "status": "published"},
        workspace_id="default",
    )
    definition = {
        "steps": [
            {"id": "report", "name": "报告", "type": "agent", "depends_on": [],
             "config": {"prompt": "report"}, "output_contract": ["report"]},
            {"id": "query", "name": "SQL", "type": "agent", "depends_on": [],
             "config": {"prompt": "query"}, "output_contract": ["metric_sql"]},
        ],
    }
    run = database.put(
        "workflow_runs",
        {
            "id": "run_parallel", "workspace_id": "default", "workflow_id": workflow["id"],
            "workflow_version": 1, "workflow_version_id": "wfver_parallel",
            "definition_snapshot": definition, "status": "queued", "inputs": {"session_id": session["id"]},
            "outputs": {}, "step_states": {
                "report": {"status": "pending", "attempts": 0},
                "query": {"status": "pending", "attempts": 0},
            },
            "order": ["report", "query"], "pause_requested": False, "cancel_requested": False,
        },
        workspace_id="default",
    )
    lock = threading.Lock()
    active = 0
    maximum = 0

    def fake_step(step, _config, _context, _workspace_id):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.12)
        with lock:
            active -= 1
        if step["id"] == "report":
            return {"report": "经营结论：收入稳定增长"}
        return {"metric_sql": {"sql": "SELECT SUM(revenue) FROM data"}, "sql": "SELECT SUM(revenue) FROM data"}

    monkeypatch.setattr(workflows, "_execute_step", fake_step)
    with app.app_context():
        result = workflows.execute_run(run["id"], lambda *_args: None, threading.Event())
        assert result["status"] == "completed"
        assert maximum == 2
        completed = database.get("workflow_runs", run["id"])
        detail = workflows.run_detail(database, completed)
        assert len(detail["nodes"]) == 2
        assert len(detail["manifests"]) == 2
        assert len(detail["lineage"]) == 2
        artifact_id = detail["lineage"][0]["artifact_id"]
        node = next(item for item in detail["nodes"] if item["node_id"] == "report")
        branch = workflows.fork_run(database, completed, node["id"])
        assert branch["forked_from_run_id"] == run["id"]
        assert branch["step_states"]["report"]["reused"] is True
        assert branch["step_states"]["query"]["status"] == "pending"
        candidates = workflows.generate_knowledge_candidates(database, completed)
        assert {item["candidate_type"] for item in candidates} == {"report_template", "metric_sql"}

    artifact = client.get(
        f"/api/session/{session['id']}/workflow-runs/{run['id']}/artifacts/{artifact_id}",
    )
    assert artifact.status_code == 200
    assert artifact.get_json()["sha256"]
    candidate = next(item for item in candidates if item["candidate_type"] == "report_template")
    accepted = client.post(
        f"/api/session/{session['id']}/workflow-knowledge-candidates/{candidate['id']}/decide",
        json={"decision": "accept", "comment": "人工复核通过"},
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["candidate"]["status"] == "accepted"
    assert accepted.get_json()["candidate"]["published_ref"]["id"]


def test_workflow_metrics_and_success_template_are_human_gated(app, client):
    session = client.post("/api/sessions", json={"name": "工作流指标"}).get_json()["item"]
    database = app.extensions["meridian_db"]
    database.put(
        "workflow_runs",
        {
            "id": "run_metric", "workspace_id": "default", "workflow_id": "flow_metric",
            "workflow_version_id": "wfver_metric", "workflow_version": 1,
            "definition_snapshot": {"steps": [{"id": "done", "type": "approval", "depends_on": [], "config": {}}]},
            "status": "completed", "inputs": {"session_id": session["id"]}, "outputs": {"done": {"approved": True}},
            "step_states": {"done": {"status": "completed", "attempts": 1}}, "order": ["done"],
            "started_at": "2026-09-05T00:00:00+00:00", "finished_at": "2026-09-05T00:00:03+00:00",
        },
        workspace_id="default",
    )
    template = client.post(
        f"/api/session/{session['id']}/workflow-runs/run_metric/template",
        json={"name": "已复核模板"},
    )
    assert template.status_code == 201
    assert template.get_json()["template"]["name"] == "已复核模板"
    metrics = client.get(f"/api/session/{session['id']}/workflow-metrics").get_json()["metrics"]
    assert metrics["summary"]["run_count"] >= 1
    assert metrics["versions"][0]["success_rate"] == 1
