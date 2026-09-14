from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..core.database import Database
from .analytics import ANALYSIS_METHODS, clean_frame, profile as profile_frame, run_analysis_with_frames
from .authorization import require_result_access, require_sources_access
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
from .security import safe_http_request
from .semantic import execute_metric_query, visible_metrics
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
        "list_semantic_metrics",
        "List approved governed business metrics. Prefer these metrics over writing raw SQL for official KPIs.",
    ),
    _function(
        "query_metric",
        "Query one approved business metric through the deterministic semantic SQL compiler.",
        {
            "metric": {"type": "string"},
            "group_by": {
                "type": "array",
                "items": {"oneOf": [{"type": "string"}, {"type": "object"}]},
            },
            "filters": {"type": "array", "items": {"type": "object"}},
            "time_range": {"type": "object"},
            "order_by": {"type": "array", "items": {"type": "object"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
        },
        ["metric"],
    ),
    _function(
        "query_data",
        (
            "Execute one read-only SQL query over the selected sources and return a bounded result table. "
            "Call get_schema first, then use each table's exact query_name without a source-id/source-name prefix. "
            "For uploaded files use DuckDB SQL, double-quote identifiers containing spaces or punctuation, and never use backticks. "
            "In UNION queries every SELECT branch must include its own FROM clause; wrap the UNION in a CTE or subquery "
            "before ordering by a CASE expression. For medians use MEDIAN(column); QUANTILE_CONT requires an explicit "
            "quantile argument in DuckDB. "
            "When the run contains a database plus other sources, pass the one source_id needed for this query in source_ids; "
            "query sources separately because cross-source federation is intentionally disabled."
        ),
        {
            "sql": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
            "source_ids": {"type": "array", "items": {"type": "string"}},
        },
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
        (
            "Optionally rank the three best chart types from the visualization intent and available columns. "
            "The result already contains the supported type ids; call this at most once for an intended chart, "
            "then call generate_chart. Skip it when the chart type is already clear."
        ),
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
    _function("ask_user", "Ask the user for missing information; the question is surfaced as a structured event.", {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6}, "choices": {"type": "array", "items": {"type": "string"}}, "multi_select": {"type": "boolean"}}, ["question"]),
    _function("browse_webpage", "Read bounded text from an explicitly provided public HTTP(S) page.", {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
    _function("workspace_glob", "Page through safe workspace file metadata.", {"pattern": {"type": "string"}, "path": {"type": "string"}, "max_results": {"type": "integer"}, "cursor": {"type": "integer"}}, ["pattern"]),
    _function("workspace_grep", "Regex-search bounded UTF-8 workspace text files.", {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "max_results": {"type": "integer"}}, ["pattern"]),
    _function("workspace_read_file", "Read a bounded workspace text, document, PDF, or spreadsheet file.", {"file_path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}, "sheet_name": {"type": "string"}}, ["file_path"]),
    _function("structured_output", "Validate and return machine-readable output.", {"output": {}, "required_fields": {"type": "array", "items": {"type": "string"}}}, ["output"]),
    _function(
        "load_analysis_skill",
        (
            "Load a named analysis Skill SOP. Built-in ids are executive-summary, quality-audit, "
            "and trend-diagnosis; common underscore aliases are also accepted."
        ),
        {"name": {"type": "string"}}, ["name"],
    ),
    _function("task_create", "Create a persistent workspace task.", {"title": {"type": "string"}, "description": {"type": "string"}, "assignee": {"type": "string"}, "blocks": {"type": "array", "items": {"type": "string"}}, "blocked_by": {"type": "array", "items": {"type": "string"}}}, ["title"]),
    _function("task_get", "Get one workspace task.", {"task_id": {"type": "string"}}, ["task_id"]),
    _function("task_list", "List workspace tasks.", {"status": {"type": "string"}, "assignee": {"type": "string"}}),
    _function("task_update", "Update a task and its dependencies.", {"task_id": {"type": "string"}, "status": {"type": "string"}, "assignee": {"type": "string"}, "description": {"type": "string"}, "add_blocks": {"type": "array", "items": {"type": "string"}}, "add_blocked_by": {"type": "array", "items": {"type": "string"}}}, ["task_id"]),
    _function("read_tool_result", "Read or search a recoverable oversized tool result.", {"artifact_id": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}, "query": {"type": "string"}}, ["artifact_id"]),
    _function("plan_complete", "Return a completed coordinator plan.", {"summary": {"type": "string"}, "steps": {"type": "array", "items": {"type": "object"}}}, ["summary", "steps"]),
]

DEFAULT_EXTRA_TOOL_NAMES = frozenset({
    "workspace_status", "get_table_detail", "create_analysis_table", "clean_data",
    "propose_excel_export", "propose_report_outline", "propose_ppt_outline",
    "generate_ppt", "set_ppt_color_scheme",
    "ask_user", "browse_webpage", "workspace_glob", "workspace_grep", "workspace_read_file",
    "structured_output", "load_analysis_skill", "task_get", "task_list",
    "read_tool_result", "plan_complete",
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
    tool_result_ids: list[str] = field(default_factory=list)
    knowledge_document_ids: list[str] | None = None
    semantic_metric_ids: list[str] | None = None
    actor_id: str = ""

    def sources(self) -> list[dict]:
        return require_sources_access(
            self.database, self.source_ids, workspace_id=self.workspace_id,
            actor_id=self.actor_id or "local-default", action="analyze",
        )


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
            if not table.get("columns"):
                continue
            alias = table["name"]
            if table_counts.get(alias, 0) > 1 and source_index > 1:
                alias = re.sub(r"[^\w\u4e00-\u9fff]+", "_", f"{source['name']}_{alias}").strip("_")[:80]
            tables.append({**table, "query_name": alias})
        combined.append({"source_id": source["id"], "source_name": source.get("name"), "tables": tables})
    semantic_metrics = [
        item for item in visible_metrics(
            context.database, context.workspace_id, context.actor_id or "local-default",
        )
        if item.get("status") == "approved" and item.get("source_id") in context.source_ids
    ]
    file_sources = any(source.get("kind") != "database" for source in sources)
    return {
        "sources": combined,
        "semantic_metrics": semantic_metrics,
        "query_instructions": {
            "dialect": "duckdb" if file_sources else str(sources[0].get("driver") or "native_sql"),
            "table_reference": "只使用 tables[].query_name，不要添加 source_id 或数据源名称前缀",
            "identifier_quote": '上传文件使用双引号（"），不要使用反引号（`）',
        },
    }


def _frame(context: AgentToolContext, args: dict):
    result_id = str(args.get("result_id") or context.latest_result_id or "")
    if result_id:
        require_result_access(
            context.database, context.database.get("query_results", result_id),
            workspace_id=context.workspace_id, actor_id=context.actor_id or "local-default",
        )
        return load_result_frame(result_id), result_id
    source_id = str(args.get("source_id") or (context.source_ids[0] if context.source_ids else ""))
    source = require_sources_access(
        context.database, [source_id], workspace_id=context.workspace_id,
        actor_id=context.actor_id or "local-default", action="analyze",
    )[0]
    return source_table(source, args.get("table") or args.get("table_name"))[1], ""


def _resolve_frame_columns(frame: pd.DataFrame, requested: list[Any]) -> list[str]:
    """Resolve model-supplied column names without treating SQL quotes as data."""
    actual = [str(value) for value in frame.columns]
    normalized: dict[str, str] = {}
    for column in actual:
        key = column.strip()
        normalized.setdefault(key, column)

    resolved: list[str] = []
    missing: list[str] = []
    for value in requested:
        raw = str(value).strip()
        candidate = raw
        while len(candidate) >= 2 and (candidate[0], candidate[-1]) in {
            ('"', '"'), ("'", "'"), ('`', '`'), ('[', ']'),
        }:
            candidate = candidate[1:-1].strip()
        match = raw if raw in actual else normalized.get(candidate)
        if match is None:
            missing.append(raw)
        else:
            resolved.append(match)
    if missing:
        raise ValueError(f"分析字段不存在：{', '.join(missing)}")
    return resolved


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
    if name == "list_semantic_metrics":
        items = [
            item for item in visible_metrics(
                context.database, context.workspace_id, context.actor_id or "local-default",
            )
            if item.get("status") == "approved" and item.get("source_id") in context.source_ids
        ]
        if context.semantic_metric_ids is not None:
            allowed = {str(value) for value in context.semantic_metric_ids}
            items = [item for item in items if str(item.get("id")) in allowed]
        return {"items": items}, events
    if name == "query_metric":
        if context.semantic_metric_ids is not None:
            requested = str(args.get("metric") or "").split("@", 1)[0]
            allowed = {str(value) for value in context.semantic_metric_ids}
            visible = [
                item for item in visible_metrics(
                    context.database, context.workspace_id, context.actor_id or "local-default",
                ) if str(item.get("id")) in allowed
            ]
            if not any(requested in {str(item.get("id")), str(item.get("name"))} for item in visible):
                raise PermissionError("指标未在当前业务数据空间发布")
        output = execute_metric_query(
            context.database, args, context.workspace_id, context.actor_id or "local-default",
            allowed_source_ids=context.source_ids,
        )
        result = output["result"]
        context.latest_result_id = result["id"]
        public = _public_record(result)
        events.extend([
            ("plan", {"semantic_plan": output["plan"], "sql": result["sql"], "assumptions": []}),
            ("table", public),
        ])
        return {"plan": output["plan"], "result": public}, events
    if name == "query_data":
        requested_source_ids = [str(value) for value in args.get("source_ids") or context.source_ids]
        if not requested_source_ids:
            raise ValueError("请选择数据源")
        outside_scope = set(requested_source_ids) - set(context.source_ids)
        if outside_scope:
            raise PermissionError("查询请求包含本次分析范围外的数据源")
        result = execute_query(
            requested_source_ids, str(args.get("sql") or ""), context.workspace_id,
            int(args.get("limit", 1000)), actor_id=context.actor_id or "local-default",
        )
        context.latest_result_id = result["id"]
        public = _public_record(result)
        events.extend([("plan", {"sql": result["sql"], "assumptions": []}), ("table", public)])
        return public, events
    if name == "profile_data":
        frame, result_id = _frame(context, args)
        columns = args.get("columns")
        if columns:
            frame = frame[_resolve_frame_columns(frame, columns)]
        from ..data_cleaning import profile as cleaning_profile

        markdown, _plotly_charts = cleaning_profile(frame, None)
        structured = profile_frame(frame)
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
                context.source_ids, str(args["sql"]), context.workspace_id,
                int(args.get("limit", 5000)), actor_id=context.actor_id or "local-default",
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
                source_ids=context.source_ids, actor_id=context.actor_id or "local-default",
            )
            if context.analysis_source_id in context.source_ids:
                context.source_ids.remove(context.analysis_source_id)
            context.analysis_source_id = derived["id"]
            context.source_ids.append(derived["id"])
            for table in derived.get("tables", []):
                safe_table = str(table["name"]).replace('"', '""')
                query = execute_query(
                    [derived["id"]], f'SELECT * FROM "{safe_table}"',  # noqa: S608
                    context.workspace_id, 5000, actor_id=context.actor_id or "local-default",
                )
                result_ids[table["name"]] = query["id"]
            if result_ids:
                context.latest_result_id = next(iter(result_ids.values()))
        record = context.database.put(
            "analysis_runs",
            {
                "id": context.database.new_id("ana"), "workspace_id": context.workspace_id,
                "actor_id": context.actor_id or "local-default",
                "source_ids": list(context.source_ids),
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
            # Do not return the entire chart catalog here. It is available from
            # the dedicated chart-catalog API and used to add tens of thousands
            # of redundant characters to every subsequent model request.
            "candidates": candidates,
        }, events
    if name == "generate_chart":
        if args.get("sql") and not args.get("result_id"):
            query = execute_query(
                context.source_ids, str(args["sql"]), context.workspace_id,
                int(args.get("limit", 5000)), actor_id=context.actor_id or "local-default",
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
        payload.setdefault("source_ids", context.source_ids)
        artifact = (
            export_data(payload, context.workspace_id, context.actor_id or "local-default")
            if name == "export_excel"
            else export_report(payload, context.workspace_id, context.actor_id or "local-default")
        )
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
        source = require_sources_access(
            context.database, [source_id], workspace_id=context.workspace_id,
            actor_id=context.actor_id or "local-default", action="read",
        )[0]
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
        query = execute_query(
            context.source_ids, str(args.get("sql") or ""), context.workspace_id, 5000,
            actor_id=context.actor_id or "local-default",
        )
        frame = load_result_frame(query["id"])
        derived = register_derived_tables(
            {str(args.get("table_name") or "analysis_data"): frame},
            context.workspace_id, name=str(args.get("table_name") or "分析表"),
            source_ids=context.source_ids, actor_id=context.actor_id or "local-default",
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
                actor_id=context.actor_id or "local-default",
            )
            for source_id in result["archived_sources"]:
                if source_id in context.source_ids:
                    context.source_ids.remove(source_id)
            return result, events
        archived = []
        for source_id in args.get("source_ids") or []:
            source = require_sources_access(
                context.database, [str(source_id)], workspace_id=context.workspace_id,
                actor_id=context.actor_id or "local-default", action="delete",
            )[0]
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
            source_ids=context.source_ids, actor_id=context.actor_id or "local-default",
        )
        context.source_ids.append(derived["id"])
        table_name = str(args.get("output_table") or "data")
        safe_table = table_name.replace('"', '""')
        query = execute_query(  # The table identifier is escaped immediately above.
            [derived["id"]], f'SELECT * FROM "{safe_table}"',  # noqa: S608
            context.workspace_id, 5000, actor_id=context.actor_id or "local-default",
        )
        context.latest_result_id = query["id"]
        return {"source": _public_record(derived), "result_id": query["id"], "operations": operation_log}, events
    if name in {"propose_excel_export", "propose_report_outline", "propose_ppt_outline"}:
        event_type = {
            "propose_excel_export": "excel_outline", "propose_report_outline": "report_outline",
            "propose_ppt_outline": "ppt_outline",
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
        payload.setdefault("source_ids", context.source_ids)
        artifact = export_report(payload, context.workspace_id, context.actor_id or "local-default")
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

        requested_name = str(args.get("name") or "").strip()
        aliases = {
            "data_quality": "quality-audit",
            "quality_audit": "quality-audit",
            "executive_summary": "executive-summary",
            "trend_diagnosis": "trend-diagnosis",
        }
        skill = get_skill(aliases.get(requested_name, requested_name), context.workspace_id)
        if not skill:
            raise ValueError(
                "Skill 不存在；内置 Skill 为 executive-summary、quality-audit、trend-diagnosis"
            )
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
