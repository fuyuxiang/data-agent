from __future__ import annotations


def test_history_search_pages_all_visible_sessions_beyond_recent_window(app, client):
    database = app.extensions["meridian_db"]
    for index in range(65):
        database.put("sessions", {
            "id": f"history_{index:03}", "name": f"经营复盘 {index:03}", "owner_id": "local-default",
        }, workspace_id="default")
    archived = database.put("sessions", {
        "id": "history_deleted", "name": "经营复盘 已删除", "owner_id": "local-default",
    }, workspace_id="default")
    database.archive("sessions", archived["id"])
    database.put("sessions", {
        "id": "history_other_workspace", "name": "经营复盘 外部", "owner_id": "local-default",
    }, workspace_id="another")

    ids = []
    for page in range(1, 4):
        response = client.get(f"/api/sessions?q=经营复盘&page={page}&limit=30")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["total"] == 65
        assert payload["has_more"] is (page < 3)
        ids.extend(item["id"] for item in payload["items"])
    assert len(ids) == len(set(ids)) == 65
    exact = client.get("/api/sessions?q=经营复盘 000&limit=30").get_json()
    assert [item["id"] for item in exact["items"]] == ["history_000"]
    # Wildcards are literal text, not SQL patterns or query fragments.
    assert client.get("/api/sessions?q=%25&limit=30").get_json()["total"] == 0
    for suffix in ("page=0", "page=oops", "limit=501", "limit=-1"):
        assert client.get(f"/api/sessions?{suffix}").status_code == 400


def test_history_limits_after_permission_filtering_and_hides_private_counts(app):
    owner, member = app.test_client(), app.test_client()
    owner_user = owner.post("/api/auth/register", json={
        "email": "history-owner@example.com", "password": "history-password",
    }).get_json()["user"]
    member_user = member.post("/api/auth/register", json={
        "email": "history-member@example.com", "password": "history-password",
    }).get_json()["user"]
    database = app.extensions["meridian_db"]
    database.put("workspace_members", {
        "id": f"default:{member_user['id']}", "user_id": member_user["id"], "role": "viewer", "enabled": True,
    }, workspace_id="default")
    database.put("sessions", {
        "id": "history_owned_old", "name": "可见历史 旧记录", "owner_id": member_user["id"],
    }, workspace_id="default")
    with database.transaction() as connection:
        connection.execute("UPDATE records SET updated_at='2000-01-01' WHERE id='history_owned_old'")
    for index in range(505):
        database.put("sessions", {
            "id": f"history_private_{index:03}", "name": "可见历史 他人私有", "owner_id": owner_user["id"],
        }, workspace_id="default")
    database.put("sessions", {
        "id": "history_shared", "name": "可见历史 共享", "visibility": "workspace", "owner_id": owner_user["id"],
    }, workspace_id="default")
    database.put("sessions", {"id": "history_legacy", "name": "可见历史 旧版无归属"}, workspace_id="default")
    headers = {"X-Workspace-Id": "default"}
    result = member.get("/api/sessions?q=可见历史&limit=30", headers=headers).get_json()
    assert result["total"] == 2
    assert {item["id"] for item in result["items"]} == {"history_owned_old", "history_shared"}
    bootstrap = member.get("/api/bootstrap", headers=headers).get_json()
    assert "history_owned_old" in {item["id"] for item in bootstrap["sessions"]}
    assert member.get("/api/sessions/history_owned_old", headers=headers).status_code == 200
    assert member.get("/api/sessions/history_legacy", headers=headers).status_code == 404
    assert member.get("/api/sessions/history_private_000", headers=headers).status_code == 404
    legacy = owner.get("/api/sessions?q=旧版无归属", headers=headers).get_json()
    assert [item["id"] for item in legacy["items"]] == ["history_legacy"]


def test_new_session_keeps_other_users_current_conversation_untouched(app, client):
    database = app.extensions["meridian_db"]
    own = client.post("/api/sessions", json={"name": "本人当前会话"}).get_json()["item"]
    foreign = database.put("sessions", {
        "id": "foreign_active", "name": "他人当前会话", "owner_id": "another-user", "status": "active",
    }, workspace_id="default")
    assert client.post("/api/sessions", json={"name": "本人新会话"}).status_code == 201
    assert database.get("sessions", own["id"])["status"] == "idle"
    assert database.get("sessions", foreign["id"]) == foreign


def test_long_conversation_keeps_latest_messages_and_pages_older_history(app, client):
    database = app.extensions["meridian_db"]
    session = client.post("/api/sessions", json={"name": "超过两百条消息"}).get_json()["item"]
    for index in range(525):
        database.add_message(session["id"], "user" if index % 2 == 0 else "assistant", f"消息 {index:03}")
    newest = client.get(f"/api/sessions/{session['id']}").get_json()
    assert len(newest["messages"]) == 200
    assert newest["messages"][0]["content"] == "消息 325"
    assert newest["messages"][-1]["content"] == "消息 524"
    # Agent history uses the latest 500 messages, not the first 500 forever.
    assert database.messages(session["id"], 500)[-1]["content"] == "消息 524"
    assert database.messages(session["id"], 500)[0]["content"] == "消息 025"
    cursor = newest["message_page"]["next_before"]
    pages = [newest["messages"]]
    while cursor:
        older = client.get(f"/api/sessions/{session['id']}?before={cursor}").get_json()
        pages.insert(0, older["messages"])
        cursor = older["message_page"]["next_before"]
    flattened = [item["content"] for page in pages for item in page]
    assert flattened == [f"消息 {index:03}" for index in range(525)]
    another = client.post("/api/sessions", json={"name": "其他会话"}).get_json()["item"]
    assert client.get(f"/api/sessions/{another['id']}?before={newest['message_page']['next_before']}").status_code == 400
    for suffix in ("message_limit=0", "message_limit=1001", "before=0", "before=invalid"):
        assert client.get(f"/api/sessions/{session['id']}?{suffix}").status_code == 400
