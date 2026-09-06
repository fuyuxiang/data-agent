from __future__ import annotations

import io


def test_authenticated_workspaces_enforce_membership_and_roles(app):
    owner = app.test_client()
    member = app.test_client()
    anonymous = app.test_client()

    registered = owner.post(
        "/api/auth/register",
        json={"email": "owner@example.com", "password": "correct-horse", "name": "Owner"},
    )
    assert registered.status_code == 201
    workspace = owner.post("/api/workspaces", json={"name": "私有空间"}).get_json()["item"]
    upload = owner.post(
        "/api/sources/upload",
        data={
            "workspace_id": workspace["id"],
            "file": (io.BytesIO(b"name,value\nA,1\nB,2\n"), "private.csv"),
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 201
    source = upload.get_json()["items"][0]

    second = member.post(
        "/api/auth/register",
        json={"email": "analyst@example.com", "password": "correct-horse", "name": "Analyst"},
    )
    assert second.status_code == 201
    denied = member.get(
        f"/api/sources/{source['id']}", headers={"X-Workspace-Id": workspace["id"]},
    )
    assert denied.status_code == 403
    assert anonymous.get("/api/bootstrap").status_code == 401

    invited = owner.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"email": "analyst@example.com", "role": "viewer"},
    )
    assert invited.status_code == 201
    assert member.post(f"/api/workspaces/{workspace['id']}/activate").status_code == 200
    assert member.get(f"/api/sources/{source['id']}").status_code == 200
    readonly = member.post(
        "/api/query", json={"source_ids": [source["id"]], "sql": "SELECT * FROM data"},
    )
    assert readonly.status_code == 403

    user_id = second.get_json()["user"]["id"]
    promoted = owner.patch(
        f"/api/workspaces/{workspace['id']}/members/{user_id}", json={"role": "editor"},
    )
    assert promoted.status_code == 200
    query = member.post(
        "/api/query", json={"source_ids": [source["id"]], "sql": "SELECT * FROM data"},
    )
    assert query.status_code == 200
    result_id = query.get_json()["result"]["id"]
    model_response = member.post(
        "/api/semantic/models",
        json={
            "name": "private_source_model", "source_id": source["id"], "table": "data",
            "dimensions": [{"name": "name", "column": "name", "type": "categorical"}],
            "measures": [{"name": "value", "column": "value", "aggregation": "sum"}],
        },
    )
    assert model_response.status_code == 201, model_response.get_json()
    model = model_response.get_json()["item"]
    metric_response = member.post(
        "/api/semantic/metrics",
        json={
            "name": "private_value", "model_id": model["id"],
            "measure": "value", "status": "draft",
        },
    )
    assert metric_response.status_code == 201
    metric = metric_response.get_json()["item"]
    assert owner.patch(
        f"/api/semantic/metrics/{metric['id']}",
        json={"status": "approved", "workspace_id": workspace["id"]},
    ).status_code == 200
    assert member.patch(
        f"/api/semantic/metrics/{metric['id']}", json={"status": "draft"},
    ).status_code == 403
    assert member.patch(
        f"/api/semantic/models/{model['id']}", json={"description": "unauthorized change"},
    ).status_code == 403
    dashboard = member.post(
        "/api/dashboards",
        json={"name": "成员看板", "widgets": [{"id": "w1", "result_id": result_id}]},
    ).get_json()["item"]
    workflow_response = member.post(
        "/api/workflows",
        json={
            "name": "成员工作流",
            "definition": {
                "steps": [{
                    "id": "query", "type": "query", "depends_on": [],
                    "config": {"source_ids": [source["id"]], "sql": "SELECT * FROM data"},
                }],
            },
        },
    )
    assert workflow_response.status_code == 201
    workflow = workflow_response.get_json()["item"]
    artifact = member.post(
        "/api/exports/data", json={"result_id": result_id, "format": "csv"},
    ).get_json()["artifact"]

    assert member.get(f"/api/workspaces/{workspace['id']}/storage").status_code == 403
    assert member.get("/api/audit").status_code == 403
    assert member.get("/api/usage").status_code == 403
    assert member.get("/api/hooks").status_code == 403

    restricted = owner.patch(
        f"/api/sources/{source['id']}",
        json={"authorized_user_ids": [registered.get_json()["user"]["id"]]},
        headers={"X-Workspace-Id": workspace["id"]},
    )
    assert restricted.status_code == 200
    assert all(item["id"] != source["id"] for item in member.get("/api/sources").get_json()["items"])
    assert member.get(f"/api/sources/{source['id']}").status_code == 403
    assert member.get(f"/api/sources/{source['id']}/schema").status_code == 403
    assert member.get(f"/api/sources/{source['id']}/preview").status_code == 403
    assert member.get(f"/api/sources/{source['id']}/profile").status_code == 403
    assert member.post(
        "/api/query", json={"source_ids": [source["id"]], "sql": "SELECT * FROM data"},
    ).status_code == 403
    assert member.post(
        "/api/analysis/run", json={"source_id": source["id"], "method": "profile"},
    ).status_code == 403
    assert member.get(f"/api/query-results/{query.get_json()['result']['id']}").status_code == 403
    assert member.get(f"/api/dashboards/{dashboard['id']}").status_code == 403
    assert dashboard["id"] not in {
        item["id"] for item in member.get("/api/dashboards").get_json()["items"]
    }
    assert workflow["id"] not in {
        item["id"] for item in member.get("/api/workflows").get_json()["items"]
    }
    assert member.get(f"/api/workflows/{workflow['id']}").status_code == 404
    assert member.get(f"/api/artifacts/{artifact['id']}/download").status_code == 403

    derived = owner.post(
        f"/api/sources/{source['id']}/clean/apply",
        json={"operations": [{"type": "drop_duplicates"}]},
        headers={"X-Workspace-Id": workspace["id"]},
    )
    assert derived.status_code == 201, derived.get_json()
    derived_id = derived.get_json()["item"]["id"]
    assert member.get(f"/api/sources/{derived_id}").status_code == 403


def test_query_gateway_enforces_catalog_table_scope(client, source):
    denied = client.post(
        "/api/query", json={"source_ids": [source["id"]], "sql": "SELECT * FROM hidden_table"},
    )
    assert denied.status_code == 403
    assert "未授权数据表" in denied.get_json()["error"]

    cte = client.post(
        "/api/query",
        json={
            "source_ids": [source["id"]],
            "sql": "WITH totals AS (SELECT SUM(sales) AS amount FROM data) SELECT amount FROM totals",
        },
    )
    assert cte.status_code == 200


def test_outbound_sources_block_private_networks(client):
    response = client.post(
        "/api/sources/http",
        json={"name": "metadata", "url": "http://127.0.0.1:8080/private"},
    )
    assert response.status_code == 400
    assert "禁止" in response.get_json()["error"]


def test_same_workspace_users_cannot_read_private_sessions_or_snapshots(app):
    owner = app.test_client()
    analyst = app.test_client()
    owner_user = owner.post(
        "/api/auth/register",
        json={"email": "session-owner@example.com", "password": "correct-horse", "name": "Owner"},
    ).get_json()["user"]
    workspace = owner.post("/api/workspaces", json={"name": "Session isolation"}).get_json()["item"]
    headers = {"X-Workspace-Id": workspace["id"]}
    private_session = owner.post(
        "/api/sessions", json={"name": "Owner private", "workspace_id": workspace["id"]},
    ).get_json()["item"]
    saved = owner.post(
        f"/api/sessions/{private_session['id']}/save",
        json={"name": "private snapshot", "workspace_id": workspace["id"]},
    ).get_json()["item"]
    database = app.extensions["meridian_db"]
    private_job = database.put(
        "jobs",
        {
            "id": database.new_id("job"), "workspace_id": workspace["id"],
            "session_id": private_session["id"], "status": "completed",
            "result": {"secret": "owner-only"},
        },
        workspace_id=workspace["id"],
    )

    analyst_user = analyst.post(
        "/api/auth/register",
        json={"email": "session-analyst@example.com", "password": "correct-horse", "name": "Analyst"},
    ).get_json()["user"]
    owner.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"email": analyst_user["email"], "role": "editor"},
    )
    assert analyst.post(f"/api/workspaces/{workspace['id']}/activate").status_code == 200

    assert analyst.get("/api/sessions", headers=headers).get_json()["items"] == []
    assert analyst.get("/api/bootstrap", headers=headers).get_json()["sessions"] == []
    assert analyst.get(f"/api/sessions/{private_session['id']}/messages", headers=headers).status_code == 404
    assert analyst.get("/api/saved-sessions", headers=headers).get_json()["items"] == []
    assert analyst.get("/api/jobs", headers=headers).get_json()["items"] == []
    assert analyst.get(f"/api/jobs/{private_job['id']}", headers=headers).status_code == 404
    assert analyst.post(
        f"/api/saved-sessions/{saved['id']}/load", json={"workspace_id": workspace["id"]},
    ).status_code == 404

    own_session = analyst.post(
        "/api/sessions", json={"name": "Analyst private", "workspace_id": workspace["id"]},
    ).get_json()["item"]
    assert own_session["owner_id"] == analyst_user["id"]
    assert owner.get(f"/api/sessions/{own_session['id']}/messages", headers=headers).status_code == 404
    assert private_session["owner_id"] == owner_user["id"]
