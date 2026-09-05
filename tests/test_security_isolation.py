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


def test_outbound_sources_block_private_networks(client):
    response = client.post(
        "/api/sources/http",
        json={"name": "metadata", "url": "http://127.0.0.1:8080/private"},
    )
    assert response.status_code == 400
    assert "禁止" in response.get_json()["error"]
