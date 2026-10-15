"""Paginated analysis history must not silently stop at the first page."""

from backend.agent.store import RunStore


def test_analysis_history_returns_stable_pages_and_exhaustion(app, client):
    session = client.post("/api/sessions", json={"title": "分页会话"}).get_json()["item"]
    store = RunStore(app.extensions["meridian_db"])
    runs = [store.create_run(
        workspace_id="default", session_id=session["id"], actor_id="local-default",
        source_scope=[], allowed_tool_ids=[], require_session=True,
    )[0] for _ in range(5)]
    url = f"/api/analyses?session_id={session['id']}&limit=2"
    first = client.get(url).get_json()
    second = client.get(f"{url}&offset={first['next_offset']}").get_json()
    last = client.get(f"{url}&offset={second['next_offset']}").get_json()
    ids = [run["id"] for page in (first, second, last) for run in page["items"]]
    assert ids == [run["id"] for run in reversed(runs)]
    assert len(ids) == len(set(ids))
    assert first["has_more"] is True and first["next_offset"] == 2
    assert second["has_more"] is True and second["next_offset"] == 4
    assert last["has_more"] is False and last["next_offset"] is None


def test_analysis_history_rejects_invalid_page_arguments(client):
    assert client.get("/api/analyses?offset=invalid").status_code == 400
    assert client.get("/api/analyses?limit=invalid").status_code == 400


def test_history_cursor_survives_deleting_its_boundary_and_adding_new_runs(app, client):
    session = client.post("/api/sessions", json={"name": "分页变更"}).get_json()["item"]
    store = RunStore(app.extensions["meridian_db"])
    runs = [store.create_run(
        workspace_id="default", session_id=session["id"], actor_id="local-default",
        source_scope=[], allowed_tool_ids=[], require_session=True,
    )[0] for _ in range(51)]
    url = f"/api/analyses?session_id={session['id']}&limit=50"
    first = client.get(url).get_json()
    assert first["next_before"] == first["items"][-1]["history_cursor"]
    boundary = first["items"][-1]
    store.update_status(boundary["id"], "failed")
    assert client.delete(f"/api/analyses/{boundary['id']}").status_code == 200
    store.create_run(workspace_id="default", session_id=session["id"], actor_id="local-default",
                     source_scope=[], allowed_tool_ids=[], require_session=True)
    older = client.get(f"{url}&before={first['next_before']}").get_json()
    assert [run["id"] for run in older["items"]] == [runs[0]["id"]]
    assert older["has_more"] is False
    assert older["next_before"] is None
    other = client.post("/api/sessions", json={"name": "其他会话"}).get_json()["item"]
    assert client.get(f"/api/analyses?session_id={other['id']}&before={first['next_before']}").status_code == 400
    for cursor in ("invalid", "0", "-1"):
        assert client.get(f"{url}&before={cursor}").status_code == 400
