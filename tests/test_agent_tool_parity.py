from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.services.agent_runtime import _recoverable_tool_text
from backend.services.agent_tools import AgentToolContext, execute_tool, tool_schemas
from backend.services.charts import REFERENCE_CHARTS
from backend.services.datasets import execute_query, register_derived_tables


def _context(app, session_id: str = "welcome", source_ids: list[str] | None = None) -> AgentToolContext:
    return AgentToolContext(
        database=app.extensions["meridian_db"], workspace_id="default",
        session_id=session_id, source_ids=source_ids or [],
    )


def test_reference_control_team_workflow_and_workspace_tools(app, client, source):
    session = client.post("/api/sessions", json={"name": "工具对齐", "source_ids": [source["id"]]}).get_json()["item"]
    with app.app_context():
        context = _context(app, session["id"], [source["id"]])
        names = {item["function"]["name"] for item in tool_schemas(context)}
        assert {
            "workspace_status", "clean_data", "set_ppt_color_scheme", "team_create",
            "team_plan_create", "agent_delegate", "workflow_create_custom",
            "read_tool_result", "plan_complete", "display_diagram",
        } <= names

        cleaned, _ = execute_tool(
            "clean_data",
            {"operation": "winsorize", "columns": ["sales"], "lower_pct": 10, "upper_pct": 90},
            context,
        )
        assert cleaned["source"]["kind"] == "derived"
        assert cleaned["operations"][0]["operation"] == "winsorize"

        created, _ = execute_tool(
            "team_create",
            {
                "name": "核对组", "members": [
                    {"name": "数据员", "role": "数据核对"},
                    {"name": "业务员", "role": "业务解读"},
                ],
            },
            context,
        )
        plan, _ = execute_tool(
            "team_plan_create",
            {
                "team_name": created["name"], "goal": "复核销售数据",
                "assignments": [
                    {"task_id": "data", "member_name": "数据员", "prompt": "核对字段"},
                    {"task_id": "business", "member_name": "业务员", "prompt": "解读结果", "depends_on": ["data"]},
                ],
            },
            context,
        )
        assert plan["status"] == "planned"
        assert plan["tasks"][1]["depends_on"] == ["data"]

        workflow, _ = execute_tool(
            "workflow_create_custom",
            {
                "name": "双人工作流", "agents": [
                    {"name": "查询员", "role": "查询", "instructions": "核对表结构"},
                    {
                        "name": "复核员", "role": "复核", "instructions": "复核查询证据",
                        "depends_on": ["查询员"], "allowed_tools": ["get_schema", "query_data"],
                    },
                ],
            },
            context,
        )
        assert workflow["status"] == "published"
        assert workflow["version"] == 1

        written, _ = execute_tool(
            "workspace_write_file", {"file_path": "workspace://outputs/note.txt", "content": "version one"}, context,
        )
        assert written["sha256"]
        fresh_context = _context(app, session["id"], [source["id"]])
        edited, _ = execute_tool(
            "workspace_edit_file",
            {"file_path": "workspace://outputs/note.txt", "old_string": "one", "new_string": "two"},
            fresh_context,
        )
        assert edited["previous_version_id"]

        text, artifact = _recoverable_tool_text(context, "large", {"rows": ["x" * 500] * 100})
        assert artifact and artifact["id"] in text
        page, _ = execute_tool(
            "read_tool_result", {"artifact_id": artifact["id"], "offset": 0, "limit": 120}, context,
        )
        assert page["next_offset"] == 120


def test_reference_ppt_outline_generates_real_slides(app):
    with app.app_context():
        context = _context(app)
        execute_tool("set_ppt_color_scheme", {"scheme": "bcg"}, context)
        result, events = execute_tool(
            "generate_ppt",
            {
                "title": "经营洞察", "slides": [
                    {"layout": "cover", "params": {"title": "经营洞察", "subtitle": "2026"}},
                    {"layout": "closing", "params": {"title": "下一步", "message": "从数据到行动"}},
                ],
            },
            context,
        )
        assert result["kind"] == "pptx"
        assert result["metadata"]["slides"] == 2
        assert events[0][0] == "artifact"


class _AskCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        function = SimpleNamespace(
            name="ask_user", arguments='{"question":"请选择分析口径","choices":["收入","毛利"]}',
        )
        delta = SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(index=0, id="call_ask", function=function)],
        )
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)])


def test_ask_user_stops_agent_until_next_user_turn(client, monkeypatch):
    completions = _AskCompletions()
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(
        "backend.services.agent_runtime.resolve_provider",
        lambda _provider_id=None, _workspace_id="default": ({"model": "fake", "temperature": 0}, fake),
    )
    session = client.post("/api/sessions", json={"name": "澄清会话"}).get_json()["item"]
    response = client.post(f"/api/sessions/{session['id']}/messages", json={"message": "做趋势分析"})
    stream = response.data.decode("utf-8")
    assert response.status_code == 200
    assert "awaiting_user_reply" in stream
    assert completions.calls == 1
    messages = client.get(f"/api/sessions/{session['id']}/messages").get_json()["items"]
    assert messages[-1]["metadata"]["choices"] == ["收入", "毛利"]


def test_custom_workflow_rejects_forward_or_unknown_dependencies(app):
    with app.app_context():
        context = _context(app)
        with pytest.raises(ValueError, match="更早"):
            execute_tool(
                "workflow_create_custom",
                {
                    "name": "无效流程", "agents": [
                        {"name": "A", "role": "A", "instructions": "a", "depends_on": ["B"]},
                        {"name": "B", "role": "B", "instructions": "b"},
                    ],
                },
                context,
            )


def test_reference_tool_result_download_route_is_session_scoped(app, client):
    first = client.post("/api/sessions", json={"name": "result owner"}).get_json()["item"]
    second = client.post("/api/sessions", json={"name": "other session"}).get_json()["item"]
    content = '{"rows":[1,2,3]}'
    with app.app_context():
        item = app.extensions["meridian_db"].put(
            "tool_results",
            {
                "id": "tr_legacy_download", "workspace_id": "default",
                "session_id": first["id"], "tool_name": "query_data",
                "content": content, "total_chars": len(content),
            },
            workspace_id="default",
        )

    raw = client.get(f"/api/session/{first['id']}/tool-results/{item['id']}")
    assert raw.status_code == 200
    assert raw.get_data(as_text=True) == content
    assert raw.headers["X-Tool-Name"] == "query_data"
    assert raw.headers["X-Content-SHA256"] == hashlib.sha256(content.encode()).hexdigest()

    record = client.get(f"/api/session/{first['id']}/tool-results/{item['id']}?format=json")
    assert record.status_code == 200
    assert record.get_json()["data"] == content
    assert client.get(f"/api/session/{second['id']}/tool-results/{item['id']}").status_code == 404


def test_reference_tool_parameter_contracts_are_exposed_and_executable(app, source):
    expected = {
        "select_chart": {"user_intent", "available_columns"},
        "generate_chart": {"chart_type", "sql", "field_mapping"},
        "get_table_detail": {"table_name"},
        "delete_analysis_tables": {"table_names", "confirm"},
        "memory_read": {"name"},
        "search_mcp_tools": {"server"},
        "ask_user": {"question", "options", "multi_select"},
        "configure_hooks": {"settings", "merge", "reason", "confirm_command_hooks"},
        "team_delegate": {
            "review_plan_id", "review_task_ids", "timeout_seconds",
            "max_concurrency", "result_max_tokens",
        },
    }
    with app.app_context():
        context = _context(app, source_ids=[source["id"]])
        schemas = {
            item["function"]["name"]: item["function"]["parameters"]
            for item in tool_schemas(context)
        }
        for name, properties in expected.items():
            assert properties <= schemas[name]["properties"].keys()

        selected, _ = execute_tool(
            "select_chart",
            {"user_intent": "按月展示销售趋势", "available_columns": ["month", "sales"]},
            context,
        )
        assert selected["candidates"][0]["chart_id"] == "Line_Chart"

        chart, events = execute_tool(
            "generate_chart",
            {
                "chart_type": "Bar_Chart",
                "sql": "SELECT region, SUM(sales) AS total_sales FROM data GROUP BY region",
                "field_mapping": {"x": "region", "y": "total_sales"},
                "title": "地区销售额",
            },
            context,
        )
        assert chart["spec"]["type"] == "bar"
        assert chart["spec"]["reference_chart_id"] == "Bar_Chart"
        assert events[0][0] == "chart"

        detail, _ = execute_tool("get_table_detail", {"table_name": "data"}, context)
        assert detail["table"]["name"] == "data"

        question, question_events = execute_tool(
            "ask_user",
            {"question": "选择指标", "options": ["销售额", "毛利"], "multi_select": True},
            context,
        )
        assert question["multi_select"] is True
        assert question_events == [("ask_user", {
            "question": "选择指标", "choices": ["销售额", "毛利"],
            "options": ["销售额", "毛利"], "multi_select": True,
        })]

        configured, _ = execute_tool(
            "configure_hooks",
            {
                "settings": {
                    "enabled": True,
                    "hooks": [{"event": "analysis.completed", "action": {"type": "notify"}}],
                },
                "merge": False,
                "reason": "reference contract",
            },
            context,
        )
        assert configured["merge"] is False
        assert configured["reason"] == "reference contract"


def test_reference_delete_analysis_tables_removes_only_named_derived_tables(app):
    with app.app_context():
        context = _context(app)
        derived = register_derived_tables(
            {
                "keep_me": pd.DataFrame({"value": [1, 2]}),
                "delete_me": pd.DataFrame({"value": [3, 4]}),
            },
            "default",
            name="two derived tables",
        )
        context.source_ids.append(derived["id"])
        removed, _ = execute_tool(
            "delete_analysis_tables",
            {"table_names": ["delete_me"], "confirm": True},
            context,
        )
        assert removed["deleted_tables"] == ["delete_me"]
        assert removed["archived_sources"] == []
        remaining = app.extensions["meridian_db"].get("sources", derived["id"])
        assert [item["name"] for item in remaining["tables"]] == ["keep_me"]
        result = execute_query([derived["id"]], "SELECT * FROM keep_me", "default", 10)
        assert result["rows"] == 2


def test_every_reference_chart_contract_generates_a_portable_chart(app):
    frame = pd.DataFrame({
        "category": ["A", "A", "B", "B", "C", "C"],
        "group": ["G1", "G2", "G1", "G2", "G1", "G2"],
        "time": [1, 2, 3, 4, 5, 6],
        "value": [12.0, 8.0, 20.0, 14.0, 10.0, 18.0],
        "start": [8.0, 7.0, 14.0, 10.0, 9.0, 12.0],
        "end": [12.0, 8.0, 20.0, 14.0, 10.0, 18.0],
        "left": [6.0, 5.0, 10.0, 8.0, 7.0, 9.0],
        "right": [7.0, 6.0, 11.0, 9.0, 8.0, 10.0],
        "source": ["A", "A", "B", "B", "C", "C"],
        "target_node": ["B", "C", "C", "D", "D", "A"],
        "longitude": [116.4, 121.5, 113.3, 104.1, 114.3, 108.9],
        "latitude": [39.9, 31.2, 23.1, 30.7, 30.6, 34.3],
    })
    role_columns = {
        "x": "category", "y": "value", "series": "group", "group": "group",
        "category": "category", "label": "category", "names": "category", "labels": "category",
        "value": "value", "values": "value", "actual": "value", "target": "end",
        "start": "start", "end": "end", "left_value": "left", "right_value": "right",
        "source": "source", "z": "value", "weight": "value", "size": "value",
        "time": "time", "longitude": "longitude", "latitude": "latitude", "parents": "group",
        "color": "group", "low": "left", "medium": "start", "high": "end",
        "order": "time", "x_mid": "start", "y_mid": "end", "highlight": "group",
        "type": "group",
    }
    with app.app_context():
        derived = register_derived_tables({"chart_data": frame}, "default", name="chart contracts")
        query = execute_query([derived["id"]], "SELECT * FROM chart_data", "default", 100)
        context = _context(app, source_ids=[derived["id"]])
        for chart_id, (native_type, required, optional) in REFERENCE_CHARTS.items():
            mapping = {}
            for role in [*required, *optional]:
                if role == "dimensions":
                    mapping[role] = ["time", "value", "start", "end"]
                elif role == "y" and chart_id == "Heatmap":
                    mapping[role] = "group"
                elif role == "x" and chart_id == "Arc_Chart":
                    mapping[role] = "source"
                elif role == "y" and chart_id == "Arc_Chart":
                    mapping[role] = "target_node"
                elif role == "target" and chart_id in {"Network_Diagram", "Sankey_Chart", "Chord_Diagram"}:
                    mapping[role] = "target_node"
                elif role in role_columns:
                    mapping[role] = role_columns[role]
            chart, events = execute_tool(
                "generate_chart",
                {"chart_type": chart_id, "result_id": query["id"], "field_mapping": mapping},
                context,
            )
            spec = chart["spec"]
            assert spec["type"] == native_type, chart_id
            assert spec["reference_chart_id"] == chart_id
            assert spec["option"].get("series"), chart_id
            assert events[0][0] == "chart"
            json.dumps(spec, allow_nan=False)
