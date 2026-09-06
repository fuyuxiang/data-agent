from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.agent.store import RunStore


def wait_for(client, path, statuses, timeout=8):
    deadline = time.time() + timeout
    item = None
    while time.time() < deadline:
        item = client.get(path).get_json()["item"]
        if item.get("status") in statuses:
            return item
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {statuses}; last={item}")


def test_cron_validation_and_standard_day_semantics():
    from backend.services.scheduler import cron_matches, validate_cron

    assert validate_cron("*/15 8-18 * * 1-5") == "*/15 8-18 * * 1-5"
    with pytest.raises(ValueError):
        validate_cron("99 * * * *")
    assert cron_matches("0 9 1 * 1", datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc))
    assert cron_matches("0 9 1 * 1", datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
    assert not cron_matches("0 9 1 * 1", datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc))


def test_schedule_requires_published_workflow_and_valid_configuration(client):
    workflow = client.post(
        "/api/workflows",
        json={"name": "Scheduled", "definition": {"steps": [{"id": "n", "type": "notification", "config": {"message": "ok"}}]}},
    ).get_json()["item"]
    assert client.post(
        "/api/schedules", json={"workflow_id": workflow["id"], "cron": "0 9 * * 1"},
    ).status_code == 400
    assert client.post(f"/api/workflows/{workflow['id']}/publish").status_code == 200
    created = client.post(
        "/api/schedules",
        json={"workflow_id": workflow["id"], "cron": "0 9 * * 1", "timezone": "Asia/Shanghai"},
    )
    assert created.status_code == 201
    schedule_id = created.get_json()["item"]["id"]
    assert client.patch(f"/api/schedules/{schedule_id}", json={"cron": "90 * * * *"}).status_code == 400
    assert client.patch(f"/api/schedules/{schedule_id}", json={"timezone": "Not/AZone"}).status_code == 400


def test_workflow_validation_approval_resume_and_export(app, client, source):
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
    assert completed["publication_id"]
    actions = RunStore(app.extensions["meridian_db"]).actions(completed["agent_run_id"])
    assert {item["tool_id"] for item in actions} == {"workflow_step", "validate_result"}


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
    assert job["status"] == "failed"
    assert "模型" in str(job.get("error") or "")

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


def test_dashboard_refresh_reexecutes_source_query(app, client, source):
    query = client.post(
        "/api/query",
        json={"source_ids": [source["id"]], "sql": "SELECT SUM(sales) AS total_sales FROM data"},
    ).get_json()["result"]
    dashboard = client.post(
        "/api/dashboards",
        json={"name": "Live", "widgets": [{"id": "sales", "result_id": query["id"], "type": "kpi"}]},
    ).get_json()["item"]
    database = app.extensions["meridian_db"]
    source_record = database.get("sources", source["id"])
    with open(source_record["path"], "a", encoding="utf-8") as stream:
        stream.write("North,2026-04-01,999,10,1\n")

    response = client.post(f"/api/dashboards/{dashboard['id']}/refresh")
    assert response.status_code == 200
    widget = response.get_json()["item"]["widgets"][0]
    assert widget["result_id"] != query["id"]
    assert widget["refresh_status"] == "ready"
    assert float(widget["kpi_value"]) == 1744


def test_team_members_use_bounded_tools_mailbox_and_quality_review(client, source, monkeypatch):
    class Completions:
        def __init__(self):
            self.calls = []
            self.stream_calls = 0

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("stream"):
                self.stream_calls += 1
                if not kwargs.get("tools"):
                    delta = SimpleNamespace(
                        content="负责人汇总：证据链完整。", refusal=None, tool_calls=[],
                    )
                    finish = "stop"
                elif self.stream_calls == 1:
                    delta = SimpleNamespace(
                        content=None, refusal=None,
                        tool_calls=[SimpleNamespace(
                            index=0, id="team_query",
                            function=SimpleNamespace(
                                name="query_data", arguments='{"sql":"SELECT region, SUM(sales) AS sales FROM data GROUP BY region"}',
                            ),
                        )],
                    )
                    finish = "tool_calls"
                elif self.stream_calls == 2:
                    previous = __import__("json").loads(kwargs["messages"][-1]["content"])
                    arguments = __import__("json").dumps({
                        "dataset_ref_id": previous["dataset_ref_id"],
                        "result_id": previous["result_id"],
                    })
                    delta = SimpleNamespace(
                        content=None, refusal=None,
                        tool_calls=[SimpleNamespace(
                            index=0, id="team_validate",
                            function=SimpleNamespace(name="validate_result", arguments=arguments),
                        )],
                    )
                    finish = "tool_calls"
                else:
                    delta = SimpleNamespace(
                        content="已依据 schema 证据完成风险检查。", refusal=None, tool_calls=[],
                    )
                    finish = "stop"
                return iter([SimpleNamespace(
                    choices=[SimpleNamespace(delta=delta, finish_reason=finish)],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, total_tokens=13),
                )])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="负责人汇总：证据链完整。"))],
                usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4, total_tokens=12),
            )

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
    assert {item["tool"] for item in run["tasks"][0]["result"]["tool_evidence"]} >= {
        "query_data", "validate_result",
    }
    assert run["review"]["status"] == "passed"
    assert run["summary"] == "负责人汇总：证据链完整。"
    assert "重点检查字段结构" in completions.calls[0]["messages"][1]["content"]
    assert {item["function"]["name"] for item in completions.calls[0]["tools"]} == {
        "get_schema", "profile_data", "query_data", "validate_result", "update_plan",
    }
