"""
NL2SQL 查询 API - 使用 LangGraph 新架构
"""
import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.database import get_db
from app.core.logging import get_logger
from app.models.models import Dataset, QueryHistory, User, UserWorkspace
from app.orchestrator_duckdb import get_engine
from app.orchestrator_graph import LangGraphOrchestrator, run_stream
from app.schemas.schemas import ExecuteSqlRequest, QueryRequest, QueryResponse
from app.services.guardrails import SQLGuardrail, SQLSecurityError

logger = get_logger(__name__)
router = APIRouter()

BACKEND_DIR = Path(__file__).resolve().parents[2]
UPLOAD_DIR = BACKEND_DIR / "data" / "uploads"


def _normalize_table_name(name: str) -> str:
    """标准化表名（与 DuckDB 加载逻辑保持一致）"""
    if not name:
        return ""
    table_name = name
    if table_name.endswith(".csv"):
        table_name = table_name[:-4]
    if "_" in table_name:
        parts = table_name.split("_", 1)
        if len(parts[0]) == 8 and parts[0].isdigit():
            table_name = parts[1]
    return table_name.replace("-", "_").replace(".", "_")


def _quote_ident(name: str) -> str:
    """DuckDB 标识符转义"""
    return '"' + (name or "").replace('"', '""') + '"'


def _scoped_table_name(dataset_id: Optional[int], table_name: str) -> str:
    """按数据集作用域生成 DuckDB 表名，避免跨数据集同名污染"""
    normalized = _normalize_table_name(table_name)
    if dataset_id is None:
        return normalized
    return f"ds{dataset_id}_{normalized}"


def _resolve_csv_file_path(file_path: Optional[str]) -> Optional[str]:
    """解析 CSV 文件路径，兼容历史绝对路径迁移到当前工程目录"""
    if not file_path:
        return None
    if os.path.exists(file_path):
        return file_path
    basename = os.path.basename(file_path)
    if not basename:
        return None
    candidate = UPLOAD_DIR / basename
    if candidate.exists():
        return str(candidate)
    return None


def _detect_csv_delimiter(file_path: str) -> Optional[str]:
    """基于表头快速检测分隔符，避免 read_csv_auto 误判整行单列"""
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            first_line = f.readline()
    except Exception:
        return None

    if not first_line:
        return None

    candidates = [",", "\t", "|", ";", chr(1)]
    best = None
    best_count = 0
    for delim in candidates:
        cnt = first_line.count(delim)
        if cnt > best_count:
            best = delim
            best_count = cnt

    return best if best_count > 0 else None


def _build_csv_read_expr(
    safe_file_path: str,
    delimiter: Optional[str] = None,
    tolerant: bool = False,
) -> str:
    """构造 DuckDB CSV 读取表达式"""
    opts: List[str] = []
    if delimiter:
        safe_delim = delimiter.replace("'", "''")
        opts.extend([
            f"delim='{safe_delim}'",
            "header=true",
            "quote='\"'",
            "escape='\"'",
        ])

    if tolerant:
        # 容错模式：允许坏行并放宽方言/列对齐限制
        opts.extend([
            "strict_mode=false",
            "ignore_errors=true",
            "null_padding=true",
            "sample_size=-1",
        ])

    opt_sql = f", {', '.join(opts)}" if opts else ""
    return f"read_csv_auto('{safe_file_path}'{opt_sql})"


def _create_table_from_csv_expr(engine, table_ident: str, csv_read_expr: str) -> None:
    engine.execute_command(
        f"""
        CREATE OR REPLACE TABLE {table_ident} AS
        SELECT * FROM {csv_read_expr}
        """
    )


def _load_csv_into_table(
    engine,
    table_name: str,
    table_ident: str,
    safe_file_path: str,
    delimiter: Optional[str],
) -> str:
    """加载 CSV 到 DuckDB（带多策略回退），返回成功策略名"""
    attempts: List[Tuple[str, str]] = []
    if delimiter:
        attempts.append((
            "detected_delimiter_tolerant",
            _build_csv_read_expr(safe_file_path, delimiter=delimiter, tolerant=True),
        ))
    attempts.append(("auto_detect_tolerant", _build_csv_read_expr(safe_file_path, delimiter=None, tolerant=True)))
    if delimiter and delimiter != ",":
        attempts.append(("comma_tolerant", _build_csv_read_expr(safe_file_path, delimiter=",", tolerant=True)))
    if delimiter:
        attempts.append((
            "detected_delimiter",
            _build_csv_read_expr(safe_file_path, delimiter=delimiter, tolerant=False),
        ))
    attempts.append(("auto_detect", _build_csv_read_expr(safe_file_path, delimiter=None, tolerant=False)))

    last_exc: Optional[Exception] = None
    last_suspicious: Optional[str] = None
    for strategy, read_expr in attempts:
        try:
            _create_table_from_csv_expr(engine, table_ident, read_expr)
            if _has_suspicious_single_blob_schema(engine, table_name):
                last_suspicious = strategy
                logger.warning(f"CSV 加载策略产出疑似单列 schema（{strategy}），继续尝试后续策略")
                continue
            return strategy
        except Exception as exc:
            last_exc = exc
            logger.warning(f"CSV 加载策略失败（{strategy}）: {exc}")

    if last_suspicious:
        logger.warning(f"CSV 所有策略均为疑似单列 schema，保留最后策略: {last_suspicious}")
        return last_suspicious
    if last_exc:
        raise last_exc
    raise RuntimeError("CSV 加载失败：未执行任何可用策略")


def _has_suspicious_single_blob_schema(engine, table_name: str) -> bool:
    """判断是否出现“整行被当成一个字段”的异常 schema"""
    try:
        schema = engine.get_schema(table_name)
    except Exception:
        return False

    if not schema or len(schema) != 1:
        return False

    col_name = str(schema[0].get("column_name", ""))
    return (col_name.count(",") >= 3) or ('","' in col_name) or col_name.startswith('"')


def _next_or_done(iterator) -> Tuple[bool, Optional[str]]:
    """线程中迭代同步生成器"""
    try:
        return False, next(iterator)
    except StopIteration:
        return True, None


def _build_result_schema(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    if not rows:
        return []
    first = rows[0]
    if not isinstance(first, dict):
        return []
    return [{"name": key, "type": "string"} for key in first.keys()]


def _strip_leading_sql_comments(sql: str) -> str:
    """移除 SQL 头部注释，便于识别首语句是否为 SELECT。"""
    text = (sql or "").lstrip()
    while text:
        if text.startswith("--"):
            idx = text.find("\n")
            if idx == -1:
                return ""
            text = text[idx + 1 :].lstrip()
            continue
        if text.startswith("#"):
            idx = text.find("\n")
            if idx == -1:
                return ""
            text = text[idx + 1 :].lstrip()
            continue
        if text.startswith("/*"):
            idx = text.find("*/")
            if idx == -1:
                return ""
            text = text[idx + 2 :].lstrip()
            continue
        break
    return text


def _resolve_scoped_table_name(raw_name: str, allowed_tables: List[str]) -> Optional[str]:
    """将手工 SQL 中的表名解析到当前数据集作用域表。"""
    if not raw_name:
        return None

    name = raw_name.strip().strip('"').strip("`")
    if not name:
        return None

    low = name.lower()
    normalized = _normalize_table_name(name).lower()
    allowed = [t for t in (allowed_tables or []) if t]
    if not allowed:
        return None

    direct_map: Dict[str, str] = {}
    unscoped_pairs: List[Tuple[str, str]] = []
    for scoped in allowed:
        scoped_low = scoped.lower()
        direct_map[scoped_low] = scoped

        unscoped = scoped.split("_", 1)[1] if scoped_low.startswith("ds") and "_" in scoped else scoped
        unscoped_low = unscoped.lower()
        direct_map[unscoped_low] = scoped
        direct_map[_normalize_table_name(unscoped).lower()] = scoped
        unscoped_pairs.append((scoped, unscoped_low))

    if low in direct_map:
        return direct_map[low]
    if normalized in direct_map:
        return direct_map[normalized]

    # 允许前缀匹配（仅在唯一命中时生效），兼容手工省略后缀的表名
    prefix_hits = [scoped for scoped, unscoped_low in unscoped_pairs if unscoped_low.startswith(low)]
    if len(prefix_hits) == 1:
        return prefix_hits[0]
    normalized_prefix_hits = [scoped for scoped, unscoped_low in unscoped_pairs if unscoped_low.startswith(normalized)]
    if len(normalized_prefix_hits) == 1:
        return normalized_prefix_hits[0]

    return None


def _normalize_manual_sql(sql: str, allowed_tables: List[str]) -> str:
    """
    规范化手工 SQL：
    1) 去掉头部注释
    2) MySQL 反引号改为双引号（DuckDB 兼容）
    3) 将未作用域表名映射到当前数据集作用域表名
    4) 去掉末尾分号
    """
    normalized = _strip_leading_sql_comments(sql or "")

    def _replace_backtick_identifier(match: re.Match) -> str:
        escaped = match.group(1).replace('"', '""')
        return f'"{escaped}"'

    normalized = re.sub(r"`([^`]+)`", _replace_backtick_identifier, normalized)
    normalized = normalized.strip().rstrip(";").strip()

    if not normalized:
        return normalized

    def _replace_table_ref(match: re.Match) -> str:
        keyword = match.group(1)
        table_expr = match.group(2)
        table_name = table_expr.strip().strip('"')
        resolved = _resolve_scoped_table_name(table_name, allowed_tables)
        if not resolved:
            return match.group(0)
        return f"{keyword} {resolved}"

    # 只改 FROM/JOIN 后面的表名，不碰子查询/别名等其余部分
    normalized = re.sub(
        r"(?i)\b(FROM|JOIN)\s+\"?([A-Za-z_][A-Za-z0-9_\-\.]*)\"?",
        _replace_table_ref,
        normalized,
    )
    return normalized


async def _ensure_workspace_access(db: AsyncSession, user: User, workspace_id: int) -> None:
    """校验当前用户是否有工作空间访问权限"""
    result = await db.execute(
        select(UserWorkspace.id).where(
            and_(
                UserWorkspace.workspace_id == workspace_id,
                UserWorkspace.user_id == user.id,
            )
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该工作空间",
        )


async def _get_authorized_dataset(
    db: AsyncSession,
    dataset_id: Optional[int],
    workspace_id: int,
) -> Optional[Dataset]:
    """按 workspace 约束查询数据集，避免越权"""
    if not dataset_id:
        return None

    result = await db.execute(
        select(Dataset).where(
            and_(
                Dataset.id == dataset_id,
                Dataset.workspace_id == workspace_id,
            )
        )
    )
    dataset = result.scalar_one_or_none()
    if not dataset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="数据集不存在",
        )
    return dataset


def load_dataset_to_duckdb(
    dataset: Dataset,
    table_names: Optional[List[str]] = None,
) -> List[str]:
    """加载数据集的 CSV 文件到 DuckDB，并返回作用域化后的可用表名"""
    if not dataset:
        return []

    engine = get_engine()
    engine.connect()

    data_source_ids = dataset.data_source_ids or []
    if dataset.data_source_id and dataset.data_source_id not in data_source_ids:
        data_source_ids.append(dataset.data_source_id)

    if not data_source_ids:
        return []

    from app.core.database import engine as sync_engine

    with sync_engine.begin() as conn:
        placeholders = ",".join([f":id{i}" for i in range(len(data_source_ids))])
        params = {f"id{i}": value for i, value in enumerate(data_source_ids)}
        result = conn.execute(
            text(
                f"""
                SELECT id, data_source_id, filename, file_path
                FROM csv_files WHERE data_source_id IN ({placeholders})
                """
            ),
            params,
        )
        csv_files = result.fetchall()

    selected_table_keys = {
        _normalize_table_name(name).lower() for name in (table_names or []) if name
    }

    loaded_tables: List[str] = []

    for csv_file in csv_files:
        resolved_path = _resolve_csv_file_path(csv_file.file_path)
        if not resolved_path:
            logger.warning(f"CSV 文件不存在，跳过加载: {csv_file.file_path}")
            continue

        raw_table_name = _normalize_table_name(csv_file.filename)
        scoped_table_name = _scoped_table_name(dataset.id, raw_table_name)

        if selected_table_keys:
            if raw_table_name.lower() not in selected_table_keys and scoped_table_name.lower() not in selected_table_keys:
                logger.info(f"跳过表 {raw_table_name}（用户未选择）")
                continue

        try:
            table_ident = _quote_ident(scoped_table_name)
            if csv_file.file_path != resolved_path:
                logger.info(f"CSV 路径已自动修复: {csv_file.file_path} -> {resolved_path}")
            safe_file_path = str(resolved_path).replace("'", "''")
            detected_delim = _detect_csv_delimiter(str(resolved_path))
            load_strategy = _load_csv_into_table(engine, scoped_table_name, table_ident, safe_file_path, detected_delim)

            # read_csv_auto 在极端数据下可能误判分隔符，导致整行单列；这里做一次自愈重载
            if _has_suspicious_single_blob_schema(engine, scoped_table_name):
                logger.warning(f"检测到 CSV 解析为单列表头，尝试按逗号重载: {scoped_table_name}")
                reload_strategy = _load_csv_into_table(engine, scoped_table_name, table_ident, safe_file_path, ",")
                load_strategy = f"{load_strategy}->{reload_strategy}"
                if _has_suspicious_single_blob_schema(engine, scoped_table_name):
                    logger.warning(f"CSV 重载后仍为疑似单列 schema: {scoped_table_name}")

            logger.info(f"已加载 CSV 到 DuckDB: {scoped_table_name}, strategy={load_strategy}")
            loaded_tables.append(scoped_table_name)
        except Exception as exc:
            logger.error(f"加载 CSV 失败: {scoped_table_name}, error={exc}")
            # 回退：若该作用域表已在 DuckDB 中存在（例如上次成功加载后本次重载失败），
            # 仍将其纳入可用表，避免多表查询被误判为单表。
            try:
                if engine.table_exists(scoped_table_name):
                    logger.warning(f"复用已存在 DuckDB 表: {scoped_table_name}")
                    loaded_tables.append(scoped_table_name)
            except Exception as fallback_exc:
                logger.warning(f"检查已存在 DuckDB 表失败: {scoped_table_name}, error={fallback_exc}")

    return list(dict.fromkeys(loaded_tables))


def _build_query_response_data(
    question: str,
    dataset: Optional[Dataset],
    trace_id: str,
    audit_id: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """将 LangGraph final state 映射为 API QueryResponse"""
    final = state.get("final_answer") or {}
    intent = final.get("type") or state.get("intent") or "list"
    sql_text = state.get("sql") or final.get("sql") or ""
    sql_params = state.get("sql_params") if isinstance(state.get("sql_params"), list) else []

    status_value = "error" if (final.get("status") == "error" or state.get("error_message")) else "success"

    error_value = None
    if status_value == "error":
        err_obj = final.get("error")
        if isinstance(err_obj, dict):
            error_value = err_obj.get("message")
        elif isinstance(err_obj, str):
            error_value = err_obj
        error_value = error_value or final.get("message") or state.get("error_message") or "查询失败"

    rows: List[Dict[str, Any]] = []
    answer = final.get("message") or final.get("answer_text") or ""

    if intent == "chat":
        rows = []
        answer = final.get("message") or final.get("value") or answer
    elif intent == "search":
        rows = final.get("value") if isinstance(final.get("value"), list) else []
        answer = final.get("message") or f"为您找到 {len(rows)} 条相关结果"
    elif intent == "count":
        state_rows = state.get("sql_result") if isinstance(state.get("sql_result"), list) else []
        if state_rows:
            rows = state_rows
        else:
            value = final.get("value")
            if value is not None:
                rows = [{"value": value}]
        answer = final.get("message") or answer
    else:
        rows = state.get("sql_result") if isinstance(state.get("sql_result"), list) else []
        if not rows and isinstance(final.get("value"), list):
            rows = final.get("value")
        answer = final.get("message") or answer or f"查询结果：返回 {len(rows)} 条记录"

    result_schema = _build_result_schema(rows)
    row_count = len(rows)

    chart_suggestion = final.get("chart_suggestion")
    if not chart_suggestion:
        if intent == "chat":
            chart_suggestion = "none"
        elif intent == "count":
            chart_suggestion = "bar"
        else:
            chart_suggestion = "table"

    evidence = final.get("evidence")
    execution_history = state.get("execution_history") if isinstance(state.get("execution_history"), list) else []
    filters = state.get("filters") if isinstance(state.get("filters"), dict) else {}
    plan_source = filters.get("plan_source")
    confidence = filters.get("confidence")
    clarification_needed = bool(filters.get("needs_clarification"))
    clarification_options = filters.get("clarification_options") if isinstance(filters.get("clarification_options"), list) else None

    return {
        "question": question,
        "normalized_question": question,
        "intent": intent,
        "matched_dataset": {"id": dataset.id, "name": dataset.name} if dataset else None,
        "sql": sql_text,
        "semantic_sql": sql_text,
        "executable_sql": sql_text,
        "sql_params": sql_params,
        "reasoning_summary": None,
        "result_schema": result_schema,
        "result_rows": rows[:1000],
        "row_count": row_count,
        "chart_suggestion": chart_suggestion,
        "cost_time_ms": None,
        "cost_rows": row_count,
        "warnings": [],
        "status": status_value,
        "error": error_value,
        "trace_id": trace_id,
        "audit_id": audit_id,
        "agent_steps": state.get("logs", []),
        "execution_history": execution_history,
        "evidence": evidence,
        "answer": answer,
        "plan_source": plan_source,
        "confidence": confidence,
        "clarification_needed": clarification_needed,
        "clarification_options": clarification_options,
    }


@router.post("/stream")
async def stream_query(
    request: QueryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """流式执行自然语言查询 - Server-Sent Events"""
    logger.info("==================== 收到流式查询请求 ====================")
    logger.info(f"问题: {request.question}, dataset_id: {request.dataset_id}, workspace_id: {request.workspace_id}")
    logger.info(f"用户: {current_user.username}")

    await _ensure_workspace_access(db, current_user, request.workspace_id)
    dataset = await _get_authorized_dataset(db, request.dataset_id, request.workspace_id)

    engine = get_engine()
    engine.connect()
    selected_tables = request.table_names or []

    if dataset:
        logger.info(f"开始加载数据集: {dataset.name}, 用户选择的表: {request.table_names}")
        loaded_tables = await run_in_threadpool(load_dataset_to_duckdb, dataset, request.table_names)
        selected_tables = loaded_tables
        logger.info(f"DuckDB 表列表: {engine.get_tables()}")
        logger.info(f"传递给 orchestrator 的 table_names: {selected_tables}")

    trace_id = f"trace_{uuid.uuid4().hex[:16]}"
    audit_id = f"audit_{uuid.uuid4().hex[:16]}"
    logger.info(f"trace_id: {trace_id}, audit_id: {audit_id}")
    request_context = request.context.model_dump() if request.context else None

    def _inject_trace_meta(raw_event: str) -> str:
        if not raw_event.startswith("data: "):
            return raw_event
        payload = raw_event[6:].strip()
        if not payload:
            return raw_event
        try:
            event_obj = json.loads(payload)
        except Exception:
            return raw_event

        if event_obj.get("type") in {"final", "error", "done"}:
            event_obj["trace_id"] = trace_id
            event_obj["audit_id"] = audit_id
            return f"data: {json.dumps(event_obj, ensure_ascii=False)}\n\n"
        return raw_event

    async def event_generator():
        logger.info("==================== 开始生成事件 ====================")
        orchestrator = LangGraphOrchestrator()
        logger.info("LangGraphOrchestrator 创建成功")

        try:
            event_count = 0
            iterator = orchestrator.stream_events(
                request.question,
                selected_tables,
                request_context,
            )
            while True:
                done, event = await asyncio.to_thread(_next_or_done, iterator)
                if done:
                    break
                event_count += 1
                if event is None:
                    continue
                transformed = _inject_trace_meta(event)
                logger.info(f"生成事件 {event_count}: {transformed[:100] if len(transformed) > 100 else transformed}")
                yield transformed

            logger.info(f"==================== 事件生成完成，共 {event_count} 个事件 ====================")
        except Exception as exc:
            logger.error(f"查询执行失败: {exc}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc), 'trace_id': trace_id, 'audit_id': audit_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("", response_model=QueryResponse)
async def execute_query(
    request: QueryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """执行自然语言查询 - 使用 LangGraph 新架构"""
    logger.info(f"收到查询请求: {request.question}, dataset_id: {request.dataset_id}")

    await _ensure_workspace_access(db, current_user, request.workspace_id)
    dataset = await _get_authorized_dataset(db, request.dataset_id, request.workspace_id)

    engine = get_engine()
    engine.connect()
    selected_tables = request.table_names or []
    if dataset:
        loaded_tables = await run_in_threadpool(load_dataset_to_duckdb, dataset, request.table_names)
        selected_tables = loaded_tables

    trace_id = f"trace_{uuid.uuid4().hex[:16]}"
    audit_id = f"audit_{uuid.uuid4().hex[:16]}"

    logger.info(f"开始执行查询: {request.question}")
    request_context = request.context.model_dump() if request.context else None

    try:
        state = await run_in_threadpool(
            run_stream,
            request.question,
            selected_tables if selected_tables else None,
            request_context,
        )
    except Exception as exc:
        logger.error(f"查询执行失败: {exc}", exc_info=True)
        state = {
            "final_answer": {
                "status": "error",
                "message": f"查询执行失败: {str(exc)}",
                "error": {"message": f"查询执行失败: {str(exc)}"},
            },
            "error_message": str(exc),
            "logs": [],
        }

    response_data = _build_query_response_data(
        question=request.question,
        dataset=dataset,
        trace_id=trace_id,
        audit_id=audit_id,
        state=state,
    )
    return QueryResponse(**response_data)


@router.get("/{trace_id}/replay")
async def replay_query(
    trace_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回放查询（重新执行）"""
    result = await db.execute(
        select(QueryHistory).where(
            and_(
                QueryHistory.trace_id == trace_id,
                QueryHistory.user_id == current_user.id,
            )
        )
    )
    query_history = result.scalar_one_or_none()

    if not query_history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="查询记录不存在",
        )

    await _ensure_workspace_access(db, current_user, query_history.workspace_id)

    selected_tables: Optional[List[str]] = None
    if query_history.dataset_id:
        ds_result = await db.execute(
            select(Dataset).where(
                and_(
                    Dataset.id == query_history.dataset_id,
                    Dataset.workspace_id == query_history.workspace_id,
                )
            )
        )
        dataset = ds_result.scalar_one_or_none()
        if dataset:
            loaded_tables = await run_in_threadpool(load_dataset_to_duckdb, dataset, None)
            selected_tables = loaded_tables if loaded_tables else None

    try:
        replay_result = await run_in_threadpool(
            run_stream,
            query_history.question,
            selected_tables,
        )
    except Exception as exc:
        replay_result = {
            "final_answer": {
                "status": "error",
                "message": str(exc),
                "error": {"message": str(exc)},
            },
            "error_message": str(exc),
            "logs": [],
        }

    return replay_result


@router.post("/execute-sql", response_model=QueryResponse)
async def execute_sql(
    request: ExecuteSqlRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """执行编辑后的 SQL"""
    logger.info(f"收到执行 SQL 请求: {request.sql}, dataset_id: {request.dataset_id}")

    await _ensure_workspace_access(db, current_user, request.workspace_id)
    dataset = await _get_authorized_dataset(db, request.dataset_id, request.workspace_id)

    engine = get_engine()
    engine.connect()

    selected_tables = request.table_names or []
    if dataset:
        loaded_tables = await run_in_threadpool(load_dataset_to_duckdb, dataset, request.table_names)
        selected_tables = loaded_tables

    allowed_tables = selected_tables or [
        _normalize_table_name(name) for name in (request.table_names or []) if name
    ]
    sql_params = SQLGuardrail.sanitize_params(request.sql_params or [])

    trace_id = f"trace_{uuid.uuid4().hex[:16]}"
    audit_id = f"audit_{uuid.uuid4().hex[:16]}"

    try:
        normalized_sql = _normalize_manual_sql(request.sql, allowed_tables)
        if not normalized_sql:
            raise RuntimeError("SQL 语句不能为空")

        if dataset and not selected_tables:
            raise RuntimeError(
                f"数据集「{dataset.name}」未加载到可用数据表，请检查 CSV 文件路径是否有效并重新上传文件后重试"
            )

        SQLGuardrail.validate_sql(normalized_sql, allowed_tables=allowed_tables if allowed_tables else None)
        explain_result = await run_in_threadpool(engine.explain, normalized_sql, sql_params)
        if not explain_result.get("ok"):
            raise RuntimeError(f"SQL 预检失败: {explain_result.get('error')}")

        exec_result = await run_in_threadpool(engine.execute, normalized_sql, sql_params)
        if exec_result.get("error"):
            raise RuntimeError(exec_result["error"])

        rows = exec_result.get("rows", [])
        columns = exec_result.get("columns", [])
        row_count = int(exec_result.get("row_count", len(rows)))
        execution_time_ms = exec_result.get("execution_time_ms")

        sql_upper = normalized_sql.upper()
        has_aggregate = any(fn in sql_upper for fn in ("COUNT(", "SUM(", "AVG(", "MAX(", "MIN("))
        intent = "count" if has_aggregate else "list"

        response_data = {
            "question": "执行 SQL",
            "normalized_question": "执行 SQL",
            "intent": intent,
            "matched_dataset": {"id": dataset.id, "name": dataset.name} if dataset else None,
            "sql": normalized_sql,
            "semantic_sql": normalized_sql,
            "executable_sql": normalized_sql,
            "sql_params": sql_params,
            "reasoning_summary": None,
            "result_schema": [{"name": c, "type": "string"} for c in columns],
            "result_rows": rows[:1000],
            "row_count": row_count,
            "chart_suggestion": "bar" if intent == "count" else "table",
            "cost_time_ms": execution_time_ms,
            "cost_rows": row_count,
            "warnings": [],
            "status": "success",
            "error": None,
            "trace_id": trace_id,
            "audit_id": audit_id,
            "agent_steps": [],
            "execution_history": [],
            "evidence": {
                "question": "执行 SQL",
                "intent": intent,
                "source_tables": allowed_tables,
                "sql": normalized_sql,
                "sql_params": sql_params,
                "row_count": row_count,
                "sample_rows": rows[:3],
                "evidence_type": "record" if intent == "list" else "aggregate",
                "summary": "结果来自手工执行 SQL 的直接输出样本。",
            },
            "answer": f"执行成功，返回 {row_count} 条记录" if intent == "list" else f"统计完成，共 {row_count} 条分组结果",
            "plan_source": "manual_sql",
            "confidence": 1.0,
            "clarification_needed": False,
            "clarification_options": None,
        }
    except (SQLSecurityError, Exception) as exc:
        logger.error(f"SQL 执行失败: {exc}", exc_info=True)
        response_data = {
            "question": "执行 SQL",
            "normalized_question": "执行 SQL",
            "intent": "list",
            "matched_dataset": {"id": dataset.id, "name": dataset.name} if dataset else None,
            "sql": request.sql,
            "semantic_sql": request.sql,
            "executable_sql": request.sql,
            "sql_params": sql_params,
            "reasoning_summary": None,
            "result_schema": [],
            "result_rows": [],
            "row_count": 0,
            "chart_suggestion": "table",
            "cost_time_ms": None,
            "cost_rows": 0,
            "warnings": [],
            "status": "error",
            "error": str(exc),
            "trace_id": trace_id,
            "audit_id": audit_id,
            "agent_steps": [],
            "execution_history": [],
            "evidence": None,
            "answer": f"SQL 执行失败: {str(exc)}",
            "plan_source": "manual_sql",
            "confidence": 0.0,
            "clarification_needed": False,
            "clarification_options": None,
        }

    return QueryResponse(**response_data)
