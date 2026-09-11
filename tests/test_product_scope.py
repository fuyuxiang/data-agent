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
    ):
        assert client.get(path).status_code == 404


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
