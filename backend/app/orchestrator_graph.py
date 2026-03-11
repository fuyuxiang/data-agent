"""
LangGraph 编排器 - 增强版

状态流转：
START → PARSE_QUESTION → VALIDATE_SQL → EXECUTE_SQL → SEMANTIC_ENHANCE → FORMAT_ANSWER → END
                ↓ (search intent)                       (list only)
            VECTOR_SEARCH → FORMAT_ANSWER → END
                                ↓ (validation/execution failed)
                            FIX_SQL (自我修正, max retries → ERROR)
"""

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.orchestrator_duckdb import get_engine
from app.orchestrator_log_utils import LogHelper
from app.services.guardrails import SQLGuardrail


class AgentState(TypedDict):
    """Agent 执行状态"""
    question: str
    intent: Optional[str]
    sql: Optional[str]
    sql_params: Optional[List]
    filters: Optional[Dict]

    sql_result: Optional[List[Dict]]
    final_answer: Optional[Any]

    error_message: Optional[str]
    retry_count: int
    max_retries: int

    semantic_scores: Optional[Dict]
    vector_only_results: Optional[List[Dict]]

    execution_history: List[Dict]
    logs: List[Dict]
    _run_id: str
    selected_tables: Optional[List[str]]


# ==================== 日志辅助 ====================

_log_helpers: Dict[str, LogHelper] = {}

def _get_logger(run_id: str) -> LogHelper:
    """获取日志助手"""
    global _log_helpers
    if run_id not in _log_helpers:
        _log_helpers[run_id] = LogHelper(run_id)
    return _log_helpers[run_id]


def _sanitize_history_payload(payload: Any, max_len: int = 240) -> Any:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload if len(payload) <= max_len else payload[: max_len - 3] + "..."
    if isinstance(payload, dict):
        sanitized: Dict[str, Any] = {}
        for idx, (k, v) in enumerate(payload.items()):
            if idx >= 20:
                sanitized["..."] = "truncated"
                break
            sanitized[k] = _sanitize_history_payload(v, max_len=max_len)
        return sanitized
    if isinstance(payload, list):
        return [_sanitize_history_payload(v, max_len=max_len) for v in payload[:10]]
    return payload


def _append_execution_history(
    state: AgentState,
    *,
    step: str,
    started_at: float,
    status: str,
    input_data: Optional[Dict[str, Any]] = None,
    output_data: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    end_ts = time.time()
    history = state.setdefault("execution_history", [])
    item: Dict[str, Any] = {
        "step": step,
        "status": status,
        "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "ended_at": datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration_ms": int((end_ts - started_at) * 1000),
    }
    if input_data:
        item["input"] = _sanitize_history_payload(input_data)
    if output_data:
        item["output"] = _sanitize_history_payload(output_data)
    if error:
        item["error"] = str(error)[:500]
    history.append(item)


def _parse_iso_timestamp(value: Optional[str], fallback: float) -> float:
    if not value:
        return fallback
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return fallback


def _persist_trace(question: str, state: AgentState, run_id: str) -> None:
    """持久化 Trace，并自动写入 SQL 缓存"""
    try:
        from app.services.trace import QueryTrace, get_trace_manager

        manager = get_trace_manager()
        if not manager:
            return

        trace = QueryTrace(question=question, session_id=run_id)
        trace.intent = state.get("intent")
        trace.sql = state.get("sql")
        trace.sql_params = state.get("sql_params") or []
        sql_result = state.get("sql_result") if isinstance(state.get("sql_result"), list) else []
        trace.result_count = len(sql_result)
        trace.final_answer = state.get("final_answer")
        trace.metadata = {
            "selected_tables": state.get("selected_tables") or [],
            "execution_history": state.get("execution_history") or [],
        }

        now_ts = time.time()
        for item in state.get("execution_history") or []:
            step_name = item.get("step", "unknown")
            step = trace.add_step(step_name, input_data=item.get("input"))
            step.start_time = _parse_iso_timestamp(item.get("started_at"), now_ts)
            step.end_time = _parse_iso_timestamp(item.get("ended_at"), step.start_time)
            step.duration_ms = item.get("duration_ms") or int((step.end_time - step.start_time) * 1000)
            step.status = item.get("status", "success")
            step.output_data = item.get("output")
            if item.get("error"):
                step.error_message = str(item.get("error"))

        has_error = bool(state.get("error_message")) or (state.get("final_answer") or {}).get("status") == "error"
        trace.status = "error" if has_error else "success"
        trace.error_message = state.get("error_message")
        trace.finish(status=trace.status)

        manager.save_trace(trace)
    except Exception as trace_error:
        print(f"[trace] save failed: {trace_error}")


def parse_question_node(state: AgentState) -> AgentState:
    """解析用户问题节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    question = state["question"]
    started_at = time.time()
    history_input = {
        "question": question,
        "selected_tables": state.get("selected_tables") or [],
    }
    logger.start_node("parse_question", {"question": question[:100]})

    try:
        from app.services.nl2sql import build_query_plan

        # 获取可用表名
        selected_tables = state.get("selected_tables")
        if not selected_tables:
            selected_tables = []
            state["selected_tables"] = []

        config = {"llm": {"enabled": True}}  # 启用 LLM
        plan = build_query_plan(question, config, table_names=selected_tables)

        state["intent"] = plan.intent
        state["sql"] = plan.sql
        state["sql_params"] = plan.params
        state["filters"] = plan.filters

        if not selected_tables and plan.intent not in {"chat", "search"}:
            err_msg = "未找到可查询的数据表：请先选择数据表，或检查数据集文件是否已成功加载"
            state["error_message"] = err_msg
            logger.error_node("parse_question", {"error": err_msg})
            _append_execution_history(
                state,
                step="parse_question",
                started_at=started_at,
                status="error",
                input_data=history_input,
                error=err_msg,
            )
            return state

        if (plan.filters or {}).get("plan_rejected"):
            reject_reason = (plan.filters or {}).get("reject_reason") or "无法可靠理解你的问题，请补充更明确条件后重试"
            state["error_message"] = reject_reason
            state["sql"] = ""
            state["sql_params"] = []
            logger.error_node("parse_question", {
                "error": reject_reason,
                "plan_source": (plan.filters or {}).get("plan_source", "reject"),
                "semantic_judge": (plan.filters or {}).get("semantic_judge", {}),
            })
            _append_execution_history(
                state,
                step="parse_question",
                started_at=started_at,
                status="error",
                input_data=history_input,
                output_data={"plan_source": (plan.filters or {}).get("plan_source", "reject")},
                error=reject_reason,
            )
            return state

        logger.end_node("parse_question", {
            "intent": plan.intent,
            "sql": plan.sql[:200] if plan.sql else "",
            "selected_table": (plan.filters or {}).get("selected_table"),
            "candidate_tables": (plan.filters or {}).get("candidate_tables", []),
            "plan_source": (plan.filters or {}).get("plan_source", "rule"),
            "semantic_judge": (plan.filters or {}).get("semantic_judge", {}),
        })
        _append_execution_history(
            state,
            step="parse_question",
            started_at=started_at,
            status="success",
            input_data=history_input,
            output_data={
                "intent": plan.intent,
                "plan_source": (plan.filters or {}).get("plan_source", "rule"),
                "selected_table": (plan.filters or {}).get("selected_table"),
            },
        )

    except Exception as e:
        state["error_message"] = f"问题解析失败: {str(e)}"
        logger.error_node("parse_question", {"error": str(e)})
        _append_execution_history(
            state,
            step="parse_question",
            started_at=started_at,
            status="error",
            input_data=history_input,
            error=state["error_message"],
        )

    return state


# ==================== 向量检索 ====================

def vector_search_node(state: AgentState) -> AgentState:
    """向量检索节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    question = state["question"]
    filters = state.get("filters") or {}
    top_k = filters.get("top_k", 10)
    started_at = time.time()
    history_input = {"question": question, "top_k": top_k}

    logger.start_node("vector_search", {"question": question[:100], "top_k": top_k})

    try:
        from app.services.vector_search import vector_search

        config = {"search": {"lancedb_dir": "data/lancedb"}}
        results = vector_search(config, question, top_k=top_k)

        state["sql_result"] = results
        state["error_message"] = None

        logger.end_node("vector_search", {
            "result_count": len(results)
        })
        _append_execution_history(
            state,
            step="vector_search",
            started_at=started_at,
            status="success",
            input_data=history_input,
            output_data={"result_count": len(results)},
        )

    except Exception as e:
        state["error_message"] = f"向量检索失败: {str(e)}"
        logger.error_node("vector_search", {"error": str(e)})
        _append_execution_history(
            state,
            step="vector_search",
            started_at=started_at,
            status="error",
            input_data=history_input,
            error=state["error_message"],
        )

    return state


# ==================== SQL 验证 ====================

def validate_sql_node(state: AgentState) -> AgentState:
    """SQL 验证节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    sql = state.get("sql", "")
    selected_tables = state.get("selected_tables") or []
    allowed_tables = [t for t in selected_tables if t]
    started_at = time.time()
    params = state.get("sql_params") or []
    history_input = {"sql": sql, "params": params, "allowed_tables": allowed_tables}
    logger.start_node("validate_sql", {"sql": sql[:100]})

    try:
        SQLGuardrail.validate_sql(sql, allowed_tables=allowed_tables if allowed_tables else None)
        # Dry-run: 执行 EXPLAIN 预检，提前暴露字段/语法错误
        engine = get_engine()
        engine.connect()
        explain_result = engine.explain(sql, params)
        if not explain_result.get("ok"):
            raise RuntimeError(f"SQL 预检失败: {explain_result.get('error')}")

        logger.end_node("validate_sql", {"valid": True, "dry_run": "ok", "allowed_tables": allowed_tables[:5]})
        _append_execution_history(
            state,
            step="validate_sql",
            started_at=started_at,
            status="success",
            input_data=history_input,
            output_data={"valid": True, "dry_run": "ok"},
        )
    except Exception as e:
        state["error_message"] = f"SQL 验证失败: {str(e)}"
        logger.error_node("validate_sql", {"error": str(e)})
        _append_execution_history(
            state,
            step="validate_sql",
            started_at=started_at,
            status="error",
            input_data=history_input,
            error=state["error_message"],
        )

    return state


# ==================== SQL 执行 ====================

def execute_sql_node(state: AgentState) -> AgentState:
    """SQL 执行节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    sql = state.get("sql", "")
    params = state.get("sql_params", [])
    started_at = time.time()
    history_input = {"sql": sql, "params": params}

    logger.start_node("execute_sql", {"sql": sql[:100]})

    try:
        engine = get_engine()
        engine.connect()
        result = engine.execute(sql, params)

        if result.get("error"):
            state["error_message"] = result["error"]
            state["sql_result"] = []
            logger.error_node("execute_sql", {"error": result["error"]})
            _append_execution_history(
                state,
                step="execute_sql",
                started_at=started_at,
                status="error",
                input_data=history_input,
                error=result["error"],
            )
        else:
            state["sql_result"] = result.get("rows", [])
            state["error_message"] = None
            logger.end_node("execute_sql", {
                "row_count": len(state["sql_result"])
            })
            _append_execution_history(
                state,
                step="execute_sql",
                started_at=started_at,
                status="success",
                input_data=history_input,
                output_data={"row_count": len(state["sql_result"])},
            )

    except Exception as e:
        state["error_message"] = f"SQL 执行失败: {str(e)}"
        state["sql_result"] = []
        logger.error_node("execute_sql", {"error": str(e)})
        _append_execution_history(
            state,
            step="execute_sql",
            started_at=started_at,
            status="error",
            input_data=history_input,
            error=state["error_message"],
        )

    return state


# ==================== 语义增强 ====================

def semantic_enhance_node(state: AgentState) -> AgentState:
    """语义增强节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    intent = state.get("intent")
    sql_result = state.get("sql_result") or []
    started_at = time.time()

    # 仅对 list intent 且有结果时增强
    if intent != "list" or not sql_result:
        state["semantic_scores"] = {}
        state["vector_only_results"] = []
        _append_execution_history(
            state,
            step="semantic_enhance",
            started_at=started_at,
            status="skipped",
            input_data={"intent": intent, "row_count": len(sql_result)},
            output_data={"reason": "intent!=list or empty result"},
        )
        return state

    question = state["question"]
    logger.start_node("semantic_enhance", {"question": question[:50]})

    try:
        from app.services.vector_search import semantic_enhance

        config = {"search": {"lancedb_dir": "data/lancedb"}}
        semantic_scores, vector_only = semantic_enhance(
            config, sql_result, question, top_k=max(len(sql_result) * 2, 20)
        )

        state["semantic_scores"] = semantic_scores
        state["vector_only_results"] = vector_only

        logger.end_node("semantic_enhance", {
            "matched": len(semantic_scores),
            "vector_only": len(vector_only)
        })
        _append_execution_history(
            state,
            step="semantic_enhance",
            started_at=started_at,
            status="success",
            input_data={"question": question, "row_count": len(sql_result)},
            output_data={"matched": len(semantic_scores), "vector_only": len(vector_only)},
        )

    except Exception as e:
        print(f"[semantic_enhance] 语义增强失败: {e}")
        state["semantic_scores"] = {}
        state["vector_only_results"] = []
        _append_execution_history(
            state,
            step="semantic_enhance",
            started_at=started_at,
            status="error",
            input_data={"question": question, "row_count": len(sql_result)},
            error=str(e),
        )

    return state


# ==================== SQL 修正 ====================

def fix_sql_node(state: AgentState) -> AgentState:
    """SQL 修正节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    state["retry_count"] += 1
    started_at = time.time()
    history_input = {
        "retry_count": state["retry_count"],
        "failed_sql": state.get("sql", ""),
        "error_message": state.get("error_message", ""),
    }
    logger.start_node("fix_sql", {
        "retry": state["retry_count"],
        "sql": state.get("sql", "")[:100]
    })

    try:
        config = {"llm": {"enabled": True}}  # 启用 LLM fallback
        selected_tables = state.get("selected_tables")
        allowed_tables = [t for t in (selected_tables or []) if t]

        from app.services.nl2sql import (
            _has_llm_config,
            _is_plan_sql_precheck_ok,
            _is_plan_sql_valid,
            build_query_plan,
            call_llm_fix_sql,
        )

        question = state["question"]
        failed_sql = state.get("sql", "") or ""
        error_msg = state.get("error_message", "") or "SQL 执行失败"

        plan = None
        if _has_llm_config(config):
            try:
                llm_fixed = call_llm_fix_sql(question, failed_sql, error_msg, config)
                if (
                    _is_plan_sql_valid(llm_fixed, allowed_tables=allowed_tables or None)
                    and _is_plan_sql_precheck_ok(llm_fixed)
                ):
                    plan = llm_fixed
            except Exception as llm_fix_error:
                print(f"[fix_sql] LLM 修复失败，回退规则轨道: {llm_fix_error}")

        if plan is None:
            # 降级到规则引擎重新生成
            plan = build_query_plan(question, config, table_names=selected_tables)

        if not plan.sql:
            raise RuntimeError("SQL 修正后为空")

        state["sql"] = plan.sql
        state["sql_params"] = plan.params
        state["filters"] = plan.filters
        state["error_message"] = None

        logger.end_node("fix_sql", {
            "new_sql": plan.sql[:100],
            "intent": plan.intent,
        })
        _append_execution_history(
            state,
            step="fix_sql",
            started_at=started_at,
            status="success",
            input_data=history_input,
            output_data={"new_sql": plan.sql, "intent": plan.intent},
        )

    except Exception as e:
        state["error_message"] = f"SQL 修正失败: {str(e)}"
        logger.error_node("fix_sql", {"error": str(e)})
        _append_execution_history(
            state,
            step="fix_sql",
            started_at=started_at,
            status="error",
            input_data=history_input,
            error=state["error_message"],
        )

    return state


# ==================== 格式化答案 ====================

def format_answer_node(state: AgentState) -> AgentState:
    """格式化答案节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    question = state["question"]
    intent = state.get("intent", "list")
    started_at = time.time()
    history_input = {
        "intent": intent,
        "has_error": bool(state.get("error_message")),
    }

    logger.start_node("format_answer", {"intent": intent})

    if state.get("error_message"):
        err_msg = state["error_message"]
        filters = state.get("filters") if isinstance(state.get("filters"), dict) else {}
        clarification_needed = bool(filters.get("needs_clarification"))
        clarification_options = filters.get("clarification_options") if isinstance(filters.get("clarification_options"), list) else []
        state["final_answer"] = {
            "type": "error",
            "status": "error",
            "value": [],
            "row_count": 0,
            "message": err_msg,
            "answer_text": err_msg,
            "error": {"message": err_msg},
            "clarification_needed": clarification_needed,
            "clarification_options": clarification_options[:5],
        }
    elif state["intent"] == "chat":
        q = question.strip().lower()
        if any(k in q for k in ["你好", "您好", "hello", "hi"]):
            reply = "你好！我是智能问数助手，可以帮你查询数据、统计分析。"
        elif any(k in q for k in ["你是谁", "你叫什么"]):
            reply = "我是智能问数助手，可以将自然语言转换为 SQL 查询数据库。"
        elif any(k in q for k in ["你能做什么", "帮助"]):
            reply = "我可以帮你查询数据、统计分析。用自然语言提问即可。"
        elif any(k in q for k in ["谢谢"]):
            reply = "不客气，有问题随时问我！"
        elif any(k in q for k in ["再见", "拜拜"]):
            reply = "再见，下次有需要随时找我！"
        else:
            reply = "我是数据查询助手，请提问数据相关问题。"

        state["final_answer"] = {
            "type": "chat",
            "value": reply,
            "message": reply,
        }

    elif state["intent"] == "search":
        if state.get("error_message"):
            state["final_answer"] = {
                "type": "search",
                "value": [],
                "message": f"向量检索失败: {state['error_message']}",
            }
        elif state["sql_result"]:
            state["final_answer"] = {
                "type": "search",
                "value": state["sql_result"],
                "message": f"为您找到 {len(state['sql_result'])} 条相关结果",
            }
        else:
            state["final_answer"] = {
                "type": "search",
                "value": [],
                "message": "未找到相关内容",
            }

    elif state["intent"] == "count":
        if state["sql_result"]:
            first_row = state["sql_result"][0]
            if len(state["sql_result"]) == 1 and len(first_row) == 1:
                count = list(first_row.values())[0]
                state["final_answer"] = {
                    "type": "count",
                    "value": count,
                    "message": f"查询结果：共 {count} 条记录"
                }
            else:
                state["final_answer"] = {
                    "type": "list",
                    "value": state["sql_result"],
                    "message": f"查询结果：返回 {len(state['sql_result'])} 条分组统计"
                }
        else:
            state["final_answer"] = {
                "type": "count",
                "value": 0,
                "message": "查询结果：共 0 条记录"
            }

    else:  # list
        state["final_answer"] = {
            "type": "list",
            "value": state["sql_result"],
            "message": f"查询结果：返回 {len(state['sql_result'])} 条记录",
            "semantic_scores": state.get("semantic_scores") or {},
            "vector_only_results": state.get("vector_only_results") or [],
        }

    # 结构化证据（非向量）：仅对 SQL 类查询注入
    if intent in {"list", "count"} and not state.get("error_message"):
        try:
            from app.services.evidence import build_structured_evidence

            rows = state.get("sql_result") if isinstance(state.get("sql_result"), list) else []
            evidence = build_structured_evidence(
                question=question,
                intent=intent,
                sql=state.get("sql") or "",
                sql_params=state.get("sql_params") or [],
                rows=rows,
                selected_tables=state.get("selected_tables") or [],
            )
            state["final_answer"]["evidence"] = evidence
        except Exception as evidence_error:
            print(f"[evidence] build failed: {evidence_error}")

    # 注入规划元信息，便于前端/审计透出
    if isinstance(state.get("final_answer"), dict):
        filters = state.get("filters") if isinstance(state.get("filters"), dict) else {}
        state["final_answer"].setdefault("plan_source", filters.get("plan_source"))
        state["final_answer"].setdefault("confidence", filters.get("confidence"))
        state["final_answer"].setdefault("clarification_needed", bool(filters.get("needs_clarification")))
        options = filters.get("clarification_options") if isinstance(filters.get("clarification_options"), list) else []
        state["final_answer"].setdefault("clarification_options", options[:5])

    final_status = (
        "error"
        if state.get("error_message") or (state.get("final_answer") or {}).get("status") == "error"
        else "success"
    )
    logger.end_node("format_answer", {
        "status": final_status,
        "message": (state.get("final_answer") or {}).get("message", "")[:80]
    })
    _append_execution_history(
        state,
        step="format_answer",
        started_at=started_at,
        status="error" if final_status == "error" else "success",
        input_data=history_input,
        output_data={
            "final_type": (state.get("final_answer") or {}).get("type"),
            "message": (state.get("final_answer") or {}).get("message", ""),
        },
        error=state.get("error_message") if final_status == "error" else None,
    )

    return state


# ==================== 路由函数 ====================

def should_continue_after_parse(state: AgentState) -> Literal["validate_sql", "format_answer", "vector_search"]:
    """解析后路由"""
    if state.get("error_message"):
        return "format_answer"
    if state.get("intent") == "chat":
        return "format_answer"
    if state.get("intent") == "search":
        return "vector_search"
    return "validate_sql"


def should_continue_after_validate(state: AgentState) -> Literal["execute_sql", "fix_sql", "format_answer"]:
    """验证后路由"""
    if state.get("error_message"):
        if state.get("retry_count", 0) >= state.get("max_retries", 3):
            return "format_answer"
        return "fix_sql"
    return "execute_sql"


def should_continue_after_execute(state: AgentState) -> Literal["semantic_enhance", "format_answer", "fix_sql"]:
    """执行后路由"""
    if state.get("error_message"):
        if state.get("retry_count", 0) >= state.get("max_retries", 3):
            return "format_answer"
        return "fix_sql"
    if state.get("intent") == "list" and state.get("sql_result"):
        return "semantic_enhance"
    return "format_answer"


# ==================== 构建状态图 ====================

def build_graph():
    """构建 LangGraph 状态图"""
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node("parse_question", parse_question_node)
    workflow.add_node("vector_search", vector_search_node)
    workflow.add_node("validate_sql", validate_sql_node)
    workflow.add_node("execute_sql", execute_sql_node)
    workflow.add_node("semantic_enhance", semantic_enhance_node)
    workflow.add_node("fix_sql", fix_sql_node)
    workflow.add_node("format_answer", format_answer_node)

    # 定义边
    workflow.add_edge(START, "parse_question")

    workflow.add_conditional_edges(
        "parse_question",
        should_continue_after_parse,
        {
            "validate_sql": "validate_sql",
            "vector_search": "vector_search",
            "format_answer": "format_answer",
        }
    )

    workflow.add_edge("vector_search", "format_answer")

    workflow.add_conditional_edges(
        "validate_sql",
        should_continue_after_validate,
        {
            "execute_sql": "execute_sql",
            "fix_sql": "fix_sql",
            "format_answer": "format_answer",
        }
    )

    workflow.add_conditional_edges(
        "execute_sql",
        should_continue_after_execute,
        {
            "semantic_enhance": "semantic_enhance",
            "format_answer": "format_answer",
            "fix_sql": "fix_sql"
        }
    )

    workflow.add_edge("semantic_enhance", "format_answer")

    workflow.add_edge("fix_sql", "validate_sql")

    workflow.add_edge("format_answer", END)

    return workflow


class LangGraphOrchestrator:
    """LangGraph 编排器"""

    def __init__(self):
        self.graph = build_graph().compile()

    def stream_events(self, question: str, selected_tables: Optional[List[str]] = None):
        """流式输出"""
        import json
        from datetime import datetime

        run_id = str(uuid.uuid4())[:8]
        logger = _get_logger(run_id)

        initial_state: AgentState = {
            "question": question,
            "intent": None,
            "sql": None,
            "sql_params": None,
            "filters": None,
            "sql_result": None,
            "final_answer": None,
            "error_message": None,
            "retry_count": 0,
            "max_retries": 3,
            "semantic_scores": None,
            "vector_only_results": None,
            "execution_history": [],
            "logs": [],
            "_run_id": run_id,
            "selected_tables": selected_tables
        }

        yield f"data: {json.dumps({'type': 'run_start', 'run_id': run_id, 'question': question[:100], 'ts': datetime.utcnow().isoformat() + 'Z'}, ensure_ascii=False)}\n\n"

        final_state = None
        try:
            last_log_count = 0
            for state_update in self.graph.stream(initial_state):
                for node_name, node_state in state_update.items():
                    final_state = node_state

                    # 输出日志
                    logs = logger.logs
                    for log_entry in logs[last_log_count:]:
                        yield f"data: {json.dumps(log_entry, ensure_ascii=False)}\n\n"
                    last_log_count = len(logs)

                    # 输出节点结束事件
                    outputs = node_state if node_state else {}
                    yield f"data: {json.dumps({'type': 'node_end', 'step': node_name, 'outputs': outputs}, ensure_ascii=False)}\n\n"

            if final_state:
                final_state["logs"] = logger.logs
                final_output = final_state.get("final_answer") or {}
                if isinstance(final_output, dict):
                    final_output = dict(final_output)
                    if final_state.get("sql") and not final_output.get("sql"):
                        final_output["sql"] = final_state.get("sql")
                    if final_state.get("sql_params") is not None and "sql_params" not in final_output:
                        final_output["sql_params"] = final_state.get("sql_params") or []
                _persist_trace(question, final_state, run_id)
                final_meta = {
                    "intent": final_state.get("intent"),
                    "sql": final_state.get("sql"),
                    "sql_params": final_state.get("sql_params") or [],
                    "status": (
                        "error"
                        if final_state.get("error_message")
                        or (final_output or {}).get("status") == "error"
                        else "success"
                    ),
                    "error_message": final_state.get("error_message"),
                    "execution_history": final_state.get("execution_history") or [],
                    "filters": final_state.get("filters") or {},
                }
                yield f"data: {json.dumps({'type': 'final', 'result': final_output, 'meta': final_meta}, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.error(f"图执行失败: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    def run_stream(self, question: str, selected_tables: Optional[List[str]] = None) -> Dict[str, Any]:
        """非流式运行"""
        run_id = str(uuid.uuid4())[:8]
        logger = _get_logger(run_id)

        initial_state: AgentState = {
            "question": question,
            "intent": None,
            "sql": None,
            "sql_params": None,
            "filters": None,
            "sql_result": None,
            "final_answer": None,
            "error_message": None,
            "retry_count": 0,
            "max_retries": 3,
            "semantic_scores": None,
            "vector_only_results": None,
            "execution_history": [],
            "logs": [],
            "_run_id": run_id,
            "selected_tables": selected_tables
        }

        final_state = None
        for state_update in self.graph.stream(initial_state):
            for node_name, node_state in state_update.items():
                final_state = node_state

        result_state = final_state or initial_state
        result_state["logs"] = logger.logs
        _persist_trace(question, result_state, run_id)
        return result_state


def run_stream(question: str, selected_tables: Optional[List[str]] = None) -> Dict[str, Any]:
    """运行流式查询"""
    orchestrator = LangGraphOrchestrator()
    return orchestrator.run_stream(question, selected_tables)
