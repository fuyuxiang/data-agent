from __future__ import annotations


def test_focused_product_keeps_analysis_foundations(client):
    assert client.get("/api/bootstrap").status_code == 200
    assert client.get("/api/sources").status_code == 200
    assert client.get("/api/semantic/metrics").status_code == 200
    assert client.get("/api/knowledge/entries").status_code == 200
    assert client.get("/api/jobs").status_code == 200


def test_retired_product_surfaces_are_not_exposed(client):
    for path in (
        "/api/product/plans",
        "/api/business-spaces",
        "/api/workflows",
        "/api/schedules",
        "/api/teams",
        "/api/feishu-bot",
        "/api/dashboards",
        "/api/analysis/methods",
        "/api/analysis/runs",
        "/api/charts/catalog",
        "/api/commands/compact",
        "/api/memories",
        "/api/lifecycle/memory-trash",
        "/api/skills/reload",
        "/api/exports/data",
        "/api/exports/report",
    ):
        assert client.get(path).status_code == 404
    assert client.post("/api/analysis/run", json={"rows": [{"value": 1}]}).status_code == 404
    assert client.post("/api/charts/spec", json={"rows": [{"value": 1}]}).status_code == 404
    assert client.post("/api/skills", json={"name": "新技能"}).status_code == 405


def test_agent_tool_surface_matches_focused_product(app):
    from backend.services.agent_tools import AgentToolContext, tool_schemas

    context = AgentToolContext(app.extensions["meridian_db"], "default", "welcome", ["source-demo"])
    exposed = {item["function"]["name"] for item in tool_schemas(context)}

    assert {
        "get_schema",
        "query_data",
        "list_semantic_metrics",
        "query_metric",
        "query_knowledge",
        "generate_chart",
    }.issubset(exposed)
    assert exposed.isdisjoint({
        "propose_dashboard_outline",
        "generate_dashboard",
        "list_feishu_bitable_tables",
        "load_feishu_bitable",
        "team_create",
        "team_delete",
        "team_list",
        "team_status",
        "send_message",
        "agent_delegate",
        "team_plan_create",
        "team_delegate",
        "workflow_create",
        "workflow_create_custom",
        "workflow_list",
        "workflow_start",
        "workflow_status",
        "memory_read",
        "export_report",
        "export_excel",
    })


def test_demo_seed_populates_the_focused_analysis_flow(client):
    first = client.post("/api/demo/seed", json={"workspace_id": "default"})
    assert first.status_code == 200, first.get_json()
    payload = first.get_json()
    assert payload["source"]["sample_seed"]["id"] == "instant_retail_city_pack"
    assert len(payload["recommended_questions"]) == 4

    bootstrap = client.get("/api/bootstrap").get_json()
    assert payload["source"]["id"] in bootstrap["active_session"]["source_ids"]
    assert any(item.get("sample_seed", {}).get("id") == "instant_retail_city_pack" for item in bootstrap["sources"])
    assert client.get("/api/semantic/metrics").get_json()["items"]
    knowledge = client.get("/api/knowledge/entries").get_json()["items"]
    assert knowledge
    assert any("Data Agent 核心主路径" in item.get("content", "") for item in knowledge)

    repeated = client.post("/api/demo/seed", json={"workspace_id": "default"})
    assert repeated.status_code == 200
    assert repeated.get_json()["created"] == []
