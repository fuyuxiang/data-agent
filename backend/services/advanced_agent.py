from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp as sql_exp
from flask import Flask, current_app

from ..agent.contracts import ToolSpec
from ..agent.loop import AgentLoop
from ..agent.model import build_model_adapter
from ..agent.store import RunStore
from ..agent.tools import ToolExecutor, ToolRegistry
from ..core.database import Database, utcnow
from ..skills.models import FORMAL_AGENT_TOOLS
from .agent_tools import AgentToolContext, execute_tool, tool_schemas
from .authorization import actor_role, require_sources_access
from .data_policy import policy_fingerprint
from .data_plane.contracts import BoundedTransferPolicy, DatasetRef, DatasetRefStore
from .data_plane.factory import livy_adapter, sandbox_client, trino_adapter
from .datasets import frame_records
from .jobs import register_job_handler
from .knowledge import strip_reasoning
from .models import resolve_provider
from .results.manifests import ResultService
from .sql_security import validate_read_only_sql
from .usage import ensure_quota, record_usage
from .validation.engine import Rule, ValidationEngine, outcome


def _source_authorized(database: Database, run: dict[str, Any]) -> bool:
    try:
        sources = require_sources_access(
            database, run["source_scope"], workspace_id=run["workspace_id"],
            actor_id=run["actor_id"], action="analyze",
        )
        current = policy_fingerprint(
            sources, actor_id=run["actor_id"],
            role=actor_role(database, run["workspace_id"], run["actor_id"]),
        )
        recorded = run.get("policy_version")
        if recorded != current and (recorded != "agent-policy-v1" or any(
            source.get("row_policy") or source.get("column_policies") for source in sources
        )):
            return False
    except (FileNotFoundError, PermissionError):
        return False
    return True


def _dataset_ref(database: Database, run: dict[str, Any], result: dict[str, Any]) -> DatasetRef:
    path = Path(str(result.get("path") or ""))
    encoded_bytes = path.stat().st_size if path.is_file() else None
    complete = str(result.get("completeness") or "unknown") == "complete"
    return DatasetRef(
        ref_id=database.new_id("dref"), kind="bounded_file",
        source_refs=tuple(str(value) for value in result.get("source_ids") or run["source_scope"]),
        engine_id="bounded-query-result", location={"query_result_id": result["id"]},
        snapshot_set={}, source_time=None, schema_ref=None, grain=None,
        query_id=result["id"],
        query_hash=hashlib.sha256(str(result.get("sql") or "").encode("utf-8")).hexdigest(),
        contract_version=int(run["contract_version"]), policy_version=run["policy_version"],
        computation_state="complete", result_completeness="complete" if complete else "partial",
        accuracy=str(result.get("accuracy") or "exact"),
        requested_scope={"sql": result.get("sql"), "user_top_n": result.get("user_top_n")},
        actual_scope={"row_count": result.get("rows"), "column_count": len(result.get("columns") or [])},
        sample_metadata={}, row_count=result.get("rows"), encoded_bytes=encoded_bytes,
        preview_ref=result["id"], provenance_ref=f"query:{result['id']}", retention_until=None,
        owner_id=run["actor_id"], acl={"workspace_id": run["workspace_id"], "actor_ids": [run["actor_id"]]},
    )


def _result_policy_fingerprint(database: Database, run: dict[str, Any], source_ids: list[str]) -> str:
    sources = require_sources_access(
        database, source_ids, workspace_id=run["workspace_id"], actor_id=run["actor_id"], action="read",
    )
    return policy_fingerprint(
        sources, actor_id=run["actor_id"], role=actor_role(database, run["workspace_id"], run["actor_id"]),
    )


def _validate_result(database: Database, run: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    ref_id = str(args.get("dataset_ref_id") or "")
    ref = DatasetRefStore(database).get(ref_id, workspace_id=run["workspace_id"]) if ref_id else None
    result_id = str(args.get("result_id") or ((ref.location or {}).get("query_result_id") if ref else "") or "")
    result = database.get("query_results", result_id, workspace_id=run["workspace_id"]) if result_id else None
    if not ref and not result:
        raise ValueError("待验证结果不存在或不属于当前工作空间")
    subject = ref_id or result_id
    actual_sources = set(ref.source_refs if ref else result.get("source_ids") or [])
    expected_sources = set(run["source_scope"])
    rules = [
        Rule("execution_complete", "1", "execution", "blocking", 3, lambda ctx: outcome(
            "PASS" if ctx["complete"] else "FAIL", "计算完整" if ctx["complete"] else "结果被系统截断或状态未知",
        )),
        Rule("source_scope", "1", "authorization", "blocking", 3, lambda ctx: outcome(
            "PASS" if ctx["sources"].issubset(ctx["expected"]) else "FAIL",
            "来源在已确认范围内" if ctx["sources"].issubset(ctx["expected"]) else "结果包含未授权来源",
        )),
        Rule("schema_present", "1", "data", "blocking", 2, lambda ctx: outcome(
            "PASS" if ctx["columns"] else "UNKNOWN", "结果包含字段定义" if ctx["columns"] else "结果字段未知",
        )),
        Rule("row_count", "1", "data", "advisory", 1, lambda ctx: outcome(
            "PASS" if ctx["rows"] is not None else "UNKNOWN", "返回行数已记录" if ctx["rows"] is not None else "返回行数未知",
        )),
    ]
    complete = (
        ref.result_completeness == "complete" and ref.computation_state == "complete"
        if ref else result.get("completeness") == "complete"
    )
    summary = ValidationEngine(database, rules).evaluate(
        run_id=run["id"], workspace_id=run["workspace_id"], subject_ref=subject,
        context={
            "complete": complete, "sources": actual_sources, "expected": expected_sources,
            "columns": (ref.actual_scope.get("column_count") if ref else len(result.get("columns") or [])),
            "rows": ref.row_count if ref else result.get("rows"),
        },
    )
    return {
        "ok": summary["status"] == "PASS", "status": "SUCCEEDED",
        "validation_status": summary["status"], "completeness": "complete" if complete else "partial",
        "dataset_ref_id": ref_id or None, "result_id": result_id or None,
        "output_refs": [subject], "validation": summary,
    }


def _warehouse_engine(database: Database, run: dict[str, Any], engine_id: str):
    selected = [
        database.get("sources", source_id, workspace_id=run["workspace_id"])
        for source_id in run["source_scope"]
    ]
    source = next((
        item for item in selected if item and item.get("kind") == "warehouse"
        and (item.get("engine_id") == engine_id or item.get("id") == engine_id)
    ), None)
    if not source:
        raise PermissionError("任务未选择该数仓引擎的来源范围")
    return source, trino_adapter(database, run["workspace_id"], str(source["engine_id"]))


def _scoped_warehouse_sql(sql: str, *, catalog: str, schema: str) -> str:
    """Bind every base relation to the selected warehouse namespace."""
    statement = sqlglot.parse_one(validate_read_only_sql(sql, "trino"), read="trino")
    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(sql_exp.CTE)}
    for table in statement.find_all(sql_exp.Table):
        if not table.name:
            raise PermissionError("数仓查询包含无法确认范围的数据表")
        if table.name.lower() in cte_names and not table.db and not table.catalog:
            continue
        if table.catalog and table.catalog.lower() != catalog.lower():
            raise PermissionError("数仓查询超出已选目录")
        if table.db and table.db.lower() != schema.lower():
            raise PermissionError("数仓查询超出已选模式")
        table.set("catalog", sql_exp.to_identifier(catalog))
        table.set("db", sql_exp.to_identifier(schema))
    return statement.sql(dialect="trino")


def materialize_trino_preview(
    database: Database, run: dict[str, Any], query: dict[str, Any], *, source_ids: list[str],
) -> tuple[dict[str, Any] | None, DatasetRef | None]:
    if query.get("has_next") or query.get("status") != "finished":
        return None, None
    rows = list(query.get("preview") or [])
    columns = query.get("columns") or []
    names = [str(item.get("name") if isinstance(item, dict) else item) for item in columns]
    output_positions = (query.get("stats") or {}).get("outputPositions")
    if output_positions is not None and int(output_positions) != len(rows):
        return None, None
    if len(rows) > 300 or len(names) > 500:
        return None, None
    result_id = database.new_id("qry")
    path = current_app.config["SETTINGS"].export_dir / f"{result_id}.csv"
    frame = pd.DataFrame(rows, columns=names or None)
    frame.to_csv(path, index=False)
    result = database.put("query_results", {
        "id": result_id, "workspace_id": run["workspace_id"], "source_ids": source_ids,
        "actor_id": run["actor_id"],
        "policy_fingerprint": _result_policy_fingerprint(database, run, source_ids),
        "sql": "", "rows": len(frame), "returned_rows": len(frame), "total_rows": len(frame),
        "completeness": "complete", "accuracy": "exact", "columns": [str(value) for value in frame.columns],
        "data": frame.where(pd.notna(frame), None).to_dict(orient="records"), "path": str(path),
        "warehouse_query_id": query["query_id"],
    }, workspace_id=run["workspace_id"])
    ref = _dataset_ref(database, run, result)
    DatasetRefStore(database).put(ref, workspace_id=run["workspace_id"], run_id=run["id"])
    return result, ref


def _warehouse_tool(database: Database, run: dict[str, Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
    source, adapter = _warehouse_engine(database, run, str(args.get("engine_id") or ""))
    allowed_catalog = str(source.get("catalog") or "")
    allowed_schema = str(source.get("schema") or "")
    if not allowed_catalog or not allowed_schema:
        raise PermissionError("数仓来源未限定目录与模式")
    if name == "warehouse_catalog":
        if args.get("catalog") and str(args["catalog"]).lower() != allowed_catalog.lower():
            raise PermissionError("数仓目录超出已选数据源")
        if args.get("schema") and str(args["schema"]).lower() != allowed_schema.lower():
            raise PermissionError("数仓模式超出已选数据源")
        return adapter.discover(
            catalog=allowed_catalog, schema=allowed_schema,
            limit=int(args.get("limit") or 100), cursor=str(args.get("cursor") or ""),
        )
    scoped_sql = _scoped_warehouse_sql(
        str(args.get("sql") or ""), catalog=allowed_catalog, schema=allowed_schema,
    )
    estimate = adapter.estimate(scoped_sql)
    if name == "warehouse_explain":
        return estimate
    engine = database.get("warehouse_engines", source["engine_id"], workspace_id=run["workspace_id"]) or {}
    estimated_bytes = estimate.get("estimated_scan_bytes")
    budget = run.get("budget") or {}
    if estimated_bytes is not None and budget.get("warehouse_scan_bytes") is not None:
        if int(estimated_bytes) > int(budget["warehouse_scan_bytes"]):
            raise RuntimeError("预估扫描字节超过任务预算")
    if estimated_bytes is None and not engine.get("native_limits_confirmed"):
        raise RuntimeError("扫描成本未知且未确认 Trino 资源组原生硬限制，拒绝提交")
    submitted = adapter.submit(
        scoped_sql, run_id=run["id"], action_id=database.new_id("external"),
        result_mode=str(args.get("result_mode") or "preview"),
        source_refs=[source["id"]],
    )
    if submitted.get("status") == "SUCCEEDED":
        record = adapter._query(submitted["query_id"])
        query = adapter.public_query(record)
        remote_ref = adapter.result_ref(
            submitted["query_id"], owner_id=run["actor_id"], contract_version=run["contract_version"],
            policy_version=run["policy_version"],
        )
        result, bounded_ref = (None, None)
        if record.get("result_mode") != "materialize":
            result, bounded_ref = materialize_trino_preview(database, run, query, source_ids=[source["id"]])
        return {
            **submitted, "dataset_ref_id": (bounded_ref or remote_ref).ref_id,
            "output_refs": [remote_ref.ref_id, *([bounded_ref.ref_id] if bounded_ref else [])],
            "result_id": result.get("id") if result else None,
            "completeness": "complete", "accuracy": "exact",
        }
    return {**submitted, "job_id": submitted["query_id"], "completeness": "unknown"}


def _spark_tool(database: Database, run: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    engine_id = str(args.get("engine_id") or "")
    engine = database.get("warehouse_engines", engine_id, workspace_id=run["workspace_id"])
    if not engine or engine.get("type") != "livy" or not engine.get("enabled", True):
        raise FileNotFoundError("Livy 引擎不存在或已禁用")
    input_ref_ids = [str(value) for value in args.get("input_ref_ids") or []]
    if not input_ref_ids:
        raise ValueError("远程 Spark 作业需要已登记的 DatasetRef")
    input_refs = []
    store = DatasetRefStore(database)
    for ref_id in input_ref_ids:
        ref = store.get(ref_id, workspace_id=run["workspace_id"])
        if not ref or ref.owner_id != run["actor_id"]:
            raise PermissionError("DatasetRef 不存在或不属于当前用户")
        if not set(ref.source_refs).issubset(set(run["source_scope"])):
            raise PermissionError("DatasetRef 超出已确认来源范围")
        uri = str(ref.location.get("uri") or ref.location.get("output_uri") or "")
        if not uri:
            raise ValueError("DatasetRef 没有 Spark 可读的受授权远程 URI")
        input_refs.append({"ref_id": ref.ref_id, "uri": uri, "snapshot_set": ref.snapshot_set})
    submitted = livy_adapter(database, run["workspace_id"], engine_id).submit({
        "method": str(args.get("method") or ""), "input_refs": input_refs,
        "parameters": dict(args.get("parameters") or {}),
        "contract_version": run["contract_version"], "policy_version": run["policy_version"],
    }, run_id=run["id"], action_id=database.new_id("external"))
    return {**submitted, "completeness": "unknown"}


def _sandbox_tool(database: Database, run: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Run reviewed methods or generated Python only inside the bounded container."""
    ref_id = str(args.get("dataset_ref_id") or "")
    ref = DatasetRefStore(database).get(ref_id, workspace_id=run["workspace_id"])
    if not ref or ref.kind != "bounded_file":
        raise ValueError("run_analysis 需要当前任务的有界文件 DatasetRef")
    if ref.owner_id != run["actor_id"] or not set(ref.source_refs).issubset(set(run["source_scope"])):
        raise PermissionError("DatasetRef 不属于当前用户或超出已确认来源范围")
    if ref.computation_state != "complete" or ref.result_completeness != "complete":
        raise ValueError("仅完整计算且未截断的 DatasetRef 可进入本地隔离分析")
    usage = run.get("usage") or {}
    BoundedTransferPolicy().approve(ref, run_egress_bytes=int(usage.get("result_bytes") or 0))
    result_id = str(ref.location.get("query_result_id") or "")
    source_result = database.get("query_results", result_id, workspace_id=run["workspace_id"])
    if not source_result:
        raise FileNotFoundError("DatasetRef 指向的有界查询结果不存在")
    source_path = Path(str(source_result.get("path") or "")).resolve()
    settings = current_app.config["SETTINGS"]
    export_root = settings.export_dir.resolve()
    if export_root not in source_path.parents or not source_path.is_file() or source_path.is_symlink():
        raise PermissionError("DatasetRef 输入不在受管的有界结果目录")
    method = str(args.get("method") or "").strip()
    code = str(args.get("code") or "")
    if bool(method) == bool(code):
        raise ValueError("run_analysis 必须且只能提供 method 或 code")

    input_root = settings.workspace_dir / "sandbox-inputs"
    output_root = settings.export_dir / "sandbox"
    input_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    input_dir = Path(tempfile.mkdtemp(prefix="analysis-", dir=input_root))
    action_ref = database.new_id("sandbox")
    input_path = input_dir / f"input{source_path.suffix.lower()}"
    shutil.copy2(source_path, input_path)
    runner = sandbox_client()
    sandbox_image = runner.expected_image
    try:
        sandbox_result = runner.execute(
            {
                "input": input_path.name, "method": method or None, "code": code or None,
                "parameters": dict(args.get("params") or {}),
            },
            input_dir=input_dir, run_id=f"{run['id']}-{action_ref}",
            should_cancel=lambda: (RunStore(database).get_run(run["id"]) or {}).get("execution_status")
            in {"cancelling", "cancelled"},
        )
    finally:
        shutil.rmtree(input_dir, ignore_errors=True)

    files = sandbox_result.get("files") or []
    output = next((item for item in files if item.get("path") == "result.parquet"), None)
    if not output:
        raise RuntimeError("sandbox 未返回受管的 Parquet 结果")
    output_path = (Path(str(sandbox_result["output_dir"])) / "result.parquet").resolve()
    if output_root.resolve() not in output_path.parents or not output_path.is_file() or output_path.is_symlink():
        raise PermissionError("sandbox 结果路径越界")
    frame = pd.read_parquet(output_path)
    if len(frame) > settings.max_analysis_rows or len(frame.columns) > 500:
        raise ValueError("sandbox 结果超过本地分析上限")
    encoded_bytes = output_path.stat().st_size
    result_budget = (run.get("budget") or {}).get("result_bytes")
    remaining = None if result_budget is None else float(result_budget) - float(
        (run.get("usage") or {}).get("result_bytes") or 0,
    )
    if remaining is not None and encoded_bytes > remaining:
        raise RuntimeError("sandbox 结果超过任务剩余产物字节预算")
    derived_id = database.new_id("qry")
    derived_result = database.put("query_results", {
        "id": derived_id, "workspace_id": run["workspace_id"],
        "actor_id": run["actor_id"],
        "policy_fingerprint": _result_policy_fingerprint(database, run, list(ref.source_refs)),
        "source_ids": list(ref.source_refs), "sql": "", "rows": len(frame),
        "returned_rows": len(frame), "total_rows": len(frame), "completeness": "complete",
        "accuracy": "exact", "columns": [str(value) for value in frame.columns],
        "data": frame_records(frame, 300), "path": str(output_path),
        "sandbox": {
            "image": sandbox_image, "method": method or "generated_python",
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest() if code else None,
            "input_dataset_ref_id": ref.ref_id, "output_sha256": output["sha256"],
            "metrics": sandbox_result.get("metrics") or {},
        },
    }, workspace_id=run["workspace_id"])
    derived_ref = _dataset_ref(database, run, derived_result)
    DatasetRefStore(database).put(derived_ref, workspace_id=run["workspace_id"], run_id=run["id"])
    analysis = database.put("analysis_runs", {
        "id": database.new_id("ana"), "workspace_id": run["workspace_id"],
        "actor_id": run["actor_id"], "source_ids": list(ref.source_refs),
        "session_id": run["session_id"], "agent_run_id": run["id"],
        "method": method or "generated_python", "code": code or None,
        "inputs": {"dataset_ref_id": ref.ref_id, "params": dict(args.get("params") or {})},
        "result": sandbox_result.get("metrics") or {}, "status": "completed",
        "result_ids": {"result": derived_id}, "dataset_ref_id": derived_ref.ref_id,
    }, workspace_id=run["workspace_id"])
    return {
        "status": "SUCCEEDED", "analysis_id": analysis["id"], "result_id": derived_id,
        "dataset_ref_id": derived_ref.ref_id, "output_refs": [derived_ref.ref_id],
        "completeness": "complete", "accuracy": "exact", "actual_cost": encoded_bytes,
        "provenance_ref": f"sandbox:{analysis['id']}", "metrics": sandbox_result.get("metrics") or {},
    }


def _prepare_run_skills(
    database: Database,
    run: dict[str, Any],
    analysis_context: dict[str, Any],
    allowed_tools: list[str],
) -> list[dict[str, Any]]:
    """Resolve this run's skills and hand the loop a narrowed capability set."""
    from ..skills.context import build_skill_context
    from ..skills.executor import SkillExecutor
    from ..skills.registry import SkillRegistry

    requested = list(analysis_context.get("skill_ids") or [])
    if not requested and run.get("skill_id"):
        requested = [str(run["skill_id"])]
    registry = SkillRegistry(database, run["workspace_id"])
    definitions = registry.get_many(requested)
    if not definitions:
        return []
    context = build_skill_context(database, run)
    executions = SkillExecutor(context, runtime_tools=allowed_tools).prepare(definitions)
    database.patch("skill_resolutions", f"skr_{run['id']}", {
        "skill_ids": [item.skill.id for item in executions],
        "allowed_tools": sorted(SkillExecutor.combined_tools(executions)),
        "warnings": [
            {"skill_id": item.skill.id, "message": message}
            for item in executions for message in item.warnings
        ],
        "resolved_at": utcnow(),
    }, workspace_id=run["workspace_id"])
    return SkillExecutor.model_blocks(executions)


def _run_knowledge_ids(database: Database, run: dict[str, Any]) -> list[str]:
    selected = database.get("analysis_context", run["id"], workspace_id=run["workspace_id"]) or {}
    explicitly_selected = bool(selected.get("knowledge_selection_explicit"))
    lineage = {run["id"]}
    ancestor = run
    for _ in range(20):
        parent_id = ancestor.get("parent_run_id")
        if not parent_id or parent_id in lineage:
            break
        parent = RunStore(database).get_run(parent_id, workspace_id=run["workspace_id"])
        if not parent or parent.get("actor_id") != run["actor_id"]:
            break
        lineage.add(parent_id)
        ancestor = parent
    ids = {
        str(item["document_id"])
        for item in database.list("analysis_attachments", workspace_id=run["workspace_id"], limit=5000)
        if item.get("run_id") in lineage and item.get("owner_id") == run["actor_id"]
    }
    if explicitly_selected:
        ids.update(
            str(document_id) for document_id in selected.get("knowledge_document_ids") or []
            if (document := database.get("knowledge_documents", str(document_id), workspace_id=run["workspace_id"]))
            and document.get("enabled", True) and document.get("visibility") != "analysis_attachment"
        )
    else:
        ids.update(
            str(item["id"])
            for item in database.list("knowledge_documents", workspace_id=run["workspace_id"], limit=5000)
            if item.get("enabled", True) and item.get("visibility") != "analysis_attachment"
        )
    ids.update(
        str(item["id"])
        for item in database.list("knowledge_entries", workspace_id=run["workspace_id"], limit=5000)
        if item.get("enabled", True)
    )
    return sorted(ids)


def build_executor(database: Database, run: dict[str, Any]) -> ToolExecutor:
    analysis_context = database.get("analysis_context", run["id"], workspace_id=run["workspace_id"]) or {}
    agent_snapshot = analysis_context.get("agent_snapshot") or {}
    metric_ids = list(agent_snapshot.get("metric_ids") or [])
    context = AgentToolContext(
        database=database, workspace_id=run["workspace_id"], session_id=run["session_id"],
        source_ids=list(run["source_scope"]),
        knowledge_document_ids=_run_knowledge_ids(database, run),
        semantic_metric_ids=metric_ids or None,
        mcp_server_ids=list(agent_snapshot.get("mcp_server_ids") or []) if agent_snapshot else None,
        actor_id=run["actor_id"],
    )
    registry = ToolRegistry()
    approved_mcp = {
        (str(server["id"]), str(name))
        for server in database.list("mcp_servers", workspace_id=run["workspace_id"], limit=5000)
        if server.get("enabled", True) and server.get("status") == "connected"
        and (context.mcp_server_ids is None or str(server["id"]) in context.mcp_server_ids)
        for name in server.get("formal_read_tools") or []
    }
    for raw in tool_schemas(context):
        function = raw.get("function") or {}
        name = str(function.get("name") or "")
        mcp_identity = context.mcp_names.get(name)
        if (name not in FORMAL_AGENT_TOOLS and mcp_identity not in approved_mcp) or name in {"run_analysis", "validate_result"}:
            continue
        spec = ToolSpec(
            id=name, description=str(function.get("description") or name),
            input_schema=function.get("parameters") or {"type": "object", "properties": {}},
            mutability="external" if mcp_identity else "read",
            idempotency="unknown" if mcp_identity else "idempotent",
            timeout_seconds=120, cancellable=name in {"query_data", "run_analysis"},
        )

        def handler(arguments: dict[str, Any], tool_name: str = name):
            value, events = execute_tool(tool_name, arguments, context)
            if tool_name in context.mcp_names:
                encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
                if len(encoded) > 64 * 1024:
                    raise ValueError("MCP 工具结果超过正式分析上下文上限")
                value = {
                    "content": value, "completeness": "unknown", "external_unverified": True,
                    "provenance_ref": f"mcp:{context.mcp_names[tool_name][0]}:{context.mcp_names[tool_name][1]}",
                }
            if tool_name in {"query_data", "query_metric"}:
                result_id = (
                    value.get("id") if tool_name == "query_data"
                    else (value.get("result") or {}).get("id")
                )
                result = database.get(
                    "query_results", str(result_id or ""), workspace_id=run["workspace_id"],
                )
                if result:
                    ref = _dataset_ref(database, run, result)
                    DatasetRefStore(database).put(ref, workspace_id=run["workspace_id"], run_id=run["id"])
                    value = {
                        **value, "result_id": result["id"], "dataset_ref_id": ref.ref_id,
                        "completeness": ref.result_completeness, "accuracy": ref.accuracy,
                        "provenance_ref": ref.provenance_ref,
                    }
            elif tool_name in {"generate_chart", "profile_data", "run_analysis"}:
                value = {**value, "completeness": value.get("completeness") or "complete"}
            return value, events

        registry.register(spec, handler)
    registry.register(ToolSpec(
        id="run_analysis", description=(
            "对一个已完整物化且通过有界转移门禁的 DatasetRef 运行隔离 Python 分析。"
            "可选审核方法，或提供在容器内以 df 为输入并将 DataFrame 赋给 result 的代码。"
        ),
        input_schema={
            "type": "object", "properties": {
                "dataset_ref_id": {"type": "string"},
                "method": {"type": "string", "enum": [
                    "describe", "correlation", "grouped_summary", "decile", "ab_test",
                    "linear_regression", "kmeans", "anomaly", "trend_forecast",
                ]},
                "code": {"type": "string"}, "params": {"type": "object"},
            }, "required": ["dataset_ref_id"],
        }, mutability="read", timeout_seconds=120, cancellable=True,
        cost_kind="result_bytes",
    ), lambda arguments: _sandbox_tool_with_events(database, run, arguments))
    registry.register(ToolSpec(
        id="validate_result", description="对当前查询结果执行独立完整性、范围与结构验证；正式发布前必须调用。",
        input_schema={
            "type": "object", "properties": {
                "dataset_ref_id": {"type": "string"}, "result_id": {"type": "string"},
            },
        }, mutability="read",
    ), lambda arguments: _validate_result(database, run, arguments))
    registry.register(ToolSpec(
        id="update_plan", description="根据新证据增加、关闭或重排分析任务；计划是可变 DAG，不是固定流程。",
        input_schema={
            "type": "object", "properties": {
                "tasks": {"type": "array", "items": {"type": "object"}},
                "reason": {"type": "string"}, "expected_version": {"type": "integer"},
            }, "required": ["tasks", "reason", "expected_version"],
        }, mutability="control",
    ), lambda arguments: {
        "plan": RunStore(database).add_plan(
            run["id"], {"tasks": arguments["tasks"]}, reason=str(arguments["reason"]),
            expected_version=int(arguments["expected_version"]),
        ),
        "completeness": "complete",
    })
    registry.register(ToolSpec(
        id="warehouse_catalog", description="按页查看当前任务选中的 Trino catalog/schema/table，不遍历全仓。",
        input_schema={"type": "object", "properties": {
            "engine_id": {"type": "string"}, "catalog": {"type": "string"}, "schema": {"type": "string"},
            "limit": {"type": "integer"}, "cursor": {"type": "string"},
        }, "required": ["engine_id"]},
    ), lambda arguments: _warehouse_tool(database, run, "warehouse_catalog", arguments))
    registry.register(ToolSpec(
        id="warehouse_explain", description="使用 Trino EXPLAIN TYPE IO 预检查只读 SQL；不执行原查询。",
        input_schema={"type": "object", "properties": {
            "engine_id": {"type": "string"}, "sql": {"type": "string"},
        }, "required": ["engine_id", "sql"]},
    ), lambda arguments: _warehouse_tool(database, run, "warehouse_explain", arguments))
    registry.register(ToolSpec(
        id="warehouse_query", description=(
            "将只读 SQL 下推到已选 Trino 引擎。preview 仅适合不超过预览上限的小结果；"
            "大结果必须选择 materialize，由服务端写入受管 scratch 表后登记稳定 DatasetRef。"
        ),
        input_schema={"type": "object", "properties": {
            "engine_id": {"type": "string"}, "sql": {"type": "string"},
            "result_mode": {"type": "string", "enum": ["preview", "materialize"]},
        }, "required": ["engine_id", "sql"]}, timeout_seconds=120, cancellable=True,
    ), lambda arguments: _warehouse_tool(database, run, "warehouse_query", arguments))
    registry.register(ToolSpec(
        id="warehouse_spark_submit",
        description="将大型特征、异常或 ML 作业以受信任方法提交至 Livy/Spark，不接受模型生成的任意代码。",
        input_schema={"type": "object", "properties": {
            "engine_id": {"type": "string"},
            "method": {"type": "string", "enum": [
                "filter_project_aggregate", "window_features", "authorized_join",
                "grouped_trend_anomaly", "mllib_logistic_regression", "mllib_kmeans",
            ]},
            "input_ref_ids": {"type": "array", "items": {"type": "string"}},
            "parameters": {"type": "object"},
        }, "required": ["engine_id", "method", "input_ref_ids"]},
        timeout_seconds=120, cancellable=True, cost_kind="remote_compute_seconds",
    ), lambda arguments: _spark_tool(database, run, arguments))
    return ToolExecutor(RunStore(database), registry)


def _sandbox_tool_with_events(
    database: Database, run: dict[str, Any], arguments: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    value = _sandbox_tool(database, run, arguments)
    return value, [
        ("analysis", {
            "analysis_id": value["analysis_id"], "dataset_ref_id": value["dataset_ref_id"],
            "provenance_ref": value["provenance_ref"],
        }),
    ]


def available_formal_tools(database: Database, workspace_id: str, session_id: str, source_ids: list[str]) -> list[str]:
    context = AgentToolContext(database, workspace_id, session_id, list(source_ids))
    discovered = {
        str((item.get("function") or {}).get("name") or "")
        for item in tool_schemas(context)
    }
    selected = [database.get("sources", item, workspace_id=workspace_id) for item in source_ids]
    remote = {"warehouse_catalog", "warehouse_explain", "warehouse_query"} if any(
        item and item.get("kind") == "warehouse" for item in selected
    ) else set()
    if remote and any(
        item.get("type") == "livy" and item.get("enabled", True)
        for item in database.list("warehouse_engines", workspace_id=workspace_id, limit=5000)
    ):
        remote.add("warehouse_spark_submit")
    approved_mcp = {
        exposed for exposed, identity in context.mcp_names.items()
        if (server := database.get("mcp_servers", identity[0], workspace_id=workspace_id))
        and identity[1] in set(server.get("formal_read_tools") or [])
    }
    available = (discovered & FORMAL_AGENT_TOOLS) | {"validate_result", "update_plan"} | remote
    if not approved_mcp:
        available.discard("search_mcp_tools")
    return sorted(available | approved_mcp)


def _analysis_job_handler(app: Flask, spec: dict[str, Any], progress, cancel) -> dict:
    database: Database = app.extensions["meridian_db"]
    store = RunStore(database)
    run_id = str(spec.get("run_id") or "")
    run = store.get_run(run_id)
    if not run:
        raise FileNotFoundError("分析任务不存在")
    if run["execution_status"] in {"finished", "failed", "cancelled"}:
        return {"run_id": run_id, "status": run["execution_status"], "outcome": run["outcome"]}
    # Check the live ACL before selected-source metadata can enter model context.
    # Publication repeats this check to close the mid-run revocation race.
    if not _source_authorized(database, run):
        store.update_status(run_id, "failed", outcome="failed", stop_reason="source_authorization_revoked")
        store.append_event(run_id, "authorization.denied", {"stage": "before_model_context"})
        return {"run_id": run_id, "status": "failed", "stop_reason": "source_authorization_revoked"}
    try:
        quota = ensure_quota(database, run["workspace_id"])
    except PermissionError:
        store.update_status(run_id, "failed", outcome="failed", stop_reason="daily_model_budget_exceeded")
        store.append_event(run_id, "budget.exhausted", {"kind": "daily_model_tokens"})
        return {"run_id": run_id, "status": "failed", "stop_reason": "daily_model_budget_exceeded"}
    budget = dict(run.get("budget") or RunStore.default_budget())
    configured_limit = budget.get("model_tokens")
    configured_tokens = (
        int(configured_limit) if configured_limit is not None else int(quota["remaining"])
    )
    quota_limited = int(quota["remaining"]) < configured_tokens
    minimum_viable_tokens = min(configured_tokens, 50_000)
    if int(quota["remaining"]) < minimum_viable_tokens:
        store.update_status(
            run_id, "failed", outcome="failed", stop_reason="daily_model_budget_exceeded",
        )
        store.append_event(run_id, "budget.exhausted", {
            "kind": "daily_model_tokens", "stage": "before_analysis_run",
            "remaining": int(quota["remaining"]), "minimum_required": minimum_viable_tokens,
        })
        return {
            "run_id": run_id, "status": "failed",
            "stop_reason": "daily_model_budget_exceeded",
        }
    budget["model_tokens"] = min(
        configured_tokens, int(quota["remaining"]),
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE agent_runs SET budget=?,updated_at=? WHERE id=?",
            (json.dumps(budget, ensure_ascii=False), utcnow(), run_id),
        )
    run = store.get_run(run_id) or run
    starting_tokens = int((run.get("usage") or {}).get("model_tokens") or 0)
    provider, client = resolve_provider(run.get("provider_id"), run["workspace_id"])
    if not provider or not client:
        store.update_status(run_id, "failed", outcome="failed", stop_reason="model_not_configured")
        store.append_event(run_id, "model.unavailable", {"reason": "model_not_configured"})
        return {"run_id": run_id, "status": "failed", "stop_reason": "model_not_configured"}
    progress(5, "加载已确认任务契约")
    executor = build_executor(database, run)
    finalizer = ResultService(database, authorize=lambda current: _source_authorized(database, current))
    loop = AgentLoop(
        store=store, model=build_model_adapter(client, provider), tools=executor,
        finalizer=finalizer.finalize,
        context_window=int(provider.get("context_window") or 32_768),
        max_output_tokens=int(provider.get("max_output_tokens") or 4_096),
        model_budget_stop_reason=(
            "daily_model_budget_exceeded" if quota_limited else "model_budget_exceeded"
        ),
    )
    history = [
        {"role": item["role"], "content": item["content"]}
        for item in database.messages(run["session_id"], 500)
        if item["role"] in {"system", "user", "assistant"}
    ]
    progress(15, "Agent 已开始动态规划与执行")
    analysis_context = database.get("analysis_context", run["id"], workspace_id=run["workspace_id"]) or {}

    # Skills are resolved through the Skill runtime: the workspace registry,
    # permission-checked against the run's own source scope, then narrowed to the
    # tools this run may actually reach.
    governed_skills = _prepare_run_skills(
        database, run, analysis_context, list(run.get("allowed_tool_ids") or []),
    )
    agent_snapshot = analysis_context.get("agent_snapshot") or {}
    role = strip_reasoning(agent_snapshot.get("instruction"))
    if role:
        governed_skills.insert(0, {
            "id": f"agent:{agent_snapshot['id']}:{agent_snapshot['version']}",
            "source": "published_agent", "description": str(agent_snapshot.get("name") or "分析智能体"),
            "instruction": role,
        })
    result = loop.run(
        run_id, runner_id=f"analysis-job:{run_id}", history=history,
        skills=governed_skills,
        should_cancel=cancel.is_set,
    )
    completed_usage = (store.get_run(run_id) or run).get("usage") or {}
    model_delta = max(0, int(completed_usage.get("model_tokens") or 0) - starting_tokens)
    if model_delta:
        record_usage(
            database, run["workspace_id"],
            {"total_tokens": model_delta, "model": provider["model"]},
            operation="analysis_run",
        )
    if result.answer:
        database.add_message(
            run["session_id"], "assistant", result.answer,
            {"run_id": run_id, "outcome": result.outcome, "publication_id": result.publication_id},
        )
    progress(100, "分析已结束")
    return asdict(result)


register_job_handler("analysis_run", _analysis_job_handler)
