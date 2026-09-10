from __future__ import annotations

import json
from datetime import datetime

import pytest

from backend.agent.store import RunStore
from backend.core.database import utcnow
from backend.services.advanced_agent import _business_space, _space_knowledge_ids
from backend.services.agent_tools import AgentToolContext, execute_tool
from backend.services.scheduler import cron_matches, subscription_cron

def _approved_metric(client, source, *, name="revenue", measure="sales"):
    model = client.post("/api/semantic/models", json={
        "name": f"{name}_model", "source_id": source["id"], "table": "data",
        "grain": "每行一个区域月度记录",
        "dimensions": [
            {"name": "region", "column": "region", "type": "categorical"},
            {"name": "month", "column": "month", "type": "time"},
        ],
        "measures": [{"name": measure, "column": measure, "aggregation": "sum"}],
        "default_time_dimension": "month",
    }).get_json()["item"]
    response = client.post("/api/semantic/metrics", json={
        "name": name, "label": "营业收入", "model_id": model["id"],
        "measure": measure, "metric_type": "atomic", "status": "approved",
        "business_object": "区域经营单元", "business_event": "收入确认",
        "business_owner": "经营管理部", "technical_owner": "数据中心",
    })
    assert response.status_code == 201, response.get_json()
    return model, response.get_json()["item"]


def test_business_space_publish_home_and_personal_subscription(client, source):
    _model, metric = _approved_metric(client, source)
    response = client.post("/api/business-spaces", json={
        "name": "经营分析空间", "business_domain": "经营管理",
        "source_ids": [source["id"]], "metric_ids": [metric["id"]],
        "knowledge_tags": ["经营口径"],
        "recommended_questions": ["本月营业收入是多少？", "各区域收入排名如何？"],
    })
    assert response.status_code == 201, response.get_json()
    space = response.get_json()["item"]
    assert space["status"] == "draft"
    assert space["readiness"]["publishable"] is True

    published = client.post(f"/api/business-spaces/{space['id']}/publish")
    assert published.status_code == 200, published.get_json()
    assert published.get_json()["item"]["status"] == "published"

    subscription = client.post("/api/subscriptions", json={
        "name": "每日收入简报", "business_space_id": space["id"],
        "metric_id": metric["id"], "frequency": "daily", "delivery_time": "09:00",
        "channel": "in_app", "condition": "环比下降超过 10% 时提醒",
    })
    assert subscription.status_code == 201, subscription.get_json()
    home = client.get("/api/business/home").get_json()
    assert home["active_space"]["id"] == space["id"]
    assert home["metrics"][0]["business_owner"] == "经营管理部"
    assert home["subscriptions"][0]["frequency"] == "daily"
    assert home["readiness"] == {"data": True, "metrics": True, "space": True}


def test_analysis_uses_business_space_scope_and_quick_mode_auto_confirms(client, source, monkeypatch):
    _model, metric = _approved_metric(client, source)
    space = client.post("/api/business-spaces", json={
        "name": "销售空间", "source_ids": [source["id"]], "metric_ids": [metric["id"]],
        "recommended_questions": ["收入是多少？"],
    }).get_json()["item"]
    client.post(f"/api/business-spaces/{space['id']}/publish")

    submitted = {}
    manager = type("Manager", (), {
        "submit_spec": lambda self, **kwargs: submitted.update(kwargs) or {"id": "job-quick", **kwargs},
    })()
    monkeypatch.setattr("backend.api.analyses.get_job_manager", lambda _app: manager)
    response = client.post("/api/analyses", json={
        "objective": "本月收入是多少？", "business_space_id": space["id"],
        "execution_mode": "quick", "auto_confirm": True,
    })
    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert payload["auto_confirmed"] is True
    assert payload["item"]["source_scope"] == [source["id"]]
    assert payload["item"]["contract"]["confirmed_at"]
    assert submitted["job_type"] == "analysis_run"


def test_derived_metric_formula_executes_with_certified_dependencies(client, source):
    model = client.post("/api/semantic/models", json={
        "name": "profit_model", "source_id": source["id"], "table": "data",
        "dimensions": [{"name": "region", "column": "region", "type": "categorical"}],
        "measures": [
            {"name": "sales_sum", "column": "sales", "aggregation": "sum"},
            {"name": "cost_sum", "column": "cost", "aggregation": "sum"},
        ],
    }).get_json()["item"]
    for name, measure in (("gross_sales", "sales_sum"), ("total_cost", "cost_sum")):
        result = client.post("/api/semantic/metrics", json={
            "name": name, "model_id": model["id"], "measure": measure,
            "metric_type": "atomic", "status": "approved",
        })
        assert result.status_code == 201, result.get_json()
    derived = client.post("/api/semantic/metrics", json={
        "name": "gross_profit", "label": "毛利润", "model_id": model["id"],
        "metric_type": "derived", "expression": "gross_sales - total_cost",
        "status": "approved", "unit": "元",
    })
    assert derived.status_code == 201, derived.get_json()
    result = client.post("/api/semantic/query", json={"metric": "gross_profit", "group_by": ["region"]})
    assert result.status_code == 200, result.get_json()
    rows = sorted(result.get_json()["result"]["data"], key=lambda row: row["region"])
    assert rows == [
        {"region": "North", "gross_profit": 165.0},
        {"region": "South", "gross_profit": 52.0},
    ]


def test_subscription_schedule_validation_and_manual_run(client, source, app, monkeypatch):
    _model, metric = _approved_metric(client, source, name="scheduled_revenue")
    space = client.post("/api/business-spaces", json={
        "name": "订阅空间", "source_ids": [source["id"]], "metric_ids": [metric["id"]],
        "recommended_questions": ["每日收入变化如何？"],
    }).get_json()["item"]
    client.post(f"/api/business-spaces/{space['id']}/publish")

    invalid_timezone = client.post("/api/subscriptions", json={
        "name": "无效时区", "business_space_id": space["id"], "timezone": "Mars/Olympus",
    })
    assert invalid_timezone.status_code == 400
    missing_connector = client.post("/api/subscriptions", json={
        "name": "外部推送", "business_space_id": space["id"], "channel": "webhook",
    })
    assert missing_connector.status_code == 400

    created = client.post("/api/subscriptions", json={
        "name": "每周收入简报", "business_space_id": space["id"], "metric_id": metric["id"],
        "frequency": "weekly", "delivery_time": "08:30", "timezone": "Asia/Shanghai",
        "question": "汇总本周收入变化", "channel": "in_app",
    })
    assert created.status_code == 201, created.get_json()
    subscription = created.get_json()["item"]
    assert subscription["cron"] == "30 8 * * 1"
    assert cron_matches(subscription_cron("weekly", "08:30"), datetime(2026, 9, 14, 8, 30))

    submitted = {}
    manager = type("Manager", (), {
        "submit_spec": lambda self, **kwargs: submitted.update(kwargs) or {"id": "job-subscription", **kwargs},
    })()
    monkeypatch.setattr("backend.services.scheduler.get_job_manager", lambda _app: manager)
    response = client.post(f"/api/subscriptions/{subscription['id']}/run")
    assert response.status_code == 202, response.get_json()
    assert response.get_json()["run"]["run_kind"] == "subscription"
    contract = RunStore(app.extensions["meridian_db"]).latest_contract(response.get_json()["run"]["id"])
    assert contract and contract["confirmed_at"]
    assert submitted["job_type"] == "analysis_run"


def test_business_space_is_the_runtime_metric_knowledge_and_skill_boundary(client, source, app, monkeypatch):
    model, metric = _approved_metric(client, source, name="space_revenue")
    outside = client.post("/api/semantic/metrics", json={
        "name": "outside_metric", "label": "空间外指标", "model_id": model["id"],
        "measure": "sales", "metric_type": "atomic", "status": "approved",
    }).get_json()["item"]
    knowledge = client.post("/api/knowledge/entries", json={
        "type": "context_note", "name": "经营周期口径", "content": "自然月为一个经营周期。",
        "tags": ["经营口径"],
    }).get_json()["item"]
    space_response = client.post("/api/business-spaces", json={
        "name": "受控经营空间", "source_ids": [source["id"]], "metric_ids": [metric["id"]],
        "knowledge_tags": ["经营口径"], "skill_ids": ["executive-summary"],
        "recommended_questions": ["本月收入是多少？"],
    })
    assert space_response.status_code == 201, space_response.get_json()
    space = space_response.get_json()["item"]
    client.post(f"/api/business-spaces/{space['id']}/publish")

    manager = type("Manager", (), {
        "submit_spec": lambda self, **kwargs: {"id": "job-boundary", **kwargs},
    })()
    monkeypatch.setattr("backend.api.analyses.get_job_manager", lambda _app: manager)
    created = client.post("/api/analyses", json={
        "objective": "本月收入是多少？", "business_space_id": space["id"],
        "execution_mode": "quick", "auto_confirm": True,
    }).get_json()["item"]
    database = app.extensions["meridian_db"]
    assert _business_space(database, created)["id"] == space["id"]
    assert knowledge["id"] in _space_knowledge_ids(database, created, space)

    context = AgentToolContext(
        database=database, workspace_id="default", session_id=created["session_id"],
        source_ids=[source["id"]], semantic_metric_ids=[metric["id"]], actor_id="local-default",
    )
    listed, _events = execute_tool("list_semantic_metrics", {}, context)
    assert [item["id"] for item in listed["items"]] == [metric["id"]]
    with pytest.raises(PermissionError, match="未在当前业务数据空间发布"):
        execute_tool("query_metric", {"metric": outside["id"]}, context)


def test_reports_require_published_owned_result(client, app):
    session = client.post("/api/sessions", json={"name": "报告分析"}).get_json()["item"]
    run = client.post("/api/analyses", json={
        "session_id": session["id"], "objective": "形成经营结论",
    }).get_json()["item"]
    rejected = client.post("/api/reports", json={"title": "经营月报", "run_id": run["id"]})
    assert rejected.status_code == 400

    database = app.extensions["meridian_db"]
    now = utcnow()
    manifest_id = database.new_id("manifest")
    publication_id = database.new_id("publication")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO result_manifests(id,workspace_id,run_id,version,status,payload,created_at) VALUES(?,?,?,?,?,?,?)",
            (manifest_id, "default", run["id"], 1, "published", json.dumps({"summary": "可信结论"}), now),
        )
        connection.execute(
            "INSERT INTO publications(id,workspace_id,run_id,manifest_id,contract_version,policy_version,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (publication_id, "default", run["id"], manifest_id, 1, "agent-policy-v1", "{}", now),
        )
    created = client.post("/api/reports", json={"title": "经营月报", "run_id": run["id"]})
    assert created.status_code == 201, created.get_json()
    report = created.get_json()["item"]
    published = client.post(f"/api/reports/{report['id']}/publish", json={"visibility": "private"})
    assert published.status_code == 200
    assert published.get_json()["item"]["status"] == "published"
