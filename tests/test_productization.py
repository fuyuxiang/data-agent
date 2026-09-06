from __future__ import annotations

from backend.services.saas import DEFAULT_SUBSCRIPTION_ID


def test_bootstrap_exposes_saas_product_control_plane(client):
    bootstrap = client.get("/api/bootstrap").get_json()

    assert bootstrap["ok"] is True
    assert bootstrap["active_workspace"]["tenant_id"] == "tenant_default"
    assert bootstrap["entitlements"]["plan"]["id"] == "enterprise"
    assert bootstrap["product"]["methodology"]
    assert bootstrap["onboarding"]["next_step"]["id"] == "connect_data"

    plans = client.get("/api/product/plans").get_json()["items"]
    assert {item["id"] for item in plans} == {"starter", "growth", "enterprise"}

    solutions = client.get("/api/product/solutions").get_json()["items"]
    assert {item["id"] for item in solutions} >= {
        "decision-intelligence", "metric-governance", "report-factory",
    }


def test_demo_seed_integrates_source_knowledge_semantic_and_session(client):
    seeded = client.post("/api/onboarding/demo", json={"workspace_id": "default"})

    assert seeded.status_code == 201, seeded.get_json()
    payload = seeded.get_json()
    assert payload["source"]["name"] == "即时零售 10 城经营样例"
    assert "path" not in payload["source"]
    assert {"source", "knowledge_entry", "semantic_model", "semantic_metric"} & set(payload["created"])

    entries = client.get("/api/knowledge/entries").get_json()["items"]
    assert {item["name"] for item in entries} >= {"活跃合作商家数", "补贴效率诊断规则"}

    metrics = client.get("/api/semantic/metrics").get_json()["items"]
    approved = [item for item in metrics if item["name"] == "active_merchants_total"]
    assert approved and approved[0]["status"] == "approved"

    bootstrap = client.get("/api/bootstrap").get_json()
    session = bootstrap["active_session"]
    assert payload["source"]["id"] in session.get("source_ids", [])
    completed_steps = {
        item["id"] for item in bootstrap["onboarding"]["steps"]
        if item["done"]
    }
    assert completed_steps >= {"connect_data", "define_context", "approve_metrics"}

    source_count = len(client.get("/api/sources").get_json()["items"])
    repeated = client.post("/api/onboarding/demo", json={"workspace_id": "default"}).get_json()
    assert len(client.get("/api/sources").get_json()["items"]) == source_count
    assert repeated["source"]["id"] == payload["source"]["id"]


def test_plan_entitlements_are_enforced_by_backend(app, client):
    database = app.extensions["meridian_db"]
    database.patch("subscriptions", DEFAULT_SUBSCRIPTION_ID, {"plan_id": "starter"})

    bootstrap = client.get("/api/bootstrap").get_json()
    assert bootstrap["entitlements"]["plan"]["id"] == "starter"
    assert bootstrap["capabilities"]["workflows"] is False

    workflow = client.post(
        "/api/workflows",
        json={"workspace_id": "default", "name": "团队版不可创建自动化", "definition": {"steps": []}},
    )
    assert workflow.status_code == 403
    assert "automation" in workflow.get_json()["error"]

    workspace = client.post("/api/workspaces", json={"name": "第二工作空间"})
    assert workspace.status_code == 403
    assert "workspaces 上限" in workspace.get_json()["error"]


def test_system_owner_can_update_commercial_subscription(client):
    updated = client.patch(
        "/api/product/subscription",
        json={
            "workspace_id": "default",
            "tenant_name": "演示客户",
            "plan_id": "growth",
            "status": "active",
            "trial": False,
            "period_days": 90,
        },
    )

    assert updated.status_code == 200, updated.get_json()
    product = updated.get_json()["product"]
    assert product["entitlements"]["tenant_name"] == "演示客户"
    assert product["entitlements"]["plan"]["id"] == "growth"
    assert product["entitlements"]["subscription"]["status"] == "active"
    assert product["entitlements"]["subscription"]["trial"] is False

    bootstrap = client.get("/api/bootstrap").get_json()
    assert bootstrap["capabilities"]["workflows"] is True
    assert bootstrap["capabilities"]["mcp"] is True
