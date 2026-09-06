from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from flask import current_app

from ..core.database import Database
from .analytics import ANALYSIS_METHODS, clean_frame, profile, run_analysis_with_frames
from .charts import catalog as chart_catalog
from .charts import make_spec, normalize_chart_type, select_charts
from .datasets import (
    delete_derived_tables,
    execute_query,
    load_result_frame,
    register_derived_tables,
    schema_for_source,
    source_table,
)
from .exports import export_data, export_report
from .knowledge import search as search_knowledge
from .memory import search_memories
from .security import SecretVault, safe_http_request
from .workspace_tools import WorkspaceFiles


def _function(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    parameters: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


BUILTIN_TOOLS = [
    _function(
        "query_knowledge",
        "Search the workspace business knowledge before interpreting business metrics.",
        {"question": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
        ["question"],
    ),
    _function("get_schema", "Return schemas for every selected data source and the exact table aliases usable in SQL."),
    _function(
        "query_data",
        "Execute one read-only SQL query over the selected sources and return a bounded result table.",
        {"sql": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 5000}},
        ["sql"],
    ),
    _function(
        "profile_data",
        "Profile a selected source table or a prior query result for data quality and distributions.",
        {
            "source_id": {"type": "string"}, "table": {"type": "string"},
            "table_name": {"type": "string"}, "result_id": {"type": "string"},
            "columns": {"type": "array", "items": {"type": "string"}},
        },
    ),
    _function(
        "run_analysis",
        "Run a built-in statistical or machine-learning analysis on a source table or query result.",
        {
            "method": {"type": "string", "enum": [item["id"] for item in ANALYSIS_METHODS]},
            "analysis_name": {"type": "string", "enum": [item["id"] for item in ANALYSIS_METHODS]},
            "sql": {"type": "string"}, "params": {"type": "object"},
            "target_column": {"type": "string"}, "groupby_column": {"type": "string"},
            "n_deciles": {"type": "integer"}, "analysis_options": {"type": "object"},
            "source_id": {"type": "string"}, "table": {"type": "string"},
            "result_id": {"type": "string"},
        },
    ),
    _function(
        "select_chart",
        "Rank supported chart types from the visualization intent, available columns, and optional query result.",
        {
            "user_intent": {"type": "string"},
            "available_columns": {"type": "array", "items": {"type": "string"}},
            "result_id": {"type": "string"},
        },
    ),
    _function(
        "generate_chart",
        "Generate and save a chart specification from a prior query result.",
        {
            "result_id": {"type": "string"}, "type": {"type": "string"},
            "chart_type": {"type": "string"}, "sql": {"type": "string"},
            "field_mapping": {"type": "object"},
            "title": {"type": "string"}, "x": {"type": "string"},
            "y": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "group": {"type": "string"}, "options": {"type": "object"},
        },
    ),
    _function(
        "export_excel",
        "Export a prior query result to a downloadable XLSX or CSV artifact.",
        {
            "result_id": {"type": "string"}, "format": {"type": "string", "enum": ["xlsx", "csv"]},
            "title": {"type": "string"}, "filename": {"type": "string"},
            "tables": {"type": "array", "items": {"type": "string"}},
        },
    ),
    _function(
        "export_report",
        "Export a prior query result and grounded conclusions to a DOCX or PPTX artifact.",
        {
            "result_id": {"type": "string"}, "format": {"type": "string", "enum": ["docx", "pptx"]},
            "title": {"type": "string"}, "summary": {"type": "string"},
            "insights": {"type": "array", "items": {"type": "string"}},
            "sections": {"type": "array", "items": {"type": "object"}},
        },
    ),
    _function(
        "memory_read",
        "Read enabled long-term memories in the current workspace.",
        {
            "name": {"type": "string"}, "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    ),
    _function(
        "search_mcp_tools",
        "Search tools discovered from connected MCP servers.",
        {
            "query": {"type": "string"}, "server": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        ["query"],
    ),
]

EXTRA_TOOLS = [
    _function("workspace_status", "Show mounted and system workspace roots and safety limits."),
    _function("get_table_detail", "Return detailed schema for one exact table.", {"source_id": {"type": "string"}, "table": {"type": "string"}, "table_name": {"type": "string"}}),
    _function("create_analysis_table", "Create a queryable derived table from read-only SQL.", {"sql": {"type": "string"}, "table_name": {"type": "string"}}, ["sql", "table_name"]),
    _function("delete_analysis_tables", "Delete named derived tables or archive exact derived sources only.", {"source_ids": {"type": "array", "items": {"type": "string"}}, "table_names": {"type": "array", "items": {"type": "string"}}, "confirm": {"type": "boolean"}}, ["confirm"]),
    _function("clean_data", "Apply non-destructive cleaning and create a derived source.", {"result_id": {"type": "string"}, "source_id": {"type": "string"}, "table": {"type": "string"}, "table_name": {"type": "string"}, "operations": {"type": "array", "items": {"type": "object"}}, "operation": {"type": "string", "enum": ["fill_na", "winsorize", "trimming"]}, "columns": {"type": "array", "items": {"type": "string"}}, "fill_method": {"type": "string"}, "lower_pct": {"type": "number"}, "upper_pct": {"type": "number"}, "trim_column": {"type": "string"}, "min_val": {"type": "number"}, "max_val": {"type": "number"}, "output_table": {"type": "string"}, "name": {"type": "string"}}),
    _function("propose_excel_export", "Return an Excel export outline for user review.", {"title": {"type": "string"}, "tables": {"type": "array", "items": {"type": "string"}}, "filename": {"type": "string"}, "summary": {"type": "string"}}),
    _function("propose_report_outline", "Return a report outline for user review.", {"title": {"type": "string"}, "sections": {"type": "array", "items": {"type": "object"}}}),
    _function("propose_ppt_outline", "Return a presentation outline for user review.", {"title": {"type": "string"}, "slides": {"type": "array", "items": {"type": "object"}}}),
    _function("generate_ppt", "Generate a PPTX from a query result and grounded outline.", {"result_id": {"type": "string"}, "title": {"type": "string"}, "filename": {"type": "string"}, "slides": {"type": "array", "items": {"type": "object"}}, "summary": {"type": "string"}, "insights": {"type": "array", "items": {"type": "string"}}}),
    _function("set_ppt_color_scheme", "Select a validated color scheme for later PPT generation.", {"scheme": {"type": "string"}, "colors": {"type": "array", "items": {"type": "string"}}}, ["scheme"]),
    _function("propose_dashboard_outline", "Return a dashboard widget outline for user review.", {"name": {"type": "string"}, "widgets": {"type": "array", "items": {"type": "object"}}}),
    _function("generate_dashboard", "Create a refreshable dashboard from widget query results or SQL.", {"name": {"type": "string"}, "description": {"type": "string"}, "widgets": {"type": "array", "items": {"type": "object"}}, "color_scheme": {"type": "string"}}, ["name", "widgets"]),
    _function("ask_user", "Ask the user for missing information; the question is surfaced as a structured event.", {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6}, "choices": {"type": "array", "items": {"type": "string"}}, "multi_select": {"type": "boolean"}}, ["question"]),
    _function("browse_webpage", "Read bounded text from an explicitly provided public HTTP(S) page.", {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
    _function("list_feishu_bitable_tables", "List tables in a Feishu Bitable using configured application credentials.", {"bitable": {"type": "string"}}, ["bitable"]),
    _function("load_feishu_bitable", "Load a bounded Feishu Bitable snapshot as an analyzable source.", {"bitable": {"type": "string"}, "table_id": {"type": "string"}, "source_name": {"type": "string"}, "max_records": {"type": "integer"}}, ["bitable"]),
    _function("workspace_glob", "Page through safe workspace file metadata.", {"pattern": {"type": "string"}, "path": {"type": "string"}, "max_results": {"type": "integer"}, "cursor": {"type": "integer"}}, ["pattern"]),
    _function("workspace_grep", "Regex-search bounded UTF-8 workspace text files.", {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "max_results": {"type": "integer"}}, ["pattern"]),
    _function("workspace_read_file", "Read a bounded workspace text, document, PDF, or spreadsheet file.", {"file_path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}, "sheet_name": {"type": "string"}}, ["file_path"]),
    _function("structured_output", "Validate and return machine-readable output.", {"output": {}, "required_fields": {"type": "array", "items": {"type": "string"}}}, ["output"]),
    _function("load_analysis_skill", "Load a named analysis Skill SOP.", {"name": {"type": "string"}}, ["name"]),
    _function("task_create", "Create a persistent workspace task.", {"title": {"type": "string"}, "description": {"type": "string"}, "assignee": {"type": "string"}, "blocks": {"type": "array", "items": {"type": "string"}}, "blocked_by": {"type": "array", "items": {"type": "string"}}}, ["title"]),
    _function("task_get", "Get one workspace task.", {"task_id": {"type": "string"}}, ["task_id"]),
    _function("task_list", "List workspace tasks.", {"status": {"type": "string"}, "assignee": {"type": "string"}}),
    _function("task_update", "Update a task and its dependencies.", {"task_id": {"type": "string"}, "status": {"type": "string"}, "assignee": {"type": "string"}, "description": {"type": "string"}, "add_blocks": {"type": "array", "items": {"type": "string"}}, "add_blocked_by": {"type": "array", "items": {"type": "string"}}}, ["task_id"]),
    _function("team_create", "Create a persistent analyst team with a fixed evidence reviewer.", {"name": {"type": "string"}, "description": {"type": "string"}, "members": {"type": "array", "items": {"type": "object"}}}, ["name", "members"]),
    _function("team_delete", "Delete an inactive team; evidence requires force confirmation.", {"name": {"type": "string"}, "force": {"type": "boolean"}}, ["name"]),
    _function("team_list", "List persistent analyst teams."),
    _function("team_status", "Get a team and recent mailbox messages.", {"name": {"type": "string"}}, ["name"]),
    _function("send_message", "Send a team mailbox message.", {"team_name": {"type": "string"}, "recipient": {"type": "string"}, "message": {"type": "string"}}, ["team_name", "recipient", "message"]),
    _function("agent_delegate", "Run one bounded delegated reasoning task with read-only evidence tools.", {"prompt": {"type": "string"}, "description": {"type": "string"}, "team_name": {"type": "string"}, "member_name": {"type": "string"}}, ["prompt"]),
    _function("team_plan_create", "Create and validate a dependency-aware team plan without running it.", {"team_name": {"type": "string"}, "goal": {"type": "string"}, "assignments": {"type": "array", "items": {"type": "object"}}}, ["team_name", "goal", "assignments"]),
    _function("team_delegate", "Start, retry, or revise a bounded parallel team plan.", {"team_name": {"type": "string"}, "goal": {"type": "string"}, "plan_id": {"type": "string"}, "retry_plan_id": {"type": "string"}, "retry_task_ids": {"type": "array", "items": {"type": "string"}}, "review_plan_id": {"type": "string"}, "review_task_ids": {"type": "array", "items": {"type": "string"}}, "assignments": {"type": "array", "items": {"type": "object"}}, "source_ids": {"type": "array", "items": {"type": "string"}}, "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 300}, "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 8}, "result_max_tokens": {"type": "integer", "minimum": 400, "maximum": 2500}}, ["team_name"]),
    _function("workflow_create", "Create and publish an auditable workflow from a built-in template.", {"name": {"type": "string"}, "description": {"type": "string"}, "mode": {"type": "string"}, "template": {"type": "string"}, "source_key": {"type": "string"}}),
    _function("workflow_create_custom", "Create and publish a custom dependency-aware Agent workflow.", {"name": {"type": "string"}, "description": {"type": "string"}, "mode": {"type": "string"}, "source_key": {"type": "string"}, "agents": {"type": "array", "items": {"type": "object"}}}, ["name", "agents"]),
    _function("workflow_list", "List published workspace workflows."),
    _function("workflow_start", "Start a published workflow.", {"name": {"type": "string"}, "workflow_id": {"type": "string"}, "workflow_version_id": {"type": "string"}, "inputs": {"type": "object"}}),
    _function("workflow_status", "Get durable workflow run status and events.", {"run_id": {"type": "string"}}, ["run_id"]),
    _function("read_tool_result", "Read or search a recoverable oversized tool result.", {"artifact_id": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}, "query": {"type": "string"}}, ["artifact_id"]),
    _function("plan_complete", "Return a completed coordinator plan.", {"summary": {"type": "string"}, "steps": {"type": "array", "items": {"type": "object"}}}, ["summary", "steps"]),
]

DEFAULT_EXTRA_TOOL_NAMES = frozenset({
    "workspace_status", "get_table_detail", "create_analysis_table", "clean_data",
    "propose_excel_export", "propose_report_outline", "propose_ppt_outline",
    "generate_ppt", "set_ppt_color_scheme", "propose_dashboard_outline",
    "generate_dashboard", "ask_user", "browse_webpage", "list_feishu_bitable_tables",
    "load_feishu_bitable", "workspace_glob", "workspace_grep", "workspace_read_file",
    "structured_output", "load_analysis_skill", "task_get", "task_list",
    "team_list", "team_status", "workflow_list", "workflow_status", "read_tool_result",
    "plan_complete",
})


@dataclass
class AgentToolContext:
    database: Database
    workspace_id: str
    session_id: str
    source_ids: list[str]
    latest_result_id: str = ""
    knowledge_references: list[dict] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    chart_ids: list[str] = field(default_factory=list)
    mcp_names: dict[str, tuple[str, str]] = field(default_factory=dict)
    analysis_source_id: str = ""
    read_paths: set[str] = field(default_factory=set)
    ppt_color_scheme: dict = field(default_factory=dict)
    outlines: list[dict] = field(default_factory=list)
    dashboard_ids: list[str] = field(default_factory=list)
    tool_result_ids: list[str] = field(default_factory=list)
    knowledge_document_ids: list[str] | None = None
    actor_id: str = ""

    def sources(self) -> list[dict]:
        sources = [self.database.get("sources", source_id) for source_id in self.source_ids]
        missing = [source_id for source_id, source in zip(self.source_ids, sources) if not source]
        if missing:
            raise ValueError(f"数据源不存在或已移除：{', '.join(missing)}")
        foreign = [source["id"] for source in sources if source.get("workspace_id", "default") != self.workspace_id]
        if foreign:
            raise PermissionError(f"数据源不属于当前工作空间：{', '.join(foreign)}")
        return [source for source in sources if source]


def _public_record(value: dict) -> dict:
    return {key: item for key, item in value.items() if key not in {"path", "credential", "text", "chunks"}}


def _mcp_function_name(server_id: str, tool_name: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", f"mcp__{server_id}__{tool_name}")[:64].rstrip("_")
    candidate = base or "mcp_tool"
    index = 2
    while candidate in used:
        suffix = f"_{index}"
        candidate = f"{base[:64 - len(suffix)]}{suffix}"
        index += 1
    used.add(candidate)
    return candidate


def _agent_policy(context: AgentToolContext) -> tuple[bool, bool]:
    session = context.database.get("sessions", context.session_id) or {}
    return bool(session.get("agent_allow_mutations")), bool(session.get("agent_allow_mcp"))


def _allowed_agent_tool_names(context: AgentToolContext) -> set[str]:
    allow_mutations, allow_mcp = _agent_policy(context)
    names = {item["function"]["name"] for item in BUILTIN_TOOLS}
    names.update(
        item["function"]["name"] for item in EXTRA_TOOLS
        if allow_mutations or item["function"]["name"] in DEFAULT_EXTRA_TOOL_NAMES
    )
    if allow_mcp:
        names.update(context.mcp_names)
    return names


def tool_schemas(context: AgentToolContext) -> list[dict]:
    schemas = [BUILTIN_TOOLS[0], *BUILTIN_TOOLS[-2:]]
    if context.source_ids:
        schemas[1:1] = BUILTIN_TOOLS[1:-2]
    allow_mutations, allow_mcp = _agent_policy(context)
    schemas.extend(
        item for item in EXTRA_TOOLS
        if allow_mutations or item["function"]["name"] in DEFAULT_EXTRA_TOOL_NAMES
    )

    used = {item["function"]["name"] for item in schemas}
    for server in context.database.list("mcp_servers", workspace_id=context.workspace_id) if allow_mcp else []:
        if not server.get("enabled", True) or server.get("status") != "connected":
            continue
        for tool in server.get("tools", []):
            raw_name = str(tool.get("name") or "").strip()
            if not raw_name:
                continue
            exposed = _mcp_function_name(str(server["id"]), raw_name, used)
            context.mcp_names[exposed] = (str(server["id"]), raw_name)
            raw_schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
            parameters = {
                "type": "object",
                "properties": raw_schema.get("properties", {}),
            }
            if raw_schema.get("required"):
                parameters["required"] = raw_schema["required"]
            schemas.append(_function(exposed, f"[MCP:{server['id']}] {tool.get('description') or raw_name}"))
            schemas[-1]["function"]["parameters"] = parameters
    return schemas


def _combined_schema(context: AgentToolContext) -> dict:
    sources = context.sources()
    combined = []
    table_counts: dict[str, int] = {}
    schemas = []
    for source in sources:
        schema = schema_for_source(source)
        schemas.append((source, schema))
        for table in schema.get("tables", []):
            table_counts[table["name"]] = table_counts.get(table["name"], 0) + 1
    for source_index, (source, schema) in enumerate(schemas, 1):
        tables = []
        for table in schema.get("tables", []):
            alias = table["name"]
            if table_counts.get(alias, 0) > 1 and source_index > 1:
                alias = re.sub(r"[^\w\u4e00-\u9fff]+", "_", f"{source['name']}_{alias}").strip("_")[:80]
            tables.append({**table, "query_name": alias})
        combined.append({"source_id": source["id"], "source_name": source.get("name"), "tables": tables})
    return {"sources": combined}


def _frame(context: AgentToolContext, args: dict):
    result_id = str(args.get("result_id") or context.latest_result_id or "")
    if result_id:
        record = context.database.get("query_results", result_id)
        if not record or record.get("workspace_id", "default") != context.workspace_id:
            raise ValueError("查询结果不存在或不属于当前工作空间")
        return load_result_frame(result_id), result_id
    source_id = str(args.get("source_id") or (context.source_ids[0] if context.source_ids else ""))
    source = context.database.get("sources", source_id)
    if not source or source.get("workspace_id", "default") != context.workspace_id:
        raise ValueError("请选择当前工作空间中的数据源或查询结果")
    return source_table(source, args.get("table") or args.get("table_name"))[1], ""


def _search_mcp(context: AgentToolContext, query: str, limit: int, server_filter: str = "") -> list[dict]:
    terms = {part for part in re.split(r"\W+", query.lower()) if part}
    ranked = []
    for exposed, (server_id, raw_name) in context.mcp_names.items():
        if server_filter and server_id != server_filter:
            continue
        server = context.database.get("mcp_servers", server_id) or {}
        tool = next((item for item in server.get("tools", []) if item.get("name") == raw_name), {})
        haystack = f"{raw_name} {tool.get('description', '')}".lower()
        score = sum(term in haystack for term in terms)
        if score:
            ranked.append((score, {"name": exposed, "server_id": server_id, "tool": raw_name, "description": tool.get("description", "")}))
    ranked.sort(key=lambda item: (-item[0], item[1]["name"]))
    return [item for _, item in ranked[:limit]]


def _named_record(context: AgentToolContext, collection: str, name: str) -> dict:
    item = next(
        (
            value for value in context.database.list(collection, workspace_id=context.workspace_id, limit=5000)
            if value.get("id") == name or value.get("name") == name
        ),
        None,
    )
    if not item:
        raise ValueError(f"找不到 {name}")
    return item


def _feishu_credentials(context: AgentToolContext) -> dict:
    candidates = [
        item for item in context.sources()
        if item.get("kind") == "lark_table" and item.get("credential")
    ] if context.source_ids else []
    if not candidates:
        candidates = [
            item for item in context.database.list("sources", workspace_id=context.workspace_id, limit=5000)
            if item.get("kind") == "lark_table" and item.get("credential")
        ]
    if not candidates:
        candidates = [
            item for item in context.database.list("connectors", workspace_id=context.workspace_id, limit=5000)
            if item.get("type") == "lark_app" and item.get("credential")
        ]
    if not candidates:
        raise ValueError("请先配置飞书应用凭据或连接一个飞书多维表格数据源")
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(candidates[0]["credential"], {})
    if not secret.get("app_id") or not secret.get("app_secret"):
        raise ValueError("飞书应用凭据不完整")
    return secret


def _task(context: AgentToolContext, task_id: str) -> dict:
    item = context.database.get("tasks", task_id)
    if not item or item.get("workspace_id", "default") != context.workspace_id:
        raise ValueError("任务不存在或不属于当前工作空间")
    return item


def _assert_task_graph(context: AgentToolContext, candidate: dict) -> None:
    tasks = {
        item["id"]: item
        for item in context.database.list("tasks", workspace_id=context.workspace_id, limit=5000)
    }
    tasks[candidate["id"]] = candidate
    graph = {task_id: set(item.get("blocked_by") or []) for task_id, item in tasks.items()}
    for task_id, dependencies in graph.items():
        missing = dependencies - tasks.keys()
        if missing:
            raise ValueError(f"任务 {task_id} 引用不存在的依赖：{', '.join(sorted(missing))}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("任务依赖存在环路")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph.get(task_id, set()):
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)


def _dashboard_from_tool(context: AgentToolContext, args: dict) -> dict:
    widgets = args.get("widgets")
    if not isinstance(widgets, list) or not widgets or len(widgets) > 50:
        raise ValueError("看板需要 1–50 个组件")
    built = []
    for index, raw in enumerate(widgets):
        if not isinstance(raw, dict):
            raise ValueError("看板组件必须是对象")
        widget = dict(raw)
        result_id = str(widget.get("result_id") or "")
        if widget.get("sql"):
            query = execute_query(
                context.source_ids, str(widget["sql"]), context.workspace_id,
                int(widget.get("limit", 1000)),
            )
            result_id = query["id"]
        if not result_id:
            result_id = context.latest_result_id
        if not result_id:
            raise ValueError(f"看板组件 {index + 1} 缺少 SQL 或 result_id")
        result_record = context.database.get("query_results", result_id)
        if not result_record or result_record.get("workspace_id", "default") != context.workspace_id:
            raise ValueError("查询结果不存在或不属于当前工作空间")
        frame = load_result_frame(result_id)
        kind = str(widget.get("type") or widget.get("chart_type") or "")
        base = {
            **widget, "id": str(widget.get("id") or context.database.new_id("widget")),
            "title": str(widget.get("title") or f"组件 {index + 1}")[:100], "result_id": result_id,
        }
        if kind in {"kpi", "KPI_Card"}:
            row = frame.iloc[0] if not frame.empty else None
            base.update({
                "type": "kpi", "kpi_value": str(row.iloc[0]) if row is not None else "—",
                "kpi_sub": str(row.iloc[1]) if row is not None and len(row) > 1 else "",
            })
        else:
            base["chart"] = make_spec(
                frame, chart_type=kind or None, title=base["title"],
                x=widget.get("x"), y=widget.get("y"), group=widget.get("group"),
                options=widget.get("options") or {},
            )
        built.append(base)
    return context.database.put(
        "dashboards",
        {
            "id": context.database.new_id("dash"), "workspace_id": context.workspace_id,
            "name": str(args.get("name") or "分析看板")[:100],
            "description": str(args.get("description") or "")[:500],
            "widgets": built, "layout": {"columns": 12}, "revision": 1,
        },
        workspace_id=context.workspace_id,
    )


def execute_tool(name: str, args: dict, context: AgentToolContext) -> tuple[dict, list[tuple[str, dict]]]:
    if name not in _allowed_agent_tool_names(context):
        raise PermissionError(f"会话策略未授权 Agent 调用工具：{name}")
    events: list[tuple[str, dict]] = []
    if name == "query_knowledge":
        rows = search_knowledge(
            str(args.get("question") or ""), context.workspace_id, int(args.get("limit", 5)),
            context.knowledge_document_ids,
        )
        context.knowledge_references.extend(
            {"document_id": item["document_id"], "chunk": item["chunk"]} for item in rows
        )
        return {"items": rows}, events
    if name == "get_schema":
        return _combined_schema(context), events
    if name == "query_data":
        result = execute_query(context.source_ids, str(args.get("sql") or ""), context.workspace_id, int(args.get("limit", 1000)))
        context.latest_result_id = result["id"]
        public = _public_record(result)
        events.extend([("plan", {"sql": result["sql"], "assumptions": []}), ("table", public)])
        return public, events
    if name == "profile_data":
        frame, result_id = _frame(context, args)
        columns = args.get("columns")
        if columns:
            missing = [str(value) for value in columns if value not in frame.columns]
            if missing:
                raise ValueError(f"分析字段不存在：{', '.join(missing)}")
            frame = frame[[str(value) for value in columns]]
        from ..data_cleaning import profile as cleaning_profile

        markdown, _plotly_charts = cleaning_profile(frame, None)
        structured = profile(frame)
        if len(frame) > 1 and len(structured["numeric_columns"]) > 0:
            chart = make_spec(
                frame, chart_type="histogram", title="数值列分布",
                x=structured["numeric_columns"][0],
            )
            events.append(("chart", chart))
        return {"result_id": result_id or None, "profile": structured, "markdown": markdown}, events
    if name == "run_analysis":
        if args.get("sql") and not args.get("result_id"):
            queried = execute_query(
                context.source_ids, str(args["sql"]), context.workspace_id, int(args.get("limit", 5000)),
            )
            context.latest_result_id = queried["id"]
        frame, result_id = _frame(context, args)
        method = str(args.get("method") or args.get("analysis_name") or "").strip()
        if not method:
            raise ValueError("run_analysis 需要 method 或 analysis_name")
        params = dict(args.get("params") or {})
        for key in ("target_column", "groupby_column", "n_deciles", "analysis_options"):
            if key in args:
                params[key] = args[key]
        analysis, frames = run_analysis_with_frames(frame, method, params)
        derived = None
        result_ids = {}
        if frames:
            derived = register_derived_tables(
                frames, context.workspace_id, name=f"{method} 分析结果",
            )
            if context.analysis_source_id in context.source_ids:
                context.source_ids.remove(context.analysis_source_id)
            context.analysis_source_id = derived["id"]
            context.source_ids.append(derived["id"])
            for table in derived.get("tables", []):
                safe_table = str(table["name"]).replace('"', '""')
                query = execute_query(
                    [derived["id"]], f'SELECT * FROM "{safe_table}"', context.workspace_id, 5000,  # noqa: S608
                )
                result_ids[table["name"]] = query["id"]
            if result_ids:
                context.latest_result_id = next(iter(result_ids.values()))
        record = context.database.put(
            "analysis_runs",
            {
                "id": context.database.new_id("ana"), "workspace_id": context.workspace_id,
                "session_id": context.session_id, "method": analysis["method"],
                "inputs": {"result_id": result_id or None, "source_id": args.get("source_id"), "params": params},
                "result": analysis["result"], "status": "completed",
                "derived_source_id": derived["id"] if derived else None,
                "result_ids": result_ids,
            },
            workspace_id=context.workspace_id,
        )
        return _public_record(record), events
    if name == "select_chart":
        result_id = ""
        columns = [str(value) for value in args.get("available_columns") or []]
        if args.get("result_id") or (not columns and context.latest_result_id):
            frame, result_id = _frame(context, args)
            columns = [str(value) for value in frame.columns]
        candidates = select_charts(str(args.get("user_intent") or ""), columns, 3)
        return {
            "result_id": result_id or None, "recommended": candidates[0]["type"],
            "candidates": candidates, "catalog": chart_catalog(),
        }, events
    if name == "generate_chart":
        if args.get("sql") and not args.get("result_id"):
            query = execute_query(
                context.source_ids, str(args["sql"]), context.workspace_id,
                int(args.get("limit", 5000)),
            )
            context.latest_result_id = query["id"]
        frame, result_id = _frame(context, args)
        mapping = args.get("field_mapping") if isinstance(args.get("field_mapping"), dict) else {}
        requested_type = str(args.get("type") or args.get("chart_type") or "")
        chart_type = normalize_chart_type(requested_type, mapping)
        explicit_y = args.get("y")
        if explicit_y is None:
            explicit_y = mapping.get("value_cols") or mapping.get("y")
        if explicit_y is None:
            roles = (
                "value", "values", "actual", "target", "start", "end", "left_value", "right_value",
                "z", "weight", "longitude", "latitude", "size",
            )
            explicit_y = [mapping[role] for role in roles if mapping.get(role) is not None]
        x = args.get("x")
        if not x:
            for role in ("x", "time", "category", "label", "group", "source", "names", "labels"):
                value = mapping.get(role)
                if isinstance(value, str):
                    x = value
                    break
        if chart_type in {"boxplot", "violin", "beeswarm"} and mapping.get("x"):
            x = mapping["x"]
        if chart_type == "dot_map" and mapping.get("longitude") and mapping.get("latitude"):
            explicit_y = [mapping["longitude"], mapping["latitude"]]
            if mapping.get("value"):
                explicit_y.append(mapping["value"])
        if chart_type in {"scatter", "bubble", "connected_scatter"} and isinstance(mapping.get("size"), str):
            relationship_y = [explicit_y] if isinstance(explicit_y, str) else list(explicit_y or [])
            if mapping["size"] not in relationship_y:
                relationship_y.append(mapping["size"])
            explicit_y = relationship_y
        if chart_type == "parallel" and isinstance(mapping.get("dimensions"), list):
            dimensions = [str(value) for value in mapping["dimensions"]]
            selected_dimensions = [value for value in dimensions if value in frame.columns]
            if isinstance(mapping.get("color"), str) and mapping["color"] in frame.columns:
                selected_dimensions.append(mapping["color"])
            frame = frame[list(dict.fromkeys(selected_dimensions))]
            x, explicit_y = (dimensions[0] if dimensions else None), dimensions[1:]
        ordered_roles = {
            "heatmap": ("x", "y", "value"),
            "sankey": ("source", "target", "value"),
            "chord": ("source", "target", "value"),
            "network": ("source", "target", "weight"),
        }.get(str(chart_type))
        if chart_type == "network" and not mapping.get("source") and mapping.get("x") and mapping.get("y"):
            ordered_roles = ("x", "y", "z")
        if ordered_roles:
            ordered = [str(mapping[role]) for role in ordered_roles if isinstance(mapping.get(role), str)]
            if len(ordered) != len(set(ordered)):
                raise ValueError(f"{requested_type or chart_type} 的字段角色必须映射到不同列")
            if len(ordered) >= 2 and all(value in frame.columns for value in ordered):
                frame = frame[ordered]
                x, explicit_y = ordered[0], ordered[1:]
        options = {**(args.get("options") or {})}
        for role in (
            "parents", "color", "category", "type", "low", "medium", "high",
            "highlight", "order", "x_mid", "y_mid",
        ):
            if mapping.get(role) is not None:
                options[role] = mapping[role]
        if requested_type == "Marimekko_PCT":
            options["percent"] = True
        spec = make_spec(
            frame, chart_type=chart_type, title=str(args.get("title") or "分析结果"),
            x=x, y=explicit_y, group=args.get("group") or mapping.get("series") or mapping.get("group"),
            options=options,
        )
        spec["catalog_chart_id"] = requested_type or None
        chart = context.database.put(
            "charts",
            {
                "id": context.database.new_id("chart"), "workspace_id": context.workspace_id,
                "name": spec["title"], "spec": spec, "result_id": result_id or None,
                "session_id": context.session_id,
                "actor_id": context.actor_id,
            },
            workspace_id=context.workspace_id,
        )
        context.chart_ids.append(chart["id"])
        events.append(("chart", spec))
        return _public_record(chart), events
    if name in {"export_excel", "export_report"}:
        payload = dict(args)
        payload["result_id"] = str(payload.get("result_id") or context.latest_result_id or "")
        if name == "export_excel" and args.get("tables"):
            requested = {str(value) for value in args.get("tables") or []}
            frames = {}
            for source in context.sources():
                for table in schema_for_source(source).get("tables", []):
                    table_name = str(table["name"])
                    if "*" not in requested and table_name not in requested:
                        continue
                    _resolved_name, frame = source_table(source, table_name)
                    output_name = table_name
                    if output_name in frames:
                        output_name = f"{source.get('name', source['id'])}_{table_name}"
                    frames[output_name] = frame
            if not frames:
                raise ValueError("没有找到待导出的表")
            payload["frames"] = frames
            payload["format"] = "xlsx"
            payload["title"] = args.get("title") or args.get("filename") or "数据导出"
        elif not payload["result_id"] and not payload.get("sections"):
            raise ValueError("导出前必须先获得查询结果或提供报告章节")
        artifact = export_data(payload, context.workspace_id) if name == "export_excel" else export_report(payload, context.workspace_id)
        context.artifact_ids.append(artifact["id"])
        public = _public_record(artifact)
        public["download_url"] = f"/api/artifacts/{artifact['id']}/download"
        events.append(("artifact", public))
        return public, events
    if name == "memory_read":
        query = str(args.get("name") or args.get("query") or "")
        return {
            "items": search_memories(
                query, context.workspace_id,
                max(1, min(int(args.get("limit", 12)), 20)),
                context.actor_id,
            ),
        }, events
    if name == "search_mcp_tools":
        return {"items": _search_mcp(
            context, str(args.get("query") or ""), max(1, min(int(args.get("limit", 5)), 10)),
            str(args.get("server") or ""),
        )}, events
    if name == "workspace_status":
        return WorkspaceFiles(
            context.database, context.workspace_id, context.read_paths, context.session_id,
        ).status(), events
    if name == "get_table_detail":
        source_id = str(args.get("source_id") or (context.source_ids[0] if context.source_ids else ""))
        source = context.database.get("sources", source_id)
        if not source or source.get("workspace_id", "default") != context.workspace_id:
            raise ValueError("数据源不存在或不属于当前工作空间")
        schema = schema_for_source(source)
        table_name = args.get("table") or args.get("table_name")
        if not table_name:
            raise ValueError("get_table_detail 需要 table 或 table_name")
        table = next(
            (item for item in schema["tables"] if item["name"] == table_name or item["source_name"] == table_name),
            None,
        )
        if not table:
            raise ValueError(f"数据表不存在：{table_name}")
        return {"source_id": source_id, "table": table}, events
    if name == "create_analysis_table":
        query = execute_query(context.source_ids, str(args.get("sql") or ""), context.workspace_id, 5000)
        frame = load_result_frame(query["id"])
        derived = register_derived_tables(
            {str(args.get("table_name") or "analysis_data"): frame},
            context.workspace_id, name=str(args.get("table_name") or "分析表"),
        )
        context.source_ids.append(derived["id"])
        context.analysis_source_id = derived["id"]
        context.latest_result_id = query["id"]
        return {"source": _public_record(derived), "result": _public_record(query)}, events
    if name == "delete_analysis_tables":
        if args.get("confirm") is not True:
            raise PermissionError("删除分析表需要 confirm=true")
        if args.get("table_names"):
            result = delete_derived_tables(
                [str(value) for value in args.get("table_names") or []], context.workspace_id,
            )
            for source_id in result["archived_sources"]:
                if source_id in context.source_ids:
                    context.source_ids.remove(source_id)
            return result, events
        archived = []
        for source_id in args.get("source_ids") or []:
            source = context.database.get("sources", str(source_id))
            if not source or source.get("workspace_id", "default") != context.workspace_id:
                raise ValueError("分析表数据源不存在")
            if source.get("kind") != "derived":
                raise PermissionError("原始数据源受保护，不能通过分析表工具删除")
            context.database.archive("sources", source["id"])
            if source["id"] in context.source_ids:
                context.source_ids.remove(source["id"])
            archived.append(source["id"])
        return {"archived": archived}, events
    if name == "clean_data":
        frame, _result_id = _frame(context, args)
        operations = args.get("operations")
        if operations is not None:
            if not isinstance(operations, list):
                raise ValueError("operations 必须是数组")
            cleaned, operation_log = clean_frame(frame, operations)
        else:
            from ..data_cleaning import fill_missing, trim, winsorize

            operation = str(args.get("operation") or "")
            columns = [str(value) for value in args.get("columns") or []] or None
            if operation == "fill_na":
                cleaned, summary = fill_missing(frame, str(args.get("fill_method") or "mean"), columns)
            elif operation == "winsorize":
                cleaned, summary = winsorize(
                    frame, float(args.get("lower_pct", 1)), float(args.get("upper_pct", 99)), columns,
                )
            elif operation == "trimming":
                if args.get("min_val") is None or args.get("max_val") is None:
                    raise ValueError("trimming 需要 min_val 和 max_val")
                cleaned, summary = trim(
                    frame, str(args.get("trim_column") or ""),
                    float(args["min_val"]), float(args["max_val"]),
                )
            else:
                raise ValueError("operation 必须是 fill_na、winsorize 或 trimming")
            if summary.startswith("❌") or summary.startswith("⚠"):
                raise ValueError(summary)
            operation_log = [{
                "operation": operation, "rows_before": len(frame), "rows_after": len(cleaned),
                "summary": summary,
            }]
        derived = register_derived_tables(
            {str(args.get("output_table") or "data"): cleaned}, context.workspace_id,
            name=str(args.get("name") or args.get("output_table") or "清洗结果"),
        )
        context.source_ids.append(derived["id"])
        table_name = str(args.get("output_table") or "data")
        safe_table = table_name.replace('"', '""')
        query = execute_query(  # The table identifier is escaped immediately above.
            [derived["id"]], f'SELECT * FROM "{safe_table}"', context.workspace_id, 5000,  # noqa: S608
        )
        context.latest_result_id = query["id"]
        return {"source": _public_record(derived), "result_id": query["id"], "operations": operation_log}, events
    if name in {"propose_excel_export", "propose_report_outline", "propose_ppt_outline", "propose_dashboard_outline"}:
        event_type = {
            "propose_excel_export": "excel_outline", "propose_report_outline": "report_outline",
            "propose_ppt_outline": "ppt_outline", "propose_dashboard_outline": "dashboard_outline",
        }[name]
        proposal = {"type": event_type, **args, "requires_confirmation": True}
        context.outlines.append(proposal)
        events.append(("outline", proposal))
        return proposal, events
    if name == "generate_ppt":
        payload = {
            **args, "format": "pptx", "result_id": args.get("result_id") or context.latest_result_id,
            "color_scheme": args.get("color_scheme") or context.ppt_color_scheme,
        }
        if not payload.get("result_id") and not payload.get("slides"):
            raise ValueError("PPT 生成需要 slides 大纲或查询结果")
        artifact = export_report(payload, context.workspace_id)
        context.artifact_ids.append(artifact["id"])
        public = _public_record(artifact) | {"download_url": f"/api/artifacts/{artifact['id']}/download"}
        events.append(("artifact", public))
        return public, events
    if name == "set_ppt_color_scheme":
        builtins = {
            "mckinsey": ["#003B71", "#005CAB", "#0083CA", "#00A3E0", "#7FBA00", "#FFC000"],
            "bcg": ["#006C5B", "#009879", "#00B398", "#CDECE5", "#A6192E", "#999999"],
            "bain": ["#E41E26", "#FF5C5C", "#A6192E", "#F4E8E9", "#00B398", "#999999"],
            "ey": ["#FFD100", "#FFED70", "#75787B", "#D9D9D6", "#7FBA00", "#DA3B01"],
        }
        scheme = str(args.get("scheme") or "mckinsey").lower()
        colors = args.get("colors") or builtins.get(scheme)
        if not isinstance(colors, list) or not 3 <= len(colors) <= 12:
            raise ValueError("配色必须包含 3–12 个颜色")
        normalized = []
        for color in colors:
            value = str(color).strip().upper()
            if not re.fullmatch(r"#[0-9A-F]{6}", value):
                raise ValueError(f"无效颜色：{color}")
            normalized.append(value)
        context.ppt_color_scheme = {"name": scheme, "colors": normalized}
        return context.ppt_color_scheme, events
    if name == "generate_dashboard":
        dashboard = _dashboard_from_tool(context, args)
        context.dashboard_ids.append(dashboard["id"])
        events.append(("dashboard", {"id": dashboard["id"], "name": dashboard["name"]}))
        return {"dashboard": dashboard, "url": f"/api/dashboards/{dashboard['id']}"}, events
    if name == "ask_user":
        question = str(args.get("question") or "").strip()
        choices = args.get("options") if args.get("options") is not None else args.get("choices")
        if not question:
            raise ValueError("ask_user 需要非空 question")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 6:
            raise ValueError("ask_user 需要 2–6 个 options/choices")
        normalized = [str(value).strip()[:40] for value in choices]
        if any(not value for value in normalized):
            raise ValueError("ask_user 选项不能为空")
        interaction = {
            "question": question[:120], "choices": normalized, "options": normalized,
            "multi_select": bool(args.get("multi_select", False)),
        }
        events.append(("ask_user", interaction))
        return {**interaction, "status": "awaiting_user_reply"}, events
    if name == "browse_webpage":
        response = safe_http_request("GET", str(args.get("url") or ""), timeout=20)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if not any(value in content_type.lower() for value in ("text/", "json", "xml", "html")):
            raise ValueError("只能读取文本类网页")
        text = response.text
        if "html" in content_type.lower():
            text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
        limit = max(100, min(int(args.get("max_chars", 12000)), 20000))
        return {"url": response.url, "status": response.status_code, "content": text[:limit]}, events
    if name in {"list_feishu_bitable_tables", "load_feishu_bitable"}:
        from .feishu import list_tables, read_records

        credentials = _feishu_credentials(context)
        if name == "list_feishu_bitable_tables":
            return list_tables(credentials, args.get("bitable")), events
        if name == "load_feishu_bitable":
            loaded = read_records(
                credentials, args.get("bitable"), args.get("table_id", ""),
                max(1, min(int(args.get("max_records", 500)), 500)),
            )
            frame = pd.json_normalize(loaded["records"])
            if not len(frame.columns):
                raise ValueError("飞书数据表中没有可分析字段")
            source = register_derived_tables(
                {"data": frame}, context.workspace_id,
                name=str(args.get("source_name") or "飞书多维表格快照"),
            )
            source = context.database.patch("sources", source["id"], {
                "kind": "lark_table_snapshot", "endpoint": loaded["url"],
                "lineage": {
                    "operation": "feishu_snapshot", "app_token": loaded["app_token"],
                    "table_id": loaded["table_id"], "limited": loaded["limited"],
                },
            }) or source
            context.source_ids.append(source["id"])
            return {"source": _public_record(source), **{key: loaded[key] for key in ("url", "record_count", "limited")}}, events
    if name.startswith("workspace_"):
        files = WorkspaceFiles(context.database, context.workspace_id, context.read_paths, context.session_id)
        if name == "workspace_glob":
            return files.glob(args.get("pattern", "*"), args.get("path", ""), args.get("max_results", 100), args.get("cursor", 0)), events
        if name == "workspace_grep":
            return files.grep(args.get("pattern", ""), args.get("path", ""), args.get("include", "**/*"), args.get("max_results", 50)), events
        if name == "workspace_read_file":
            return files.read(args["file_path"], offset=args.get("offset", 0), limit=args.get("limit", 400), sheet_name=args.get("sheet_name", "")), events
    if name == "structured_output":
        output = args.get("output")
        missing = [field for field in args.get("required_fields") or [] if not isinstance(output, dict) or field not in output]
        if missing:
            raise ValueError(f"结构化输出缺少字段：{', '.join(missing)}")
        return {"output": output, "valid": True}, events
    if name == "load_analysis_skill":
        from .skills import get_skill, public_skill

        skill = get_skill(str(args.get("name") or ""), context.workspace_id)
        if not skill:
            raise ValueError("Skill 不存在")
        return public_skill(skill, include_prompt=True), events
    if name == "task_create":
        item = {
            "id": context.database.new_id("task"), "workspace_id": context.workspace_id,
            "title": str(args.get("title") or "")[:200], "description": str(args.get("description") or "")[:8000],
            "assignee": str(args.get("assignee") or "")[:100], "status": "pending",
            "blocks": [str(value) for value in args.get("blocks") or []],
            "blocked_by": [str(value) for value in args.get("blocked_by") or []],
        }
        _assert_task_graph(context, item)
        return context.database.put("tasks", item, workspace_id=context.workspace_id), events
    if name == "task_get":
        return _task(context, str(args.get("task_id") or "")), events
    if name == "task_list":
        items = context.database.list("tasks", workspace_id=context.workspace_id, limit=5000)
        for key in ("status", "assignee"):
            if args.get(key):
                items = [item for item in items if item.get(key) == args[key]]
        return {"items": items}, events
    if name == "task_update":
        item = _task(context, str(args.get("task_id") or ""))
        for key in ("status", "assignee", "description"):
            if key in args:
                item[key] = args[key]
        if item.get("status") not in {"pending", "in_progress", "completed", "blocked"}:
            raise ValueError("任务状态无效")
        item["blocks"] = list(dict.fromkeys([*(item.get("blocks") or []), *(args.get("add_blocks") or [])]))
        item["blocked_by"] = list(dict.fromkeys([*(item.get("blocked_by") or []), *(args.get("add_blocked_by") or [])]))
        _assert_task_graph(context, item)
        return context.database.put("tasks", item, workspace_id=context.workspace_id), events
    if name in {
        "team_create", "team_delete", "team_list", "team_status", "send_message",
        "agent_delegate", "team_plan_create", "team_delegate",
    }:
        teams = context.database.list("teams", workspace_id=context.workspace_id, limit=5000)
        if name == "team_create":
            members = args.get("members")
            if not isinstance(members, list) or not 1 <= len(members) <= 8:
                raise ValueError("团队需要 1–8 名成员")
            team_name = str(args.get("name") or "").strip()[:100]
            if not team_name or any(item.get("name") == team_name for item in teams):
                raise ValueError("团队名称为空或已存在")
            normalized = []
            names = set()
            for index, raw in enumerate(members, 1):
                if not isinstance(raw, dict):
                    raise ValueError("团队成员必须是对象")
                profile = context.database.get("agent_profiles", str(raw.get("profile_id") or ""))
                if profile and profile.get("workspace_id", "default") != context.workspace_id:
                    raise PermissionError("成员配置不属于当前工作空间")
                member_name = str(raw.get("name") or (profile or {}).get("name") or f"成员 {index}")[:100]
                if member_name in names:
                    raise ValueError("团队成员名称不能重复")
                names.add(member_name)
                normalized.append({
                    **raw, "name": member_name,
                    "role": str(raw.get("role") or (profile or {}).get("role") or "分析顾问")[:1000],
                    "instructions": str(raw.get("instructions") or (profile or {}).get("instructions") or "")[:8000],
                    "tools": raw.get("tools") or (profile or {}).get("tools") or ["query", "analysis", "knowledge"],
                })
            item = context.database.put(
                "teams",
                {
                    "id": context.database.new_id("team"), "workspace_id": context.workspace_id,
                    "name": team_name, "objective": str(args.get("description") or "")[:2000],
                    "members": normalized, "lead_profile_id": normalized[0].get("profile_id"),
                    "quality_reviewer": {"name": "固定证据复核员", "role": "quality_reviewer"},
                    "status": "ready",
                },
                workspace_id=context.workspace_id,
            )
            return item, events
        if name == "team_list":
            return {"items": teams}, events
        if name == "agent_delegate" and not args.get("team_name"):
            from .teams import delegate_once

            return delegate_once(
                team=None, member=None, prompt=str(args.get("prompt") or ""),
                description=str(args.get("description") or ""), workspace_id=context.workspace_id,
                source_ids=context.source_ids, session_id=context.session_id, actor_id=context.actor_id,
            ), events
        team = next((item for item in teams if item.get("name") == args.get("team_name") or item.get("name") == args.get("name")), None)
        if not team:
            raise ValueError("团队不存在")
        if name == "team_delete":
            runs = [
                item for item in context.database.list("team_runs", workspace_id=context.workspace_id, limit=5000)
                if item.get("team_id") == team["id"]
            ]
            if any(item.get("status") in {"queued", "running"} for item in runs):
                raise PermissionError("运行中的团队不能删除")
            messages = [
                item for item in context.database.list("team_messages", workspace_id=context.workspace_id, limit=5000)
                if item.get("team_id") == team["id"]
            ]
            has_evidence = bool(messages or runs)
            if has_evidence and args.get("force") is not True:
                raise PermissionError("团队有邮箱或复核证据，查看 team_status 后需 force=true")
            context.database.archive("teams", team["id"])
            return {"archived": True, "team_id": team["id"], "evidence_retained": has_evidence}, events
        if name == "team_status":
            messages = [item for item in context.database.list("team_messages", workspace_id=context.workspace_id) if item.get("team_id") == team["id"]]
            runs = [item for item in context.database.list("team_runs", workspace_id=context.workspace_id) if item.get("team_id") == team["id"]]
            plans = [item for item in context.database.list("team_plans", workspace_id=context.workspace_id) if item.get("team_id") == team["id"]]
            return {"team": team, "messages": messages[:100], "runs": runs[:50], "plans": plans[:50]}, events
        if name == "send_message":
            item = context.database.put(
                "team_messages",
                {
                    "id": context.database.new_id("teammsg"), "workspace_id": context.workspace_id,
                    "team_id": team["id"], "sender": "leader", "recipients": [args.get("recipient", "*")],
                    "content": str(args.get("message") or "")[:8000], "read_by": [],
                },
                workspace_id=context.workspace_id,
            )
            return item, events
        from .teams import create_team_plan, delegate_once, retry_team_run, start_team_plan, start_team_run

        if name == "agent_delegate":
            member = None
            if args.get("member_name"):
                member = next((item for item in team["members"] if item.get("name") == args["member_name"]), None)
                if not member:
                    raise ValueError("团队成员不存在")
            return delegate_once(
                team=team, member=member or team["members"][0], prompt=str(args.get("prompt") or ""),
                description=str(args.get("description") or ""), workspace_id=context.workspace_id,
                source_ids=context.source_ids, session_id=context.session_id, actor_id=context.actor_id,
            ), events
        if name == "team_plan_create":
            return create_team_plan(team, {
                "goal": args.get("goal"), "assignments": args.get("assignments"),
                "source_ids": context.source_ids,
            }), events
        if args.get("plan_id"):
            plan = _named_record(context, "team_plans", str(args["plan_id"]))
            plan, run, job = start_team_plan(team, plan, {
                "source_ids": args.get("source_ids") or context.source_ids,
                "session_id": context.session_id,
                "actor_id": context.actor_id,
                "timeout_seconds": args.get("timeout_seconds"),
                "max_concurrency": args.get("max_concurrency"),
                "result_max_tokens": args.get("result_max_tokens"),
            })
            return {"plan": plan, "run": run, "job": job}, events
        if args.get("review_plan_id"):
            plan = _named_record(context, "team_plans", str(args["review_plan_id"]))
            if not plan.get("run_id"):
                raise ValueError("团队计划尚未执行")
            run = _named_record(context, "team_runs", str(plan["run_id"]))
            if run.get("status") != "needs_review":
                raise ValueError("只有 needs_review 状态的团队计划可按复核意见修订")
            review_ids = [str(value) for value in args.get("review_task_ids") or []]
            selected = review_ids or [item["id"] for item in run.get("tasks") or []]
            reset = retry_team_run(run, selected)
            rerun, job = start_team_run(team, {
                "timeout_seconds": args.get("timeout_seconds"),
                "max_concurrency": args.get("max_concurrency"),
                "result_max_tokens": args.get("result_max_tokens"),
            }, existing_run=reset)
            context.database.patch("team_plans", plan["id"], {"status": "running"})
            return {"plan": plan, "run": rerun, "job": job, "review_task_ids": selected}, events
        if args.get("retry_plan_id"):
            plan = _named_record(context, "team_plans", str(args["retry_plan_id"]))
            if not plan.get("run_id"):
                raise ValueError("团队计划尚未执行")
            run = _named_record(context, "team_runs", str(plan["run_id"]))
            reset = retry_team_run(run, [str(value) for value in args.get("retry_task_ids") or []] or None)
            rerun, job = start_team_run(team, {
                "timeout_seconds": args.get("timeout_seconds"),
                "max_concurrency": args.get("max_concurrency"),
                "result_max_tokens": args.get("result_max_tokens"),
            }, existing_run=reset)
            context.database.patch("team_plans", plan["id"], {"status": "running"})
            return {"plan": plan, "run": rerun, "job": job}, events

        run, job = start_team_run(team, {
            "task": args.get("goal"), "assignments": args.get("assignments"),
            "source_ids": args.get("source_ids") or context.source_ids,
            "session_id": context.session_id,
            "actor_id": context.actor_id,
            "timeout_seconds": args.get("timeout_seconds"),
            "max_concurrency": args.get("max_concurrency"),
            "result_max_tokens": args.get("result_max_tokens"),
        })
        return {"run": run, "job": job}, events
    if name in {"workflow_create", "workflow_create_custom"}:
        from .workflows import create_published_workflow, template_definition

        mode = str(args.get("mode") or "full_auto")
        if name == "workflow_create":
            template = str(args.get("template") or "analysis")
            definition = template_definition(template, str(args.get("source_key") or "source_ids"))
        else:
            agents = args.get("agents")
            if not isinstance(agents, list) or not 1 <= len(agents) <= 8:
                raise ValueError("自定义工作流需要 1–8 个 Agent")
            names = [str(item.get("name") or "").strip() for item in agents if isinstance(item, dict)]
            if len(names) != len(agents) or any(not value for value in names) or len(set(names)) != len(names):
                raise ValueError("Agent 名称不能为空或重复")
            name_to_id = {value: f"agent_{index}" for index, value in enumerate(names, 1)}
            steps = []
            seen = set()
            for raw, agent_name in zip(agents, names):
                instructions = str(raw.get("instructions") or "").strip()
                if not instructions:
                    raise ValueError(f"Agent {agent_name} 缺少 instructions")
                dependencies = [str(value) for value in raw.get("depends_on") or []]
                if any(value not in seen for value in dependencies):
                    raise ValueError(f"Agent {agent_name} 只能依赖列表中更早的 Agent")
                allowed = raw.get("allowed_tools") or ["get_schema", "query_data"]
                if any(value not in {"get_schema", "query_data"} for value in allowed):
                    raise PermissionError("自定义工作流 Agent 只能使用 get_schema 和 query_data")
                profile = context.database.put(
                    "agent_profiles",
                    {
                        "id": context.database.new_id("profile"), "workspace_id": context.workspace_id,
                        "name": agent_name, "role": str(raw.get("role") or agent_name)[:1000],
                        "instructions": instructions[:8000], "tools": allowed, "enabled": True,
                    },
                    workspace_id=context.workspace_id,
                )
                steps.append({
                    "id": name_to_id[agent_name], "name": agent_name, "type": "agent",
                    "depends_on": [name_to_id[value] for value in dependencies],
                    "config": {
                        "prompt": instructions, "agent_profile_id": profile["id"],
                        "allowed_tools": allowed,
                    },
                })
                seen.add(agent_name)
            definition = {"steps": steps, "source_key": str(args.get("source_key") or "source_ids")}
        workflow = create_published_workflow(
            workspace_id=context.workspace_id, name=str(args.get("name") or "分析工作流"),
            description=str(args.get("description") or ""), definition=definition, mode=mode,
        )
        return workflow, events
    if name in {"workflow_list", "workflow_start", "workflow_status"}:
        if name == "workflow_list":
            return {"items": [
                item for item in context.database.list("workflows", workspace_id=context.workspace_id)
                if item.get("status") == "published"
            ]}, events
        if name == "workflow_status":
            run = _named_record(context, "workflow_runs", str(args.get("run_id") or ""))
            history = [item for item in context.database.list("workflow_events", workspace_id=context.workspace_id) if item.get("run_id") == run["id"]]
            return {"run": run, "events": history}, events
        from .workflows import start_workflow

        workflow_ref = str(args.get("workflow_id") or args.get("name") or "")
        if args.get("workflow_version_id"):
            version = _named_record(context, "workflow_versions", str(args["workflow_version_id"]))
            workflow = _named_record(context, "workflows", str(version["workflow_id"]))
            workflow = {**workflow, "definition": version["definition"], "version": version["version"], "current_version_id": version["id"]}
        else:
            workflow = _named_record(context, "workflows", workflow_ref)
        if workflow.get("status") != "published":
            raise ValueError("工作流尚未发布")
        run = start_workflow(
            {**workflow, "definition": workflow.get("published_definition") or workflow["definition"]},
            args.get("inputs") or {}, actor_id=context.actor_id,
        )
        return {"run": run}, events
    if name == "read_tool_result":
        item = context.database.get("tool_results", str(args.get("artifact_id") or ""))
        if not item or item.get("workspace_id") != context.workspace_id or item.get("session_id") != context.session_id:
            raise ValueError("工具结果 Artifact 不存在或不属于当前会话")
        content = str(item.get("content") or "")
        query = str(args.get("query") or "").strip().lower()
        limit = max(1, min(int(args.get("limit", 4000)), 4000))
        if query:
            matches = []
            start = 0
            while len(matches) < 20:
                index = content.lower().find(query, start)
                if index < 0:
                    break
                left, right = max(0, index - 160), min(len(content), index + len(query) + 320)
                matches.append({"offset": index, "text": content[left:right]})
                start = index + max(1, len(query))
            return {"artifact_id": item["id"], "matches": matches, "total_chars": len(content)}, events
        offset = max(0, int(args.get("offset", 0)))
        return {
            "artifact_id": item["id"], "content": content[offset:offset + limit],
            "offset": offset, "next_offset": offset + limit if offset + limit < len(content) else None,
            "total_chars": len(content),
        }, events
    if name == "plan_complete":
        return {"status": "completed", "summary": str(args.get("summary") or ""), "steps": args.get("steps") or []}, events
    if name in context.mcp_names:
        server_id, raw_name = context.mcp_names[name]
        server = context.database.get("mcp_servers", server_id)
        if not server or server.get("workspace_id", "default") != context.workspace_id:
            raise ValueError("MCP 服务不存在或不属于当前工作空间")
        if not server.get("enabled", True):
            raise PermissionError("MCP 服务已禁用")
        from .mcp import get_mcp_manager

        return get_mcp_manager().call_tool(server, raw_name, args), events
    raise ValueError(f"未知 Agent 工具：{name}")


def model_text(value: dict, max_chars: int = 24_000) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[: max_chars // 2] + "\n…[工具结果过长，已截断]…\n" + text[-max_chars // 2 :]
