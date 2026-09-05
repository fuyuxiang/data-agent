from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.agent_runtime import _recoverable_tool_text
from backend.services.agent_tools import AgentToolContext, execute_tool, tool_schemas


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
