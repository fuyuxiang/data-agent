"""
Services 模块
"""

from app.services.guardrails import SQLGuardrail, SQLSecurityError
from app.services.trace import (
    QueryTrace,
    TraceStep,
    TraceManager,
    init_trace_manager,
    get_trace_manager,
    normalize_question,
    question_hash,
    scoped_question_hash,
    normalize_table_scope,
)
from app.services.schema_meta import (
    build_schema_prompt,
    build_schema_prompt_cached,
    get_table_schema,
    load_schema_config,
)
from app.services.nl2sql import (
    QueryPlan,
    build_query_plan,
    call_llm_fix_sql,
)
from app.services.vector_search import (
    ModelManager,
    get_model_manager,
    vector_search,
    hybrid_search,
    semantic_enhance,
)
from app.services.evidence import (
    build_structured_evidence,
    extract_tables_from_sql,
)
from app.services.verified_queries import (
    match_verified_query,
)
