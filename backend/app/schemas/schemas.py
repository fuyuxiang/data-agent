"""
Pydantic schemas - API 请求/响应模型
"""
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


# ── Workspace ──────────────────────────────────────────────────────────────

class WorkspaceBase(BaseModel):
    name: str
    description: Optional[str] = None


class WorkspaceCreate(WorkspaceBase):
    pass


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class WorkspaceResponse(WorkspaceBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# ── User ───────────────────────────────────────────────────────────────────

class UserBase(BaseModel):
    username: str
    email: Optional[str] = None
    full_name: Optional[str] = None


class UserCreate(UserBase):
    password: str


class UserResponse(UserBase):
    id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class UserLogin(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── DataSource ────────────────────────────────────────────────────────────

class DataSourceBase(BaseModel):
    name: str
    type: str  # mysql, postgresql, sqlserver, csv
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None  # 写入时使用，读取时不返回
    connection_string: Optional[str] = None
    workspace_id: int


class DataSourceCreate(DataSourceBase):
    pass


class DataSourceUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    connection_string: Optional[str] = None
    is_active: Optional[bool] = None


class DataSourceResponse(BaseModel):
    id: int
    workspace_id: int
    name: str
    type: str
    host: Optional[str]
    port: Optional[int]
    database: Optional[str]
    username: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DataSourceTestRequest(BaseModel):
    type: str
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    connection_string: Optional[str] = None


class DataSourceTestResponse(BaseModel):
    success: bool
    message: str
    table_count: Optional[int] = None


# ── CSV File ───────────────────────────────────────────────────────────────

class CSVFileResponse(BaseModel):
    id: int
    data_source_id: int
    filename: str
    file_size: Optional[int]
    row_count: Optional[int]
    column_count: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


# ── Schema / Column ───────────────────────────────────────────────────────

class SchemaColumnResponse(BaseModel):
    table_name: str
    column_name: str
    column_type: Optional[str] = None
    description: Optional[str] = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    is_nullable: bool = True
    sample_values: Optional[List[Any]] = None
    row_count_estimate: Optional[int] = None

    class Config:
        from_attributes = True


class SchemaRefreshResponse(BaseModel):
    success: bool
    table_count: int
    column_count: int
    message: str


# ── Dataset ───────────────────────────────────────────────────────────────

class MetricDefinition(BaseModel):
    """指标定义"""
    name: str
    expression: str  # SQL 表达式
    description: Optional[str] = None
    aggregation_type: str = "sum"  # sum, avg, count, max, min


class DimensionDefinition(BaseModel):
    """维度定义"""
    name: str
    column: str  # 对应列名
    description: Optional[str] = None


class AliasMapping(BaseModel):
    """别名映射"""
    alias: str
    column: str
    description: Optional[str] = None


class DatasetBase(BaseModel):
    name: str
    description: Optional[str] = None


class DatasetCreate(DatasetBase):
    data_source_ids: Optional[List[int]] = None
    data_source_id: Optional[int] = None  # 兼容旧版
    metrics: Optional[List[MetricDefinition]] = None
    dimensions: Optional[List[DimensionDefinition]] = None
    aliases: Optional[List[AliasMapping]] = None
    business_rules: Optional[str] = None
    status: Optional[str] = 'draft'


class DatasetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    data_source_ids: Optional[List[int]] = None
    data_source_id: Optional[int] = None  # 兼容旧版
    metrics: Optional[List[MetricDefinition]] = None
    dimensions: Optional[List[DimensionDefinition]] = None
    aliases: Optional[List[AliasMapping]] = None
    business_rules: Optional[str] = None
    status: Optional[str] = None


class DatasetResponse(DatasetBase):
    id: int
    workspace_id: int
    data_source_id: Optional[int] = None
    data_source_ids: Optional[List[int]] = None
    status: str
    metrics: Optional[List[dict]] = None
    dimensions: Optional[List[dict]] = None
    aliases: Optional[List[dict]] = None
    business_rules: Optional[str] = None
    version: int
    created_at: datetime

    class Config:
        from_attributes = True


# ── Query / NL2SQL ──────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    """NL2SQL 查询请求"""
    question: str
    workspace_id: int
    dataset_id: Optional[int] = None
    table_names: Optional[List[str]] = None  # 用户选择的表名列表
    context: Optional[dict] = None  # 额外上下文


class ExecuteSqlRequest(BaseModel):
    """执行 SQL 请求"""
    sql: str
    workspace_id: int
    dataset_id: Optional[int] = None
    table_names: Optional[List[str]] = None  # 用户选择的表名列表
    sql_params: Optional[List[Any]] = None


class QueryResponse(BaseModel):
    """NL2SQL 查询响应"""
    question: str
    normalized_question: Optional[str] = None
    intent: Optional[str] = None
    matched_dataset: Optional[dict] = None
    sql: Optional[str] = None  # 生成的 SQL
    semantic_sql: Optional[str] = None  # 业务逻辑SQL
    executable_sql: Optional[str] = None  # 可执行SQL
    sql_params: Optional[List[Any]] = None
    reasoning_summary: Optional[str] = None
    result_schema: Optional[List[dict]] = None
    result_rows: Optional[List[dict]] = None
    row_count: int = 0
    chart_suggestion: Optional[str] = None
    cost_time_ms: Optional[float] = None
    cost_rows: Optional[int] = None
    warnings: Optional[List[str]] = None
    status: str
    error: Optional[str] = None
    trace_id: str
    audit_id: str
    agent_steps: Optional[List[dict]] = None  # Agent 思考步骤
    execution_history: Optional[List[dict]] = None  # 完整节点执行历史
    evidence: Optional[dict] = None  # 结构化证据
    answer: Optional[str] = None  # 自然语言答案
    plan_source: Optional[str] = None  # 规划来源：rule/llm/sql_cache/manual_sql/reject
    confidence: Optional[float] = None  # 规划置信度（0~1）
    clarification_needed: Optional[bool] = None  # 是否需要用户澄清
    clarification_options: Optional[List[str]] = None  # 澄清建议候选


class QueryHistoryResponse(BaseModel):
    """查询历史响应"""
    id: int
    question: str
    normalized_question: Optional[str]
    intent: Optional[str]
    semantic_sql: Optional[str]
    executable_sql: Optional[str]
    row_count: int
    execution_time_ms: Optional[float]
    status: str
    trace_id: str
    created_at: datetime

    class Config:
        from_attributes = True


class QueryHistoryCreate(BaseModel):
    """创建查询历史请求"""
    workspace_id: int
    dataset_id: Optional[int] = None
    question: str
    normalized_question: Optional[str] = None
    intent: Optional[str] = None
    semantic_sql: Optional[str] = None
    executable_sql: Optional[str] = None
    sql_params: Optional[List[Any]] = None
    result_schema: Optional[list] = None
    result_rows: Optional[list] = None
    row_count: int = 0
    execution_time_ms: Optional[float] = None
    status: str = "success"
    error_message: Optional[str] = None
    warnings: Optional[list] = None
    trace_id: str
    audit_id: Optional[str] = None


# ── Conversation / Message ────────────────────────────────────────────────

class MessageCreate(BaseModel):
    role: str
    content: str
    metadata: Optional[dict] = None


class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    metadata: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ConversationCreate(BaseModel):
    workspace_id: int
    dataset_id: Optional[int] = None
    title: Optional[str] = None


class ConversationResponse(BaseModel):
    id: int
    workspace_id: int
    user_id: int
    dataset_id: Optional[int]
    title: Optional[str]
    created_at: datetime
    messages: List[MessageResponse] = []

    class Config:
        from_attributes = True
