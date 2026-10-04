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
        "/api/exports/data",
        "/api/exports/report",
    ):
        assert client.get(path).status_code == 404
    assert client.post("/api/analysis/run", json={"rows": [{"value": 1}]}).status_code == 404
    assert client.post("/api/charts/spec", json={"rows": [{"value": 1}]}).status_code == 404
    # Skill creation is a real V2 capability; what must stay retired is the
    # V1 pseudo-skill surface, not the Skill runtime itself.
    assert client.post("/api/skills/reload").status_code == 405


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
    from backend.services.demo_sales import SAMPLE_SEED_ID, sample_questions

    first = client.post("/api/demo/seed", json={"workspace_id": "default"})
    assert first.status_code == 200, first.get_json()
    payload = first.get_json()
    assert payload["source"]["sample_seed"]["id"] == SAMPLE_SEED_ID
    assert payload["recommended_questions"] == sample_questions()[:4]

    # The demo must be a real multi-dimensional fact table, not a lookup sheet:
    # year-over-year, attribution and forecasting are impossible without one.
    summary = payload["summary"]
    assert summary["months"] >= 24
    assert summary["rows"] > 1000
    assert len(summary["regions"]) >= 3
    assert len(summary["categories"]) >= 3
    assert payload["source"]["tables"][0]["name"] == "sales_monthly"

    bootstrap = client.get("/api/bootstrap").get_json()
    assert payload["source"]["id"] in bootstrap["active_session"]["source_ids"]
    assert any(
        item.get("sample_seed", {}).get("id") == SAMPLE_SEED_ID for item in bootstrap["sources"]
    )
    metrics = client.get("/api/semantic/metrics").get_json()["items"]
    approved = {item["name"] for item in metrics if item.get("status") == "approved"}
    assert {"sales_amount", "order_count", "average_order_value"} <= approved
    knowledge = client.get("/api/knowledge/entries").get_json()["items"]
    assert knowledge
    assert any(item.get("type") == "business_rule" for item in knowledge)
    assert any(
        item.get("id") == "agent-superskill"
        for item in client.get("/api/agents").get_json()["items"]
    )

    repeated = client.post("/api/demo/seed", json={"workspace_id": "default"})
    assert repeated.status_code == 200
    assert repeated.get_json()["created"] == []


def test_demo_questions_are_not_suggested_without_an_authorized_demo_source(client, app):
    from backend.services.demo_sales import sample_questions

    assert client.get("/api/bootstrap").get_json()["recommended_questions"] == []
    seeded = client.post("/api/demo/seed", json={"workspace_id": "default"}).get_json()
    assert client.get("/api/bootstrap").get_json()["recommended_questions"] == sample_questions()[:4]

    database = app.extensions["meridian_db"]
    database.patch(
        "sources", seeded["source"]["id"],
        {"authorized_user_ids": ["another-user"]}, workspace_id="default",
    )
    bootstrap = client.get("/api/bootstrap").get_json()
    assert bootstrap["recommended_questions"] == []
    assert all(
        not agent["suggested_questions"]
        for agent in bootstrap["agents"] if agent["id"] == "agent-superskill"
    )


def test_repeated_demo_seed_summarizes_the_stored_dataset(client, monkeypatch):
    from datetime import date

    from backend.services import demo_sales

    build_frame = demo_sales.build_frame
    monkeypatch.setattr(demo_sales, "build_frame", lambda: build_frame(date(2026, 9, 1)))
    first = client.post("/api/demo/seed", json={"workspace_id": "default"}).get_json()
    monkeypatch.setattr(demo_sales, "build_frame", lambda: build_frame(date(2026, 10, 1)))
    repeated = client.post("/api/demo/seed", json={"workspace_id": "default"}).get_json()

    assert repeated["created"] == []
    assert repeated["summary"] == first["summary"]


def test_demo_seed_does_not_attach_source_to_another_users_session(client, app):
    database = app.extensions["meridian_db"]
    other = database.put(
        "sessions", {
            "id": database.new_id("ses"), "workspace_id": "default",
            "owner_id": "another-user", "status": "active", "source_ids": [],
        }, workspace_id="default",
    )

    seeded = client.post("/api/demo/seed", json={"workspace_id": "default"}).get_json()
    stored = database.get("sessions", other["id"], workspace_id="default")
    assert seeded["source"]["id"] not in stored["source_ids"]
