from __future__ import annotations

import io

import pandas as pd


def test_bootstrap_and_capability_catalog(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.get_json()["database"] == "ready"

    bootstrap = client.get("/api/bootstrap").get_json()
    assert bootstrap["ok"] is True
    assert bootstrap["active_workspace"]["id"] == "default"
    assert bootstrap["active_session"]

    charts = client.get("/api/charts/catalog").get_json()["items"]
    methods = client.get("/api/analysis/methods").get_json()["items"]
    assert len(charts) >= 45
    assert {item["id"] for item in methods} >= {"cluster", "ab_test", "forecast", "anomaly"}


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


def test_source_query_profile_clean_and_guard(client, source):
    source_id = source["id"]
    assert client.get(f"/api/sources/{source_id}/schema").status_code == 200
    profile = client.get(f"/api/sources/{source_id}/profile").get_json()["profile"]
    assert profile["rows"] == 6
    assert profile["quality_score"] == 100

    query = client.post(
        "/api/query",
        json={"source_ids": [source_id], "sql": "SELECT region, SUM(sales) AS sales FROM data GROUP BY region ORDER BY sales DESC"},
    )
    assert query.status_code == 200
    result = query.get_json()["result"]
    assert result["rows"] == 2
    assert result["data"][0]["region"] == "North"

    blocked = client.post("/api/query", json={"source_ids": [source_id], "sql": "DROP TABLE data"})
    assert blocked.status_code == 400

    file_escape = client.post(
        "/api/query",
        json={"source_ids": [source_id], "sql": "SELECT * FROM read_csv('/tmp/private.csv')"},
    )
    assert file_escape.status_code == 400

    literal_keyword = client.post(
        "/api/query",
        json={"source_ids": [source_id], "sql": "SELECT 'please DELETE later' AS note FROM data LIMIT 1"},
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


def test_analysis_chart_and_delivery(client, source):
    source_id = source["id"]
    correlation = client.post("/api/analysis/run", json={"source_id": source_id, "method": "correlation"})
    assert correlation.status_code == 200
    matrix = correlation.get_json()["run"]["result"]["matrix"]
    assert "sales" in matrix

    query = client.post("/api/query", json={"source_ids": [source_id], "sql": "SELECT region, SUM(sales) AS sales FROM data GROUP BY region"}).get_json()["result"]
    chart = client.post("/api/charts/spec", json={"result_id": query["id"], "title": "区域销售"})
    assert chart.status_code == 200
    assert chart.get_json()["item"]["spec"]["type"] in {"bar", "pie"}

    for format_name in ("csv", "xlsx"):
        export = client.post("/api/exports/data", json={"result_id": query["id"], "format": format_name})
        assert export.status_code == 201
        download = client.get(export.get_json()["artifact"]["download_url"])
        assert download.status_code == 200
        assert download.data

    for format_name in ("docx", "pptx"):
        report = client.post("/api/exports/report", json={"result_id": query["id"], "format": format_name, "insights": ["North 销售领先"]})
        assert report.status_code == 201
        assert client.get(report.get_json()["artifact"]["download_url"]).status_code == 200


def test_knowledge_skill_memory_and_session(client):
    document = client.post(
        "/api/knowledge/documents",
        data={"file": (io.BytesIO("GMV 指支付成功订单金额，不含取消订单。".encode()), "metric.md")},
        content_type="multipart/form-data",
    )
    assert document.status_code == 201
    results = client.post("/api/knowledge/search", json={"query": "GMV 口径"}).get_json()["items"]
    assert results and results[0]["document_name"] == "metric"

    skill = client.post("/api/skills", json={"name": "利润诊断", "instruction": "分析收入、成本和利润率"})
    assert skill.status_code == 201
    memory = client.post("/api/memories", json={"title": "财年", "content": "财年从四月开始"})
    assert memory.status_code == 201

    session = client.post("/api/sessions", json={"name": "季度复盘"}).get_json()["item"]
    saved = client.post(f"/api/sessions/{session['id']}/save", json={"name": "季度复盘快照"})
    assert saved.status_code == 201
    loaded = client.post(f"/api/saved-sessions/{saved.get_json()['item']['id']}/load")
    assert loaded.status_code == 200


def test_hybrid_knowledge_file_skills_and_governed_memory(client):
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
    assert len(skills["items"]) >= 28
    regression = client.get("/api/skills/regression").get_json()["item"]
    assert regression["source"] == "builtin"
    assert regression["allowed_tools"] == ["get_schema", "query_data", "run_analysis", "generate_chart"]
    assert "线性回归" in regression["instruction"]

    response = client.post("/api/memories", json={
        "title": "图表语言偏好", "content": "以后所有图表标题默认使用中文。",
        "scope": "user", "type": "user",
    })
    assert response.status_code == 201
    memories = client.get("/api/memories").get_json()["items"]
    assert any("图表标题" in item.get("content", "") for item in memories), client.get("/api/jobs").get_json()
    assert client.get("/api/memories/search?q=图表标题").get_json()["items"]


def test_local_conversation_stream(client, source):
    session = client.post("/api/sessions", json={"name": "对话测试", "source_ids": [source["id"]]}).get_json()["item"]
    response = client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"message": "按 region 汇总 sales", "source_ids": [source["id"]], "skill_id": "executive-summary"},
    )
    text = response.data.decode("utf-8")
    assert response.status_code == 200
    assert "event: contract" in text
    assert '"requires_confirmation": true' in text
    assert "event: done" in text
    assert client.get(f"/api/sessions/{session['id']}/messages").get_json()["items"][-1]["role"] == "user"


def test_identity_password_not_exposed(client):
    registered = client.post("/api/auth/register", json={"email": "owner@example.com", "password": "correct-horse", "name": "Owner"})
    assert registered.status_code == 201
    assert "password_hash" not in registered.get_json()["user"]
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"}).status_code == 400
    assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct-horse"}).status_code == 200


def test_advanced_modeling_and_forecasting(client):
    rows = [
        {
            "date": f"2026-01-{index + 1:02d}",
            "visits": 100 + index * 4,
            "spend": 20 + (index % 7) * 3,
            "revenue": 150 + index * 6 + (index % 5),
            "converted": int(index % 3 != 0),
        }
        for index in range(28)
    ]
    cases = [
        ("decision_tree", {"target": "converted", "features": ["visits", "spend"]}),
        ("gradient_boosting", {"target": "revenue", "features": ["visits", "spend"]}),
        ("mlp", {"target": "revenue", "features": ["visits", "spend"], "max_iter": 120}),
        ("univariate_screening", {"target": "revenue", "features": ["visits", "spend"]}),
        ("prophet_like", {"date_column": "date", "value_column": "revenue", "horizon": 4, "season_length": 7}),
        ("neural_forecast", {"date_column": "date", "value_column": "revenue", "horizon": 4, "lookback": 5, "max_iter": 150}),
    ]
    for method, params in cases:
        response = client.post("/api/analysis/run", json={"rows": rows, "method": method, "params": params})
        assert response.status_code == 200, (method, response.get_json())
        assert response.get_json()["run"]["status"] == "completed"


def test_registered_analysis_contracts_are_available_and_persist_tables(app, client, source):
    registered_ids = {
        "AB_Test_Analysis", "Data_Decile_Analysis", "Decision_Tree", "K_Means",
        "Logistic_Regression", "Regression", "Sklearn_Model", "Torch_MLP",
        "Univariate_Screening", "Time_Series_ARIMA", "Time_Series_SARIMA",
        "Time_Series_VAR", "Time_Series_Prophet", "Time_Series_GRU",
    }
    methods = client.get("/api/analysis/methods").get_json()["items"]
    assert {item["id"] for item in methods} >= registered_ids
    response = client.post(
        "/api/analysis/run",
        json={
            "rows": [
                {"variant": "control", "converted": value} for value in [0, 1, 0, 1, 0, 1]
            ] + [
                {"variant": "treatment", "converted": value} for value in [1, 1, 0, 1, 1, 1]
            ],
            "method": "AB_Test_Analysis",
            "params": {
                "target_column": "converted", "groupby_column": "variant",
                "analysis_options": {"control_group": "control", "metric_type": "binary"},
            },
        },
    )
    assert response.status_code == 200, response.get_json()
    result = response.get_json()["run"]["result"]
    assert set(result["tables"]) == {"analysis_result", "analysis_breakdown", "analysis_metrics"}
    metrics = {row["metric"]: row["value"] for row in result["tables"]["analysis_metrics"]["data"]}
    assert metrics["metric_type"] == "binary"
    assert "p_value" in metrics and "srm_p_value" not in metrics

    from backend.services.agent_tools import AgentToolContext, execute_tool

    with app.app_context():
        context = AgentToolContext(
            app.extensions["meridian_db"], "default", "welcome", [source["id"]],
        )
        record, _events = execute_tool(
            "run_analysis",
            {
                "analysis_name": "Data_Decile_Analysis",
                "sql": "SELECT sales FROM data",
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

        skill, _events = execute_tool("load_analysis_skill", {"name": "data_quality"}, context)
        assert skill["id"] == "quality-audit"

        profile_context = AgentToolContext(
            app.extensions["meridian_db"], "default", "welcome", [source["id"]],
        )
        profiled, _events = execute_tool(
            "profile_data",
            {"source_id": source["id"], "table": "data", "columns": ['"sales"']},
            profile_context,
        )
        assert profiled["profile"]["numeric_columns"] == ["sales"]


def test_cross_origin_write_is_rejected(client):
    response = client.post("/api/sessions", json={"name": "blocked"}, headers={"Origin": "https://malicious.example"})
    assert response.status_code == 403
