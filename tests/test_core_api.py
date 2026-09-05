from __future__ import annotations

import io
import time


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


def test_hybrid_knowledge_file_skills_and_automatic_memory(client):
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

    session = client.post("/api/sessions", json={"name": "记忆提取"}).get_json()["item"]
    response = client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"message": "请记住：以后所有图表标题默认使用中文。"},
    )
    assert response.status_code == 200
    assert b"event: done" in response.data
    deadline = time.time() + 5
    memories = []
    while time.time() < deadline:
        memories = client.get("/api/memories").get_json()["items"]
        if any("图表标题" in item.get("content", "") for item in memories):
            break
        time.sleep(0.05)
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
    assert "event: plan" in text
    assert "event: table" in text
    assert "event: done" in text
    assert client.get(f"/api/sessions/{session['id']}/messages").get_json()["items"][-1]["role"] == "assistant"


def test_hooks_run_from_agent_lifecycle_and_can_reject_tools(client, source):
    completed_hook = client.post(
        "/api/hooks",
        json={"name": "分析完成记录", "event": "analysis.completed", "action": {"type": "noop"}},
    ).get_json()["item"]
    post_hook = client.post(
        "/api/hooks",
        json={
            "name": "查询复核提示", "event": "post_tool_use",
            "condition": "tool == query_data && ok == true",
            "action": {"type": "prompt", "message": "复核查询 $TOOL_NAME"},
        },
    ).get_json()["item"]
    session = client.post(
        "/api/sessions", json={"name": "Hook 生命周期", "source_ids": [source["id"]]},
    ).get_json()["item"]
    stream = client.post(
        f"/api/sessions/{session['id']}/messages",
        json={"message": "按 region 汇总 sales", "source_ids": [source["id"]]},
    ).data.decode("utf-8")
    assert "event: hook_event" in stream
    assert "查询复核提示" in stream
    hooks = {item["id"]: item for item in client.get("/api/hooks").get_json()["items"]}
    assert hooks[completed_hook["id"]]["run_count"] == 1
    assert hooks[post_hook["id"]]["run_count"] == 1
    history = client.get("/api/hooks/history").get_json()["items"]
    assert {item["event"] for item in history} >= {"post_tool_use", "analysis.completed"}

    rejected_hook = client.post(
        "/api/hooks",
        json={
            "name": "禁止查询", "event": "pre_tool_use", "condition": "tool == query_data",
            "reject": True, "action": {"type": "prompt", "message": "策略拒绝 $TOOL_NAME"},
        },
    ).get_json()["item"]
    rejected_session = client.post(
        "/api/sessions", json={"name": "Hook 拒绝", "source_ids": [source["id"]]},
    ).get_json()["item"]
    rejected_stream = client.post(
        f"/api/sessions/{rejected_session['id']}/messages",
        json={"message": "查询 sales", "source_ids": [source["id"]]},
    ).data.decode("utf-8")
    assert "策略拒绝 query_data" in rejected_stream
    assert "event: table" not in rejected_stream
    rejected = next(item for item in client.get("/api/hooks").get_json()["items"] if item["id"] == rejected_hook["id"])
    assert rejected["run_count"] == 1


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


def test_cross_origin_write_is_rejected(client):
    response = client.post("/api/sessions", json={"name": "blocked"}, headers={"Origin": "https://malicious.example"})
    assert response.status_code == 403
