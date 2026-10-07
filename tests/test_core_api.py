from __future__ import annotations

import io

import pytest

import pandas as pd


def test_bootstrap_and_capability_catalog(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.get_json()["database"] == "ready"

    bootstrap = client.get("/api/bootstrap").get_json()
    assert bootstrap["ok"] is True
    assert bootstrap["active_workspace"]["id"] == "default"
    assert bootstrap["active_session"]

    assert client.get("/api/analyses").status_code == 200
    assert client.get("/api/agents").status_code == 200


def test_analysis_session_can_be_renamed_and_archived(client):
    created = client.post("/api/sessions", json={"name": "待整理分析", "source_ids": []})
    assert created.status_code == 201
    session_id = created.get_json()["item"]["id"]

    renamed = client.patch(f"/api/sessions/{session_id}", json={"name": "九月经营分析"})
    assert renamed.status_code == 200
    assert renamed.get_json()["item"]["name"] == "九月经营分析"

    archived = client.delete(f"/api/sessions/{session_id}")
    assert archived.status_code == 200
    assert archived.get_json()["archived"] is True
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_analysis_can_be_cancelled_and_archived_individually(client):
    session = client.post("/api/sessions", json={"name": "单条分析操作"}).get_json()["item"]
    created = client.post("/api/analyses", json={
        "session_id": session["id"], "objective": "等待确认的分析",
    })
    assert created.status_code == 201
    run = created.get_json()["item"]

    cancelled = client.post(f"/api/analyses/{run['id']}/control", json={"action": "cancel"})
    assert cancelled.status_code == 200
    assert cancelled.get_json()["item"]["execution_status"] == "cancelled"

    archived = client.delete(f"/api/analyses/{run['id']}")
    assert archived.status_code == 200
    assert archived.get_json()["archived"] is True
    assert client.get(f"/api/analyses/{run['id']}").status_code == 404
    assert run["id"] not in {
        item["id"] for item in client.get(f"/api/analyses?session_id={session['id']}").get_json()["items"]
    }
    messages = client.get(f"/api/sessions/{session['id']}").get_json()["messages"]
    assert all(item.get("metadata", {}).get("run_id") != run["id"] for item in messages)


def test_session_source_scope_prunes_missing_sources(client, source):
    created = client.post(
        "/api/sessions",
        json={"name": "范围清理", "source_ids": ["src_missing_history", source["id"]]},
    )
    assert created.status_code == 201
    session = created.get_json()["item"]
    assert session["source_ids"] == [source["id"]]

    patched = client.patch(
        f"/api/sessions/{session['id']}",
        json={"source_ids": ["src_deleted_in_other_tab", source["id"]]},
    )
    assert patched.status_code == 200
    assert patched.get_json()["item"]["source_ids"] == [source["id"]]


def test_explicit_analysis_sources_replace_legacy_business_space(client, app, source):
    database = app.extensions["meridian_db"]
    space = database.put("business_spaces", {
        "id": "space_scope_regression", "workspace_id": "default",
        "name": "旧数据空间", "status": "published", "source_ids": [source["id"]],
    }, workspace_id="default")
    assert client.post("/api/sessions", json={"business_space_id": space["id"]}).status_code == 400
    created = client.post("/api/sessions", json={"source_ids": [source["id"]]})
    assert created.status_code == 201
    session = created.get_json()["item"]
    database.patch("sessions", session["id"], {"business_space_id": space["id"]}, workspace_id="default")
    assert session["source_ids"] == [source["id"]]

    # An explicit empty selection must never silently restore the space's sources.
    run = client.post("/api/analyses", json={
        "session_id": session["id"], "objective": "仅核对本次显式范围", "source_ids": [],
    })
    assert run.status_code == 201
    assert run.get_json()["item"]["source_scope"] == []
    refreshed = client.get(f"/api/sessions/{session['id']}").get_json()["item"]
    assert refreshed["source_ids"] == []
    assert refreshed["business_space_id"] is None

    patched = client.patch(f"/api/sessions/{session['id']}", json={"source_ids": [source["id"]]})
    assert patched.status_code == 200
    assert patched.get_json()["item"]["business_space_id"] is None
    another = client.post("/api/analyses", json={
        "session_id": session["id"], "objective": "重新选择数据源", "source_ids": [source["id"]],
    })
    assert another.status_code == 201
    assert another.get_json()["item"]["source_scope"] == [source["id"]]

    followup = client.post(f"/api/analyses/{another.get_json()['item']['id']}/branch", json={
        "mode": "followup", "prompt": "继续核对相同数据",
    })
    assert followup.status_code == 201
    assert followup.get_json()["item"]["source_scope"] == [source["id"]]

    other = client.post("/api/sources/upload", data={
        "file": (io.BytesIO(b"region,value\nEast,4\n"), "other.csv"),
        "workspace_id": "default",
    }, content_type="multipart/form-data").get_json()["items"][0]
    agent = client.post("/api/agents", json={
        "name": "指定来源智能体", "source_ids": [other["id"]],
    }).get_json()["item"]
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    selected = client.patch(f"/api/sessions/{session['id']}", json={"source_ids": [other["id"]]})
    assert selected.get_json()["item"]["business_space_id"] is None
    agent_run = client.post("/api/analyses", json={
        "session_id": session["id"], "objective": "核对新来源", "source_ids": [other["id"]],
        "agent_id": agent["id"],
    })
    assert agent_run.status_code == 201
    assert agent_run.get_json()["item"]["source_scope"] == [other["id"]]


def test_archiving_source_removes_it_from_session_scope(client, source):
    created = client.post(
        "/api/sessions",
        json={"name": "待清理范围", "source_ids": [source["id"]]},
    )
    assert created.status_code == 201
    session_id = created.get_json()["item"]["id"]

    archived = client.delete(f"/api/sources/{source['id']}")
    assert archived.status_code == 200
    assert archived.get_json()["cleaned"]["sessions"] >= 1

    session = client.get(f"/api/sessions/{session_id}").get_json()["item"]
    assert session["source_ids"] == []


def test_source_query_profile_clean_and_guard(client, source):
    source_id = source["id"]
    assert client.get(f"/api/sources/{source_id}/schema").status_code == 200
    profile = client.get(f"/api/sources/{source_id}/profile").get_json()["profile"]
    assert profile["rows"] == 6
    assert profile["quality_score"] == 100

    query = client.post(
        "/api/query",
        json={"source_ids": [source_id], "sql": "SELECT region, SUM(sales) AS sales FROM sales GROUP BY region ORDER BY sales DESC"},
    )
    assert query.status_code == 200
    result = query.get_json()["result"]
    assert result["rows"] == 2
    assert result["data"][0]["region"] == "North"

    blocked = client.post("/api/query", json={"source_ids": [source_id], "sql": "DROP TABLE sales"})
    assert blocked.status_code == 400

    file_escape = client.post(
        "/api/query",
        json={"source_ids": [source_id], "sql": "SELECT * FROM read_csv('/tmp/private.csv')"},
    )
    assert file_escape.status_code == 400

    literal_keyword = client.post(
        "/api/query",
        json={"source_ids": [source_id], "sql": "SELECT 'please DELETE later' AS note FROM sales LIMIT 1"},
    )
    assert literal_keyword.status_code == 200

    cleaned = client.post(
        f"/api/sources/{source_id}/clean/apply",
        json={"operations": [{"type": "trim_text"}, {"type": "drop_duplicates"}]},
    )
    assert cleaned.status_code == 201
    assert cleaned.get_json()["item"]["parent_source_id"] == source_id


def test_query_ignores_empty_placeholder_workbook_sheets(client):
    workbook = io.BytesIO()
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame({"学号": [1, 2, 3], "姓名": ["甲", "乙", "丙"]}).to_excel(
            writer, sheet_name="Sheet1", index=False,
        )
        pd.DataFrame().to_excel(writer, sheet_name="Sheet2", index=False)
    workbook.seek(0)
    uploaded = client.post(
        "/api/sources/upload",
        data={"file": (workbook, "students.xlsx"), "workspace_id": "default"},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 201
    source_id = uploaded.get_json()["items"][0]["id"]

    queried = client.post(
        "/api/query",
        json={"source_ids": [source_id], "sql": 'SELECT COUNT(*) AS student_count FROM "Sheet1"'},
    )
    assert queried.status_code == 200
    assert queried.get_json()["result"]["data"] == [{"student_count": 3}]


def test_database_preview_uses_bounded_read_only_query(app, tmp_path):
    import pytest
    from sqlalchemy import create_engine, text

    from backend.services.datasets import preview_source
    from backend.services.security import SecretVault

    database_path = tmp_path / "remote-preview.sqlite3"
    url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE orders (id INTEGER, amount INTEGER)"))
            connection.execute(text(
                "INSERT INTO orders(id, amount) VALUES (1, 10), (2, 20), (3, 30), (4, 40)"
            ))
    finally:
        engine.dispose()

    with app.app_context():
        source = {
            "id": "src_database_preview", "kind": "database",
            "credential": SecretVault(app.config["VAULT_KEY"]).seal({"url": url}),
            "tables": [{"name": "orders", "source_name": "orders", "schema_name": None}],
        }
        preview = preview_source(source, "orders", 2)
        assert preview["rows"] == 2
        assert preview["sampled"] is True
        assert preview["truncated"] is True
        assert [row["id"] for row in preview["data"]] == [1, 2]
        with pytest.raises(ValueError, match="数据表不存在"):
            preview_source(source, "not_allowed", 2)


def test_database_profile_uses_bounded_read_only_preview(app, client, tmp_path):
    from sqlalchemy import create_engine, text

    from backend.services.security import SecretVault

    database_path = tmp_path / "remote-profile.sqlite3"
    url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE students (id INTEGER, score INTEGER)"))
            connection.execute(text(
                "INSERT INTO students(id, score) VALUES (1, 90), (2, 80), (3, 80)"
            ))
    finally:
        engine.dispose()

    with app.app_context():
        app.extensions["meridian_db"].put("sources", {
            "id": "src_database_profile", "workspace_id": "default", "name": "学生库",
            "kind": "database", "status": "ready",
            "credential": SecretVault(app.config["VAULT_KEY"]).seal({"url": url}),
            "tables": [{"name": "students", "source_name": "students", "schema_name": None}],
        }, workspace_id="default")

    response = client.get("/api/sources/src_database_profile/profile?table=students")
    assert response.status_code == 200
    result = response.get_json()["profile"]
    assert result["sampled"] is True
    assert result["table"] == "students"
    assert result["rows"] == 3
    assert result["duplicate_rows"] == 0
    assert {column["name"] for column in result["columns"]} == {"id", "score"}


def test_database_analysis_tables_can_be_selected(app, client):
    from backend.services.datasets import _query_table_scope

    source = app.extensions["meridian_db"].put("sources", {
        "id": "src_table_scope", "workspace_id": "default", "name": "业务库",
        "kind": "database", "driver": "mysql", "status": "ready",
        "tables": [
            {"name": "orders", "source_name": "orders", "schema_name": None},
            {"name": "customers", "source_name": "customers", "schema_name": None},
        ],
    }, workspace_id="default")

    selected = client.patch(
        f"/api/sources/{source['id']}", json={"analysis_tables": ["orders"]},
    )
    assert selected.status_code == 200
    assert selected.get_json()["item"]["analysis_tables"] == ["orders"]
    assert _query_table_scope([selected.get_json()["item"]]) == {"orders"}

    cleared = client.patch(
        f"/api/sources/{source['id']}", json={"analysis_tables": []},
    )
    assert cleared.status_code == 200
    assert _query_table_scope([cleared.get_json()["item"]]) == set()

    rejected = client.patch(
        f"/api/sources/{source['id']}", json={"analysis_tables": ["not_a_table"]},
    )
    assert rejected.status_code == 400


def test_query_result_can_be_retrieved(client, source):
    source_id = source["id"]
    response = client.post("/api/query", json={"source_ids": [source_id], "sql": "SELECT region, SUM(sales) AS sales FROM sales GROUP BY region"})
    assert response.status_code == 200
    query = response.get_json()["result"]
    fetched = client.get(f"/api/query-results/{query['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["result"]["id"] == query["id"]


def test_knowledge_and_session(client):
    document = client.post(
        "/api/knowledge/documents",
        data={"file": (io.BytesIO("GMV 指支付成功订单金额，不含取消订单。".encode()), "metric.md")},
        content_type="multipart/form-data",
    )
    assert document.status_code == 201
    results = client.post("/api/knowledge/search", json={"query": "GMV 口径"}).get_json()["items"]
    assert results and results[0]["document_name"] == "metric"

    session = client.post("/api/sessions", json={"name": "季度复盘"}).get_json()["item"]
    saved = client.post(f"/api/sessions/{session['id']}/save", json={"name": "季度复盘快照"})
    assert saved.status_code == 201
    loaded = client.post(f"/api/saved-sessions/{saved.get_json()['item']['id']}/load")
    assert loaded.status_code == 200


def test_knowledge_document_upload_accepts_chinese_txt_filename(client):
    document = client.post(
        "/api/knowledge/documents",
        data={
            "file": (
                io.BytesIO("及格率指成绩达到 60 分及以上的学生占比。".encode("utf-8")),
                "及格率解析.txt",
            ),
        },
        content_type="multipart/form-data",
    )
    assert document.status_code == 201
    item = document.get_json()["item"]
    assert item["name"] == "及格率解析"
    assert item["format"] == "txt"

    results = client.post("/api/knowledge/search", json={"query": "及格率"}).get_json()["items"]
    assert results and results[0]["document_name"] == "及格率解析"


def test_hybrid_knowledge_file_skills(client):
    metric = client.post(
        "/api/knowledge/entries",
        json={
            "type": "metric", "name": "GMV", "alias": "成交总额",
            "definition": "支付成功且未取消订单的含税金额总和",
            "sql_template": "SUM(CASE WHEN paid=1 AND cancelled=0 THEN amount ELSE 0 END)",
        },
    )
    assert metric.status_code == 201
    found = client.post("/api/knowledge/search", json={"query": "成交总额怎么计算"}).get_json()["items"]
    assert found and found[0]["kind"] == "metric"
    assert found[0]["vector_score"] >= 0
    assert found[0]["lexical_score"] > 0

    skills = client.get("/api/skills").get_json()
    builtin = {item["id"] for item in skills["items"]}
    assert {
        "data-query", "data-analysis", "attribution", "forecast",
        "excel-analysis", "visualization", "deep-research", "report", "ppt", "excel-export",
    } == builtin
    assert skills["can_manage"] is True
    attribution = client.get("/api/skills/attribution").get_json()["item"]
    assert attribution["source"] == "builtin"
    assert "归因" in attribution["instruction"]
    assert "query_metric" in attribution["allowed_tools"]



def test_formal_analysis_records_user_message(client, source):
    session = client.post("/api/sessions", json={"name": "对话测试", "source_ids": [source["id"]]}).get_json()["item"]
    response = client.post(
        "/api/analyses",
        json={
            "session_id": session["id"], "objective": "按 region 汇总 sales",
            "source_ids": [source["id"]], "skill_id": "data-analysis",
        },
    )
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["item"]["contract"]["payload"]["source_scope"] == [source["id"]]
    assert client.get(f"/api/sessions/{session['id']}").get_json()["messages"][-1]["role"] == "user"

    # A question may also name its skill inline, which is how the composer works.
    inline = client.post(
        "/api/analyses",
        json={
            "session_id": session["id"], "objective": "@预测分析 预测下个月",
            "source_ids": [source["id"]],
        },
    )
    assert inline.status_code == 201, inline.get_json()
    assert inline.get_json()["item"]["skill_id"] == "forecast"

    unknown = client.post(
        "/api/analyses",
        json={
            "session_id": session["id"], "objective": "随便看看",
            "source_ids": [source["id"]], "skill_id": "no-such-skill",
        },
    )
    assert unknown.status_code == 400


def test_identity_password_not_exposed(client):
    registered = client.post("/api/auth/register", json={"email": "owner@example.com", "password": "correct-horse", "name": "Owner"})
    assert registered.status_code == 201
    assert "password_hash" not in registered.get_json()["user"]
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"}).status_code == 400
    assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct-horse"}).status_code == 200


def test_formal_analysis_tools_preserve_dataset_lineage(app, source):
    from backend.services.agent_tools import AgentToolContext, execute_tool

    with app.app_context():
        context = AgentToolContext(
            app.extensions["meridian_db"], "default", "welcome", [source["id"]],
        )
        record, _events = execute_tool(
            "run_analysis",
            {
                "analysis_name": "Data_Decile_Analysis",
                "sql": "SELECT sales FROM sales",
                "target_column": "sales",
                "n_deciles": 3,
            },
            context,
        )
        assert record["derived_source_id"] == context.analysis_source_id
        assert set(record["result_ids"]) == {"analysis_result"}
        assert context.latest_result_id == record["result_ids"]["analysis_result"]
        chart, _events = execute_tool("generate_chart", {"type": "bar"}, context)
        assert chart["result_id"] == context.latest_result_id
        selection, _events = execute_tool(
            "select_chart", {"user_intent": "比较销售额", "available_columns": ["region", "sales"]}, context,
        )
        assert selection["recommended"]
        assert 1 <= len(selection["candidates"]) <= 3
        assert "catalog" not in selection

        skill, _events = execute_tool("load_analysis_skill", {"name": "data-analysis"}, context)
        assert skill["id"] == "data-analysis"
        assert "query_metric" in skill["allowed_tools"]
        # A skill the workspace does not have must fail with the list of what it does have.
        with pytest.raises(ValueError, match="技能不存在"):
            execute_tool("load_analysis_skill", {"name": "no-such-skill"}, context)

        profile_context = AgentToolContext(
            app.extensions["meridian_db"], "default", "welcome", [source["id"]],
        )
        profiled, _events = execute_tool(
            "profile_data",
            {"source_id": source["id"], "table": "sales", "columns": ['"sales"']},
            profile_context,
        )
        assert profiled["profile"]["numeric_columns"] == ["sales"]


def test_cross_origin_write_is_rejected(client):
    response = client.post("/api/sessions", json={"name": "blocked"}, headers={"Origin": "https://malicious.example"})
    assert response.status_code == 403
