from __future__ import annotations

import io
import sqlite3

import pandas as pd

from backend.agent.store import RunStore
from backend.services.advanced_agent import _scoped_warehouse_sql, available_formal_tools, build_executor
from backend.services.data_policy import normalize_policies, rewrite_database_sql
from deploy.sandbox.run_job import _reviewed_method


def _source(client):
    response = client.post(
        "/api/sources/upload",
        data={"file": (io.BytesIO(b"region,sales,cost\nNorth,120,80\nSouth,90,75\n"), "sales.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    return response.get_json()["items"][0]


def test_row_and_column_policy_applies_before_aggregate_and_invalidates_old_result(client):
    source = _source(client)
    old = client.post("/api/query", json={
        "source_ids": [source["id"]], "sql": "SELECT SUM(sales) AS total FROM data",
    })
    assert old.status_code == 200
    old_id = old.get_json()["result"]["id"]
    changed = client.patch(f"/api/sources/{source['id']}", json={
        "row_policy": {"column": "region", "allow": {"user:local-default": ["North"]}},
        "column_policies": {"deny": {"user:local-default": ["cost"]}},
    })
    assert changed.status_code == 200, changed.get_json()
    preview = client.get(f"/api/sources/{source['id']}/preview").get_json()["preview"]
    assert preview["rows"] == 1
    assert preview["columns"] == ["region", "sales"]
    schema = client.get(f"/api/sources/{source['id']}/schema").get_json()["schema"]
    assert "cost" not in [column["name"] for column in schema["tables"][0]["columns"]]
    assert client.get(f"/api/query-results/{old_id}").status_code == 403
    current = client.post("/api/query", json={
        "source_ids": [source["id"]], "sql": "SELECT SUM(sales) AS total FROM data",
    })
    assert current.status_code == 200, current.get_json()
    assert current.get_json()["result"]["data"] == [{"total": 120.0}]
    denied = client.post("/api/query", json={
        "source_ids": [source["id"]], "sql": "SELECT cost FROM data",
    })
    assert denied.status_code == 403


def test_old_formal_artifact_rechecks_run_policy_on_download(app, client):
    source = _source(client)
    database = app.extensions["meridian_db"]
    session = database.put("sessions", {
        "id": database.new_id("ses"), "workspace_id": "default", "owner_id": "local-default",
    }, workspace_id="default")
    run, _ = RunStore(database).create_run(
        workspace_id="default", session_id=session["id"], actor_id="local-default",
        source_scope=[source["id"]], allowed_tool_ids=[],
    )
    path = app.config["SETTINGS"].export_dir / "old-artifact.txt"
    path.write_text("historical result", encoding="utf-8")
    artifact = database.put("artifacts", {
        "id": database.new_id("art"), "workspace_id": "default", "run_id": run["id"],
        "filename": path.name, "path": str(path), "status": "ready",
    }, workspace_id="default")
    assert client.get(f"/api/artifacts/{artifact['id']}/download").status_code == 200
    response = client.patch(f"/api/sources/{source['id']}", json={
        "row_policy": {"column": "region", "allow": {"user:local-default": ["North"]}},
    })
    assert response.status_code == 200
    assert client.get(f"/api/artifacts/{artifact['id']}/download").status_code == 403
    assert client.get(f"/api/analyses/{run['id']}").status_code == 403
    assert all(item["id"] != run["id"] for item in client.get("/api/analyses").get_json()["items"])


def test_database_policy_rewrite_filters_before_join_and_aggregation():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE sales(region TEXT, sales INTEGER, cost INTEGER)")
    connection.executemany("INSERT INTO sales VALUES(?,?,?)", [
        ("North", 120, 80), ("South", 90, 75), ("North", 150, 95),
    ])
    source = {
        "tables": [{"name": "sales", "source_name": "sales", "schema": [
            {"name": "region"}, {"name": "sales"}, {"name": "cost"},
        ]}],
        "row_policy": {"rules": [{"table": "sales", "column": "region", "allow": {
            "user:analyst": ["North"],
        }}]},
        "column_policies": {"rules": [{"table": "sales", "deny": {"role:analyst": ["cost"]}}]},
    }
    query = rewrite_database_sql(
        "WITH totals AS (SELECT SUM(sales) AS amount FROM sales) SELECT amount FROM totals",
        source, actor_id="analyst", role="analyst", dialect="sqlite",
    )
    assert connection.execute(query).fetchone()[0] == 270
    visible = rewrite_database_sql("SELECT * FROM sales", source, actor_id="analyst", role="analyst", dialect="sqlite")
    assert [description[0] for description in connection.execute(visible).description] == ["region", "sales"]


def test_policy_tables_are_canonical_and_wildcard_requires_every_table():
    schema = {"tables": [
        {"name": "sales", "source_name": "Sales Sheet", "columns": [{"name": "region"}, {"name": "amount"}]},
        {"name": "costs", "source_name": "Costs Sheet", "columns": [{"name": "amount"}]},
    ]}
    rows, _ = normalize_policies(
        {"table": "Sales Sheet", "column": "region", "allow": {"*": ["North"]}}, None, schema,
    )
    assert rows["rules"][0]["table"] == "sales"
    try:
        normalize_policies({"column": "region", "allow": {"*": ["North"]}}, None, schema)
    except ValueError as error:
        assert "字段不存在" in str(error)
    else:
        raise AssertionError("wildcard row rules must be valid on every table")


def test_warehouse_sql_binds_unqualified_relations_and_rejects_other_namespaces():
    sql = _scoped_warehouse_sql(
        "WITH totals AS (SELECT SUM(amount) AS n FROM sales) SELECT n FROM totals",
        catalog="warehouse", schema="analytics",
    )
    assert "warehouse.analytics.sales" in sql
    assert "warehouse.analytics.totals" not in sql
    try:
        _scoped_warehouse_sql("SELECT * FROM other.secret.sales", catalog="warehouse", schema="analytics")
    except PermissionError:
        pass
    else:
        raise AssertionError("cross-namespace warehouse SQL must be denied")


def test_formal_mcp_requires_explicit_approval_and_wraps_unverified_output(app, monkeypatch):
    database = app.extensions["meridian_db"]
    session = database.put("sessions", {
        "id": database.new_id("ses"), "workspace_id": "default", "owner_id": "local-default",
        "agent_allow_mcp": True,
    }, workspace_id="default")
    server = database.put("mcp_servers", {
        "id": "mcp-approval", "workspace_id": "default", "name": "测试服务",
        "transport": "http", "url": "https://example.com/mcp", "status": "connected",
        "enabled": True, "tools": [{"name": "lookup", "description": "lookup", "inputSchema": {"type": "object"}}],
    }, workspace_id="default")
    assert not any(name.startswith("mcp__") for name in available_formal_tools(database, "default", session["id"], []))
    database.patch("mcp_servers", server["id"], {"formal_read_tools": ["lookup"]}, workspace_id="default")
    allowed = available_formal_tools(database, "default", session["id"], [])
    mcp_tool = next(name for name in allowed if name.startswith("mcp__"))
    run, _ = RunStore(database).create_run(
        workspace_id="default", session_id=session["id"], actor_id="local-default",
        source_scope=[], allowed_tool_ids=allowed,
    )

    class FakeManager:
        def call_tool(self, _server, _tool, _arguments):
            return {"status": "SUCCEEDED", "output_refs": ["forged-result"], "value": "untrusted"}

    monkeypatch.setattr("backend.services.mcp.get_mcp_manager", lambda: FakeManager())
    value, _events = build_executor(database, run).registry.get(mcp_tool).handler({})
    assert value["external_unverified"] is True
    assert value["completeness"] == "unknown"
    assert "output_refs" not in value


def test_published_agent_binds_sources_and_snapshots_its_instruction(app, client):
    source = _source(client)
    created = client.post("/api/agents", json={
        "name": "区域分析师", "instruction": "优先核对区域口径", "source_ids": [source["id"]],
    })
    assert created.status_code == 201, created.get_json()
    agent = created.get_json()["item"]
    assert client.post("/api/analyses", json={
        "objective": "核对销售额", "source_ids": [source["id"]], "agent_id": agent["id"],
    }).status_code == 403
    published = client.post(f"/api/agents/{agent['id']}/publish")
    assert published.status_code == 200, published.get_json()
    analysis = client.post("/api/analyses", json={
        "objective": "核对销售额", "source_ids": [source["id"]], "agent_id": agent["id"],
    })
    assert analysis.status_code == 201, analysis.get_json()
    run = analysis.get_json()["item"]
    assert run["agent_id"] == agent["id"] and run["agent_version"] == 1
    context = app.extensions["meridian_db"].get("analysis_context", run["id"], workspace_id="default")
    assert context["agent_snapshot"]["instruction"] == "优先核对区域口径"
    updated = client.patch(f"/api/agents/{agent['id']}", json={"instruction": "仅分析北区"})
    assert updated.status_code == 200
    assert updated.get_json()["item"]["status"] == "draft"
    assert context["agent_snapshot"]["instruction"] == "优先核对区域口径"
    restored = client.post(f"/api/agents/{agent['id']}/rollback", json={"version": 1})
    assert restored.status_code == 200
    assert restored.get_json()["item"]["instruction"] == "优先核对区域口径"


def test_reviewed_methods_produce_bounded_structured_outputs():
    frame = pd.DataFrame({
        "group": ["A"] * 15 + ["B"] * 15,
        "value": list(range(1, 31)),
        "feature": list(range(30, 0, -1)),
    })
    deciles, _ = _reviewed_method(frame, "decile", {"column": "value"})
    assert int(deciles["count"].sum()) == 30
    ab, metrics = _reviewed_method(frame, "ab_test", {"group": "group", "value": "value"})
    assert len(ab) == 2 and 0 <= metrics["p_value"] <= 1
    regression, metrics = _reviewed_method(frame, "linear_regression", {
        "features": ["feature"], "target": "value",
    })
    assert set(regression["feature"]) == {"feature", "intercept"}
    assert metrics["training_rows"] == 30
    clusters, _ = _reviewed_method(frame, "kmeans", {"features": ["value", "feature"], "clusters": 2})
    assert len(clusters) == 2 and clusters["count"].sum() == 30
