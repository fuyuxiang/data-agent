"""
LangGraph 编排器 - 增强版

状态流转：
START → PARSE_QUESTION → VALIDATE_SQL → EXECUTE_SQL → SEMANTIC_ENHANCE → FORMAT_ANSWER → END
                ↓ (search intent)                       (list only)
            VECTOR_SEARCH → FORMAT_ANSWER → END
                                ↓ (validation/execution failed)
                            FIX_SQL (自我修正, max retries → ERROR)
"""

import uuid
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


def parse_question_node(state: AgentState) -> AgentState:
    """解析用户问题节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    question = state["question"]
    logger.start_node("parse_question", {"question": question[:100]})

    try:
        from app.services.nl2sql import build_query_plan
        from app.orchestrator_duckdb import get_engine

        # 获取可用表名
        selected_tables = state.get("selected_tables")
        if not selected_tables:
            # 如果没有指定表，从 DuckDB 获取第一个可用表
            engine = get_engine()
            engine.connect()
            tables = engine.get_tables()
            if tables:
                selected_tables = [tables[0]]
                print(f"[parse_question] 未指定表，使用默认表: {selected_tables}")

        config = {"llm": {"enabled": True}}  # 启用 LLM
        plan = build_query_plan(question, config, table_names=selected_tables)

        state["intent"] = plan.intent
        state["sql"] = plan.sql
        state["sql_params"] = plan.params
        state["filters"] = plan.filters

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
            return state

        logger.end_node("parse_question", {
            "intent": plan.intent,
            "sql": plan.sql[:200] if plan.sql else "",
            "selected_table": (plan.filters or {}).get("selected_table"),
            "candidate_tables": (plan.filters or {}).get("candidate_tables", []),
            "plan_source": (plan.filters or {}).get("plan_source", "rule"),
            "semantic_judge": (plan.filters or {}).get("semantic_judge", {}),
        })

    except Exception as e:
        state["error_message"] = f"问题解析失败: {str(e)}"
        logger.error_node("parse_question", {"error": str(e)})

    return state


# ==================== 向量检索 ====================

def vector_search_node(state: AgentState) -> AgentState:
    """向量检索节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    question = state["question"]
    filters = state.get("filters") or {}
    top_k = filters.get("top_k", 10)

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

    except Exception as e:
        state["error_message"] = f"向量检索失败: {str(e)}"
        logger.error_node("vector_search", {"error": str(e)})

    return state


# ==================== SQL 验证 ====================

def validate_sql_node(state: AgentState) -> AgentState:
    """SQL 验证节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    sql = state.get("sql", "")
    selected_tables = state.get("selected_tables") or []
    allowed_tables = [t for t in selected_tables if t]
    logger.start_node("validate_sql", {"sql": sql[:100]})

    try:
        SQLGuardrail.validate_sql(sql, allowed_tables=allowed_tables if allowed_tables else None)
        logger.end_node("validate_sql", {"valid": True, "allowed_tables": allowed_tables[:5]})
    except Exception as e:
        state["error_message"] = f"SQL 验证失败: {str(e)}"
        logger.error_node("validate_sql", {"error": str(e)})

    return state


# ==================== SQL 执行 ====================

def execute_sql_node(state: AgentState) -> AgentState:
    """SQL 执行节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    sql = state.get("sql", "")
    params = state.get("sql_params", [])

    logger.start_node("execute_sql", {"sql": sql[:100]})

    try:
        engine = get_engine()
        engine.connect()
        result = engine.execute(sql, params)

        if result.get("error"):
            state["error_message"] = result["error"]
            logger.error_node("execute_sql", {"error": result["error"]})
        else:
            state["sql_result"] = result.get("rows", [])
            state["error_message"] = None
            logger.end_node("execute_sql", {
                "row_count": len(state["sql_result"])
            })

    except Exception as e:
        state["error_message"] = f"SQL 执行失败: {str(e)}"
        logger.error_node("execute_sql", {"error": str(e)})

    return state


# ==================== 语义增强 ====================

def semantic_enhance_node(state: AgentState) -> AgentState:
    """语义增强节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    intent = state.get("intent")
    sql_result = state.get("sql_result") or []

    # 仅对 list intent 且有结果时增强
    if intent != "list" or not sql_result:
        state["semantic_scores"] = {}
        state["vector_only_results"] = []
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

    except Exception as e:
        print(f"[semantic_enhance] 语义增强失败: {e}")
        state["semantic_scores"] = {}
        state["vector_only_results"] = []

    return state


# ==================== SQL 修正 ====================

def fix_sql_node(state: AgentState) -> AgentState:
    """SQL 修正节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    state["retry_count"] += 1
    logger.start_node("fix_sql", {
        "retry": state["retry_count"],
        "sql": state.get("sql", "")[:100]
    })

    try:
        config = {"llm": {"enabled": True}}  # 启用 LLM fallback

        # 降级到规则引擎重新生成
        from app.services.nl2sql import build_query_plan
        selected_tables = state.get("selected_tables")
        plan = build_query_plan(state["question"], config, table_names=selected_tables)

        state["sql"] = plan.sql
        state["sql_params"] = plan.params
        state["error_message"] = None

        logger.end_node("fix_sql", {
            "new_sql": plan.sql[:100]
        })

    except Exception as e:
        state["error_message"] = f"SQL 修正失败: {str(e)}"
        logger.error_node("fix_sql", {"error": str(e)})

    return state


# ==================== 格式化答案 ====================

def format_answer_node(state: AgentState) -> AgentState:
    """格式化答案节点"""
    run_id = state.get("_run_id", "default")
    logger = _get_logger(run_id)
    question = state["question"]
    intent = state.get("intent", "list")

    logger.start_node("format_answer", {"intent": intent})

    if state.get("error_message"):
        err_msg = state["error_message"]
        state["final_answer"] = {
            "type": "error",
            "status": "error",
            "value": [],
            "row_count": 0,
            "message": err_msg,
            "answer_text": err_msg,
            "error": {"message": err_msg},
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

    final_status = "error" if (state.get("final_answer") or {}).get("status") == "error" else "success"
    logger.end_node("format_answer", {
        "status": final_status,
        "message": (state.get("final_answer") or {}).get("message", "")[:80]
    })

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


def should_continue_after_validate(state: AgentState) -> Literal["execute_sql", "fix_sql"]:
    """验证后路由"""
    if state.get("error_message"):
        return "fix_sql"
    return "execute_sql"


def should_continue_after_execute(state: AgentState) -> Literal["semantic_enhance", "format_answer", "fix_sql"]:
    """执行后路由"""
    if state.get("error_message"):
        return "fix_sql"
    if state.get("intent") == "list" and state.get("sql_result"):
        return "semantic_enhance"
    return "format_answer"


def should_retry(state: AgentState) -> Literal["fix_sql", "format_answer"]:
    """重试路由"""
    if state["retry_count"] < state.get("max_retries", 3):
        return "fix_sql"
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
            "fix_sql": "fix_sql"
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

    workflow.add_conditional_edges(
        "fix_sql",
        should_retry,
        {
            "fix_sql": "validate_sql",
            "format_answer": "format_answer",
        }
    )

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
                final_output = final_state.get("final_answer") or {}
                yield f"data: {json.dumps({'type': 'final', 'result': final_output}, ensure_ascii=False)}\n\n"

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

        return final_state or initial_state


def run_stream(question: str, selected_tables: Optional[List[str]] = None) -> Dict[str, Any]:
    """运行流式查询"""
    orchestrator = LangGraphOrchestrator()
    return orchestrator.run_stream(question, selected_tables)
