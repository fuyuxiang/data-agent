from __future__ import annotations

import time
from types import SimpleNamespace


def wait_for(client, path, statuses, timeout=8):
    deadline = time.time() + timeout
    item = None
    while time.time() < deadline:
        item = client.get(path).get_json()["item"]
        if item.get("status") in statuses:
            return item
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {statuses}; last={item}")


def test_workflow_validation_approval_resume_and_export(client, source):
    definition = {
        "steps": [
            {"id": "query", "name": "查询", "type": "query", "depends_on": [], "config": {"source_ids": [source["id"]], "sql": "SELECT * FROM data"}},
            {"id": "review", "name": "复核", "type": "approval", "depends_on": ["query"], "config": {}},
            {"id": "deliver", "name": "交付", "type": "export_data", "depends_on": ["review"], "config": {"format": "xlsx"}},
        ]
    }
    workflow = client.post("/api/workflows", json={"name": "交付流程", "definition": definition}).get_json()["item"]
    validation = client.post(f"/api/workflows/{workflow['id']}/validate").get_json()["validation"]
    assert validation["valid"] is True
    assert validation["order"] == ["query", "review", "deliver"]
    assert client.post(f"/api/workflows/{workflow['id']}/publish").status_code == 200
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"inputs": {}}).get_json()["run"]
    waiting = wait_for(client, f"/api/workflow-runs/{run['id']}", {"waiting_approval", "failed"})
    assert waiting["status"] == "waiting_approval"
    assert client.post(f"/api/workflow-runs/{run['id']}/approve", json={"comment": "口径正确"}).status_code == 202
    completed = wait_for(client, f"/api/workflow-runs/{run['id']}", {"completed", "failed"})
    assert completed["status"] == "completed", completed.get("error")
    assert completed["outputs"]["deliver"]["kind"] == "xlsx"


def test_invalid_workflow_cycle(client):
    workflow = client.post(
        "/api/workflows",
        json={"name": "循环流程", "definition": {"steps": [
            {"id": "a", "type": "approval", "depends_on": ["b"], "config": {}},
            {"id": "b", "type": "approval", "depends_on": ["a"], "config": {}},
        ]}},
    ).get_json()["item"]
    validation = client.post(f"/api/workflows/{workflow['id']}/validate").get_json()["validation"]
    assert validation["valid"] is False
    assert any("环路" in error for error in validation["errors"])


def test_workflow_graph_versions_conditions_and_idempotency(client, source):
    graph = {
        "entry_node_ids": ["query"],
        "nodes": [
            {
                "node_id": "query", "type": "sql",
                "config": {"source_ids": [source["id"]], "sql": "SELECT * FROM data"},
            },
            {
                "node_id": "validate", "type": "validation",
                "config": {"rules": [{"field": "steps.query.rows", "operator": "gte", "value": 1}]},
            },
            {"node_id": "notify", "type": "notification", "config": {"message": "不应执行"}},
        ],
        "edges": [
            {"id": "e1", "from_node": "query", "to_node": "validate", "type": "auto"},
            {
                "id": "e2", "from_node": "validate", "to_node": "notify", "type": "conditional",
                "condition": {"field": "steps.validate.valid", "operator": "equals", "value": False},
            },
        ],
    }
    workflow = client.post(
        "/api/workflows", json={"name": "图工作流", "definition": graph},
    ).get_json()["item"]
    validation = client.post(f"/api/workflows/{workflow['id']}/validate").get_json()["validation"]
    assert validation["valid"] is True
    first_publish = client.post(f"/api/workflows/{workflow['id']}/publish").get_json()
    second_publish = client.post(f"/api/workflows/{workflow['id']}/publish").get_json()
    assert first_publish["version"]["id"] == second_publish["version"]["id"]
    assert second_publish["reused"] is True
    assert len(client.get(f"/api/workflows/{workflow['id']}/versions").get_json()["items"]) == 1

    start_payload = {"inputs": {"request": "same"}, "idempotency_key": "stable-request-1"}
    first_run = client.post(f"/api/workflows/{workflow['id']}/runs", json=start_payload).get_json()["run"]
    repeated_run = client.post(f"/api/workflows/{workflow['id']}/runs", json=start_payload).get_json()["run"]
    assert repeated_run["id"] == first_run["id"]
    completed = wait_for(client, f"/api/workflow-runs/{first_run['id']}", {"completed", "failed"})
    assert completed["status"] == "completed", completed.get("error")
    assert completed["step_states"]["validate"]["status"] == "completed"
    assert completed["step_states"]["notify"]["status"] == "skipped"
    assert client.get(f"/api/workflow-runs/{first_run['id']}").get_json()["events"]

    conflict = client.put(
        f"/api/workflows/{workflow['id']}/draft",
        json={"description": "stale", "expected_revision": 999},
    )
    assert conflict.status_code == 400


def test_team_hook_map_dashboard_and_trash(client, source):
    profiles = client.get("/api/agent-profiles").get_json()["items"]
    team = client.post("/api/teams", json={"name": "复核组", "members": [{"name": item["name"], "role": item["role"]} for item in profiles[:2]]})
    assert team.status_code == 201
    team_id = team.get_json()["item"]["id"]
    team_run = client.post(f"/api/teams/{team_id}/runs", json={"task": "检查经营风险"})
    assert team_run.status_code == 202
    job_id = team_run.get_json()["job"]["id"]
    job = wait_for(client, f"/api/jobs/{job_id}", {"completed", "failed"})
    assert job["status"] == "completed"

    hook = client.post("/api/hooks", json={"name": "一次记录", "event": "test.done", "once": True, "action": {"type": "noop"}})
    assert hook.status_code == 201
    dispatched = client.post("/api/hooks/dispatch", json={"event": "test.done", "payload": {"value": 1}})
    assert len(dispatched.get_json()["matched"]) == 1

    decision_map = client.post("/api/decision-maps", json={"name": "指标树", "template_id": "metric-tree"}).get_json()["item"]
    updated = client.patch(f"/api/decision-maps/{decision_map['id']}", json={"blocks": {**decision_map["blocks"], "北极星指标": "销售额"}})
    assert updated.get_json()["item"]["revision"] == 2

    query = client.post("/api/query", json={"source_ids": [source["id"]], "sql": "SELECT region, SUM(sales) AS sales FROM data GROUP BY region"}).get_json()["result"]
    chart = client.post("/api/charts/spec", json={"result_id": query["id"], "title": "销售"}).get_json()["item"]
    dashboard = client.post("/api/dashboards", json={"name": "经营看板", "widgets": [{"id": "w1", "title": "销售", "result_id": query["id"], "chart": chart["spec"]}]}).get_json()["item"]
    assert client.post(f"/api/dashboards/{dashboard['id']}/refresh").status_code == 200
    html_export = client.post(f"/api/dashboards/{dashboard['id']}/export")
    assert html_export.status_code == 200
    assert client.delete(f"/api/dashboards/{dashboard['id']}").status_code == 200
    trash = client.get("/api/trash").get_json()["items"]
    assert any(item["id"] == dashboard["id"] for item in trash)
    assert client.post(f"/api/trash/dashboards/{dashboard['id']}/restore").status_code == 200


def test_team_members_use_bounded_tools_mailbox_and_quality_review(client, source, monkeypatch):
    class Completions:
        def __init__(self):
            self.calls = []
            self.responses = [
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content=None,
                        tool_calls=[SimpleNamespace(
                            id="team_schema", function=SimpleNamespace(name="get_schema", arguments="{}"),
                        )],
                    ))],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content="已依据 schema 证据完成风险检查。", tool_calls=[],
                    ))],
                    usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5, total_tokens=17),
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content="负责人汇总：证据链完整。", tool_calls=[],
                    ))],
                    usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4, total_tokens=12),
                ),
            ]

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return self.responses.pop(0)

    completions = Completions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(
        "backend.services.teams.resolve_provider",
        lambda _provider_id=None, _workspace_id="default": ({"model": "team-model", "temperature": 0}, fake_client),
    )
    team = client.post(
        "/api/teams",
        json={
            "name": "证据团队",
            "members": [{"name": "分析员", "role": "数据证据检查", "tools": ["query"]}],
        },
    ).get_json()["item"]
    assert client.post(
        f"/api/teams/{team['id']}/messages",
        json={"sender": "leader", "recipients": ["分析员"], "content": "重点检查字段结构"},
    ).status_code == 201
    started = client.post(
        f"/api/teams/{team['id']}/runs",
        json={"task": "检查经营风险", "source_ids": [source["id"]]},
    ).get_json()
    job = wait_for(client, f"/api/jobs/{started['job']['id']}", {"completed", "failed"})
    assert job["status"] == "completed", job.get("error")
    run = client.get(f"/api/team-runs/{started['run']['id']}").get_json()["item"]
    assert run["status"] == "completed"
    assert run["tasks"][0]["result"]["tool_evidence"][0]["tool"] == "get_schema"
    assert run["review"]["status"] == "passed"
    assert run["summary"] == "负责人汇总：证据链完整。"
    assert "重点检查字段结构" in completions.calls[0]["messages"][1]["content"]
    assert {item["function"]["name"] for item in completions.calls[0]["tools"]} == {
        "get_schema", "profile_data", "query_data",
    }
