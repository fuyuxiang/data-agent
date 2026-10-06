from __future__ import annotations


def _model(client, source):
    response = client.post("/api/semantic/models", json={
        "name": "sales_model",
        "description": "销售事实语义模型",
        "source_id": source["id"],
        "table": "sales",
        "grain": "每行为一个区域月度记录",
        "entities": [],
        "dimensions": [
            {"name": "region", "column": "region", "type": "categorical", "label": "区域"},
            {"name": "month", "column": "month", "type": "time", "label": "月份"},
        ],
        "measures": [
            {"name": "sales_amount", "column": "sales", "aggregation": "sum", "label": "销售额"},
            {"name": "row_count", "column": "*", "aggregation": "count", "label": "记录数"},
        ],
        "default_time_dimension": "month",
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()["item"]


def test_approved_metric_compiles_and_executes_deterministically(client, source):
    model = _model(client, source)
    metric = client.post("/api/semantic/metrics", json={
        "name": "total_sales", "label": "销售额", "aliases": ["营收"],
        "description": "销售额合计", "model_id": model["id"],
        "measure": "sales_amount", "status": "approved", "unit": "元",
    })
    assert metric.status_code == 201, metric.get_json()

    response = client.post("/api/semantic/query", json={
        "metric": "营收", "group_by": ["region"],
        "order_by": [{"field": "total_sales", "direction": "desc"}],
    })
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["plan"]["metric"]["version"] == 1
    assert payload["result"]["semantic_query"]["metric_name"] == "total_sales"
    assert payload["result"]["data"] == [
        {"region": "North", "total_sales": 440.0},
        {"region": "South", "total_sales": 305.0},
    ]


def test_metric_filters_are_structured_and_draft_metrics_do_not_publish(client, source):
    model = _model(client, source)
    created = client.post("/api/semantic/metrics", json={
        "name": "draft_sales", "model_id": model["id"],
        "measure": "sales_amount", "status": "draft",
    })
    assert created.status_code == 201
    assert client.post("/api/semantic/query", json={"metric": "draft_sales"}).status_code == 403

    approved = client.post("/api/semantic/metrics", json={
        "name": "safe_sales", "model_id": model["id"],
        "measure": "sales_amount", "status": "approved",
    })
    assert approved.status_code == 201
    injected = client.post("/api/semantic/query", json={
        "metric": "safe_sales",
        "filters": [{"dimension": "region", "op": "=", "value": "North' OR 1=1 --"}],
    })
    assert injected.status_code == 200, injected.get_json()
    assert injected.get_json()["result"]["rows"] == 1
    assert injected.get_json()["result"]["data"][0]["safe_sales"] is None

    invalid = client.post("/api/semantic/compile", json={
        "metric": "safe_sales", "group_by": ["not_a_dimension"],
    })
    assert invalid.status_code == 400


def test_semantic_versions_are_immutable_and_model_changes_revoke_approval(app, client, source):
    model = _model(client, source)
    metric = client.post("/api/semantic/metrics", json={
        "name": "record_count", "label": "记录数", "model_id": model["id"],
        "measure": "row_count", "status": "approved",
    }).get_json()["item"]
    counted = client.post("/api/semantic/query", json={"metric": metric["id"]})
    assert counted.status_code == 200
    assert counted.get_json()["result"]["data"] == [{"record_count": 6}]

    measures = [dict(item) for item in model["measures"]]
    measures[0]["aggregation"] = "avg"
    changed = client.patch(f"/api/semantic/models/{model['id']}", json={"measures": measures})
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()["item"]["version"] == 2
    assert client.post("/api/semantic/query", json={"metric": metric["id"]}).status_code == 403

    database = app.extensions["meridian_db"]
    assert len(database.list("semantic_model_versions", workspace_id="default")) == 2
    metric_versions = database.list("semantic_metric_versions", workspace_id="default")
    assert {item["version"]: item["status"] for item in metric_versions} == {1: "approved", 2: "draft"}


def test_semantic_model_requires_deleting_referencing_metrics_first(client, source):
    model = _model(client, source)
    metric_response = client.post("/api/semantic/metrics", json={
        "name": "model_delete_guard", "model_id": model["id"],
        "measure": "sales_amount", "status": "draft",
    })
    assert metric_response.status_code == 201
    metric = metric_response.get_json()["item"]

    blocked = client.delete(f"/api/semantic/models/{model['id']}")
    assert blocked.status_code == 400
    assert "指标引用" in blocked.get_json()["error"]

    assert client.delete(f"/api/semantic/metrics/{metric['id']}").status_code == 200
    assert client.delete(f"/api/semantic/models/{model['id']}").status_code == 200
    assert model["id"] not in {
        item["id"] for item in client.get("/api/semantic/models").get_json()["items"]
    }


def test_disabled_model_references_are_visible_to_delete_dialog(client, source):
    model = _model(client, source)
    metric = client.post("/api/semantic/metrics", json={
        "name": "disabled_model_metric", "model_id": model["id"],
        "measure": "sales_amount", "status": "draft",
    }).get_json()["item"]
    assert client.patch(f"/api/semantic/models/{model['id']}", json={"enabled": False}).status_code == 200
    assert metric["id"] not in {
        item["id"] for item in client.get("/api/semantic/metrics").get_json()["items"]
    }

    references = client.get(f"/api/semantic/models/{model['id']}/references")
    assert references.status_code == 200
    assert references.get_json()["metrics"] == [{
        "id": metric["id"], "name": "disabled_model_metric", "status": "draft",
    }]
    assert client.delete(f"/api/semantic/models/{model['id']}").status_code == 400


def test_metric_delete_requires_removing_metric_and_agent_references(app, client, source):
    model = _model(client, source)
    base = client.post("/api/semantic/metrics", json={
        "name": "base_sales", "model_id": model["id"],
        "measure": "sales_amount", "status": "approved",
    }).get_json()["item"]
    derived_response = client.post("/api/semantic/metrics", json={
        "name": "double_sales", "model_id": model["id"], "metric_type": "derived",
        "expression": "base_sales * 2", "status": "approved",
    })
    assert derived_response.status_code == 201, derived_response.get_json()
    derived = derived_response.get_json()["item"]
    assert client.post("/api/semantic/compile", json={"metric": derived["id"]}).status_code == 200

    database = app.extensions["meridian_db"]
    database.put("agent_definitions", {
        "id": "agent-using-metric", "workspace_id": "default", "name": "业绩助手",
        "status": "published", "metric_ids": [base["id"]],
    }, workspace_id="default")
    references = client.get(f"/api/semantic/metrics/{base['id']}/references")
    assert references.status_code == 200
    assert [item["id"] for item in references.get_json()["metrics"]] == [derived["id"]]
    assert [item["id"] for item in references.get_json()["agents"]] == ["agent-using-metric"]
    assert client.delete(f"/api/semantic/metrics/{base['id']}").status_code == 400
    assert client.post("/api/semantic/compile", json={"metric": derived["id"]}).status_code == 200

    assert client.delete(f"/api/semantic/metrics/{derived['id']}").status_code == 200
    assert client.delete(f"/api/semantic/metrics/{base['id']}").status_code == 400
    database.patch("agent_definitions", "agent-using-metric", {"metric_ids": []}, workspace_id="default")
    assert client.delete(f"/api/semantic/metrics/{base['id']}").status_code == 200
