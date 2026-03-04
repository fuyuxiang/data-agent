"""
NL2SQL 查询 API - 使用 LangGraph 新架构
"""
import uuid
import json
import os
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.api.auth import get_current_user
from app.core.logging import get_logger
from app.models.models import User, Dataset, QueryHistory, CSVFile
from app.schemas.schemas import QueryRequest, QueryResponse
from app.orchestrator_graph import LangGraphOrchestrator, run_stream
from app.orchestrator_duckdb import get_engine

logger = get_logger(__name__)
router = APIRouter()


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


def load_dataset_to_duckdb(db: AsyncSession, dataset: Dataset, table_names: Optional[List[str]] = None) -> List[str]:
    """加载数据集的 CSV 文件到 DuckDB

    Args:
        db: 数据库会话
        dataset: 数据集
        table_names: 用户选择的表名列表，如果为 None 则加载所有表

    Returns:
        实际可用的 DuckDB 表名列表
    """
    if not dataset:
        return []

    engine = get_engine()
    engine.connect()

    # 获取数据集关联的数据源
    data_source_ids = dataset.data_source_ids or []
    if dataset.data_source_id and dataset.data_source_id not in data_source_ids:
        data_source_ids.append(dataset.data_source_id)

    if not data_source_ids:
        return []

    # 查询 CSV 文件（同步执行，因为 DuckDB 是同步的）
    from sqlalchemy import text
    from app.core.database import engine as sync_engine

    with sync_engine.begin() as conn:
        placeholders = ','.join([f':id{i}' for i in range(len(data_source_ids))])
        params = {f'id{i}': v for i, v in enumerate(data_source_ids)}
        result = conn.execute(text(f"""
            SELECT id, data_source_id, filename, file_path, file_size, row_count, column_count, created_at
            FROM csv_files WHERE data_source_id IN ({placeholders})
        """), params)
        csv_files = result.fetchall()

    selected_table_keys = {
        _normalize_table_name(name).lower() for name in (table_names or []) if name
    }
    loaded_tables: List[str] = []

    # 加载每个 CSV 文件到 DuckDB
    for csv_file in csv_files:
        file_path = csv_file.file_path
        if file_path and os.path.exists(file_path):
            try:
                # 使用文件名作为表名
                table_name = _normalize_table_name(csv_file.filename)

                # 如果指定了 table_names，检查是否需要加载这个表
                if selected_table_keys and table_name.lower() not in selected_table_keys:
                    logger.info(f"跳过表 {table_name}（用户未选择）")
                    continue

                # 使用 DuckDB 读取 CSV
                engine.conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table_name} AS
                    SELECT * FROM read_csv_auto('{file_path}')
                """)
                logger.info(f"已加载 CSV 到 DuckDB: {table_name}")
                loaded_tables.append(table_name)
            except Exception as e:
                logger.error(f"加载 CSV 失败: {e}")

    # 兜底：如果没加载出任何表但用户有选择，则返回标准化后的用户选择
    if not loaded_tables and table_names:
        return list(dict.fromkeys([_normalize_table_name(name) for name in table_names if name]))

    return list(dict.fromkeys(loaded_tables))


@router.post("/stream")
async def stream_query(
    request: QueryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """流式执行自然语言查询 - Server-Sent Events"""
    logger.info(f"==================== 收到流式查询请求 ====================")
    logger.info(f"问题: {request.question}, dataset_id: {request.dataset_id}, workspace_id: {request.workspace_id}")
    logger.info(f"用户: {current_user.username}")

    # 验证数据集（可选）
    dataset = None
    if request.dataset_id:
        result = await db.execute(
            select(Dataset).where(Dataset.id == request.dataset_id)
        )
        dataset = result.scalar_one_or_none()
        logger.info(f"数据集验证: {dataset.name if dataset else 'None'}")
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="数据集不存在",
            )

    # 初始化数据到 DuckDB
    engine = get_engine()
    engine.connect()
    selected_tables = request.table_names or []

    # 加载数据集的 CSV 数据到 DuckDB
    if dataset:
        logger.info(f"开始加载数据集: {dataset.name}, 用户选择的表: {request.table_names}")
        loaded_tables = load_dataset_to_duckdb(db, dataset, request.table_names)
        if loaded_tables:
            selected_tables = loaded_tables
        logger.info(f"DuckDB 表列表: {engine.get_tables()}")
        logger.info(f"传递给 orchestrator 的 table_names: {selected_tables}")

    # 生成 trace_id
    trace_id = f"trace_{uuid.uuid4().hex[:16]}"
    audit_id = f"audit_{uuid.uuid4().hex[:16]}"
    logger.info(f"trace_id: {trace_id}, audit_id: {audit_id}")

    async def event_generator():
        logger.info("==================== 开始生成事件 ====================")
        orchestrator = LangGraphOrchestrator()
        logger.info("LangGraphOrchestrator 创建成功")

        try:
            # 将同步生成器转换为异步生成器
            event_count = 0
            for event in orchestrator.stream_events(request.question, selected_tables):
                event_count += 1
                logger.info(f"生成事件 {event_count}: {event[:100] if len(event) > 100 else event}")
                yield event

            logger.info(f"==================== 事件生成完成，共 {event_count} 个事件 ====================")
        except Exception as e:
            logger.error(f"查询执行失败: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"

        # 执行完成后，保存到历史记录（这里简化为最后输出最终结果）
        yield f"data: {json.dumps({'type': 'final', 'trace_id': trace_id, 'audit_id': audit_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@router.post("", response_model=QueryResponse)
async def execute_query(
    request: QueryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """执行自然语言查询 - 使用 LangGraph 新架构"""
    logger.info(f"收到查询请求: {request.question}, dataset_id: {request.dataset_id}")

    # 验证数据集（可选）
    dataset = None
    if request.dataset_id:
        result = await db.execute(
            select(Dataset).where(Dataset.id == request.dataset_id)
        )
        dataset = result.scalar_one_or_none()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="数据集不存在",
            )

    # 初始化数据到 DuckDB（如果有数据集）
    engine = get_engine()
    engine.connect()
    selected_tables = request.table_names or []
    if dataset:
        loaded_tables = load_dataset_to_duckdb(db, dataset, request.table_names)
        if loaded_tables:
            selected_tables = loaded_tables

    # 生成 trace_id
    trace_id = f"trace_{uuid.uuid4().hex[:16]}"
    audit_id = f"audit_{uuid.uuid4().hex[:16]}"

    logger.info(f"开始执行查询: {request.question}")

    # 使用 LangGraph 编排器执行
    try:
        result = run_stream(request.question, selected_tables if selected_tables else None)
    except Exception as e:
        logger.error(f"查询执行失败: {e}", exc_info=True)
        result = {
            "final_answer": {
                "status": "error",
                "answer_text": f"查询执行失败: {str(e)}",
            },
            "logs": [],
        }

    # 构建响应 - 兼容新旧格式
    # 新格式: final_answer 包含 type, value, message
    # 旧格式: final_answer 包含 columns, rows, row_count 等
    final = result.get("final_answer") or {}
    intent_type = final.get("type") or final.get("intent") or result.get("intent")

    # 处理不同意图类型的响应
    if final.get("type") == "chat":
        # 闲聊类型
        response_data = {
            "question": request.question,
            "normalized_question": request.question,
            "intent": "chat",
            "matched_dataset": {"id": dataset.id, "name": dataset.name} if dataset else None,
            "sql": result.get("sql", ""),
            "semantic_sql": result.get("sql", ""),
            "executable_sql": result.get("sql", ""),
            "reasoning_summary": None,
            "result_schema": [],
            "result_rows": [],
            "row_count": 0,
            "chart_suggestion": "none",
            "cost_time_ms": None,
            "cost_rows": 0,
            "warnings": [],
            "status": "success",
            "error": None,
            "trace_id": trace_id,
            "audit_id": audit_id,
            "agent_steps": result.get("logs", []),
            "answer": final.get("message") or final.get("value") or "",
        }
    elif final.get("type") == "search":
        # 向量检索类型
        rows = final.get("value") or []
        response_data = {
            "question": request.question,
            "normalized_question": request.question,
            "intent": "search",
            "matched_dataset": {"id": dataset.id, "name": dataset.name} if dataset else None,
            "sql": result.get("sql", ""),
            "semantic_sql": result.get("sql", ""),
            "executable_sql": result.get("sql", ""),
            "reasoning_summary": None,
            "result_schema": [{"name": c, "type": "string"} for c in (list(rows[0].keys()) if rows else [])],
            "result_rows": rows[:1000],
            "row_count": len(rows),
            "chart_suggestion": "table",
            "cost_time_ms": None,
            "cost_rows": len(rows),
            "warnings": [],
            "status": "success",
            "error": None,
            "trace_id": trace_id,
            "audit_id": audit_id,
            "agent_steps": result.get("logs", []),
            "answer": final.get("message") or f"为您找到 {len(rows)} 条相关结果",
        }
    else:
        # 原有格式或其他类型
        exec_result = final.get("result", {})
        response_data = {
            "question": request.question,
            "normalized_question": request.question,
            "intent": intent_type,
            "matched_dataset": {"id": dataset.id, "name": dataset.name} if dataset else None,
            "sql": result.get("sql", ""),
            "semantic_sql": result.get("sql", ""),
            "executable_sql": result.get("sql", ""),
            "reasoning_summary": None,
            "result_schema": [{"name": c, "type": "string"} for c in final.get("columns", [])],
            "result_rows": exec_result.get("rows", []) if isinstance(exec_result, dict) else [],
            "row_count": final.get("row_count", 0),
            "chart_suggestion": final.get("chart_suggestion"),
            "cost_time_ms": None,
            "cost_rows": final.get("row_count", 0),
            "warnings": [],
            "status": final.get("status", "error"),
            "error": final.get("error", {}).get("message") if isinstance(final.get("error"), dict) else None,
            "trace_id": trace_id,
            "audit_id": audit_id,
            "agent_steps": result.get("logs", []),
            "answer": final.get("answer_text") or final.get("message"),
        }

    # 确保 sql 字段存在
    logger.info(f"[execute_query] result.get('sql'): {result.get('sql', 'NOT FOUND')[:100] if result.get('sql') else 'NOT FOUND'}")
    if "sql" not in response_data:
        response_data["sql"] = result.get("sql", "")

    return QueryResponse(**response_data)


@router.get("/{trace_id}/replay")
async def replay_query(
    trace_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回放查询（重新执行）"""
    result = await db.execute(
        select(QueryHistory).where(QueryHistory.trace_id == trace_id)
    )
    query_history = result.scalar_one_or_none()

    if not query_history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="查询记录不存在",
        )

    # 重新执行
    try:
        result = run_stream(query_history.question)
    except Exception as e:
        result = {
            "final_answer": {"status": "error", "answer_text": str(e)},
            "logs": [],
        }

    return result
