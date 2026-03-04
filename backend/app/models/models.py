"""
数据库模型定义
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Enum as SQLEnum,
)
from sqlalchemy.orm import relationship
import enum

from app.core.database import Base


class Workspace(Base):
    """工作空间"""
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    users = relationship("UserWorkspace", back_populates="workspace")
    data_sources = relationship("DataSource", back_populates="workspace")
    datasets = relationship("Dataset", back_populates="workspace")
    conversations = relationship("Conversation", back_populates="workspace")


class User(Base):
    """用户"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    workspaces = relationship("UserWorkspace", back_populates="user")
    conversations = relationship("Conversation", back_populates="user")
    query_histories = relationship("QueryHistory", back_populates="user")


class UserWorkspace(Base):
    """用户-工作空间关联"""
    __tablename__ = "user_workspaces"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    role = Column(String(50), default="member")  # owner, admin, member

    # 关系
    user = relationship("User", back_populates="workspaces")
    workspace = relationship("Workspace", back_populates="users")


class DataSourceType(str, enum.Enum):
    """数据源类型"""
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    SQLSERVER = "sqlserver"
    DUCKDB = "duckdb"
    CSV = "csv"


class DataSource(Base):
    """数据源"""
    __tablename__ = "data_sources"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(255), nullable=False)
    type = Column(SQLEnum(DataSourceType), nullable=False)
    host = Column(String(255), nullable=True)
    port = Column(Integer, nullable=True)
    database = Column(String(255), nullable=True)
    username = Column(String(255), nullable=True)
    # 密码加密存储
    password_encrypted = Column(String(512), nullable=True)
    connection_string = Column(Text, nullable=True)  # CSV 文件路径或其他
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    workspace = relationship("Workspace", back_populates="data_sources")
    schemas = relationship("DataSourceSchema", back_populates="data_source")
    datasets = relationship("Dataset", back_populates="data_source")
    csv_files = relationship("CSVFile", back_populates="data_source")


class CSVFile(Base):
    """CSV 文件 - 属于某个数据源"""
    __tablename__ = "csv_files"

    id = Column(Integer, primary_key=True, index=True)
    data_source_id = Column(Integer, ForeignKey("data_sources.id"), nullable=False)
    filename = Column(String(255), nullable=False)  # 文件名
    file_path = Column(String(512), nullable=False)  # 文件存储路径
    file_size = Column(Integer, nullable=True)  # 文件大小（字节）
    row_count = Column(Integer, nullable=True)  # 行数
    column_count = Column(Integer, nullable=True)  # 列数
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    data_source = relationship("DataSource", back_populates="csv_files")


class DataSourceSchema(Base):
    """数据源 Schema 信息"""
    __tablename__ = "data_source_schemas"

    id = Column(Integer, primary_key=True, index=True)
    data_source_id = Column(Integer, ForeignKey("data_sources.id"), nullable=False)
    table_name = Column(String(255), nullable=False)
    column_name = Column(String(255), nullable=False)
    column_type = Column(String(100), nullable=True)
    description = Column(String(500), nullable=True)
    is_primary_key = Column(Boolean, default=False)
    is_foreign_key = Column(Boolean, default=False)
    is_nullable = Column(Boolean, default=True)
    default_value = Column(String(255), nullable=True)
    sample_values = Column(JSON, nullable=True)  # 示例值
    row_count_estimate = Column(Integer, nullable=True)

    # 关系
    data_source = relationship("DataSource", back_populates="schemas")


class DatasetStatus(str, enum.Enum):
    """数据集状态"""
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class Dataset(Base):
    """数据集 - 语义层定义"""
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    data_source_id = Column(Integer, ForeignKey("data_sources.id"), nullable=True)  # 兼容旧版
    data_source_ids = Column(JSON, nullable=True)  # 多个数据源 ID 列表
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(SQLEnum(DatasetStatus), default=DatasetStatus.DRAFT)

    # 语义层配置
    metrics = Column(JSON, nullable=True)  # 指标定义
    dimensions = Column(JSON, nullable=True)  # 维度定义
    aliases = Column(JSON, nullable=True)  # 别名映射
    business_rules = Column(Text, nullable=True)  # 业务规则说明

    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    workspace = relationship("Workspace", back_populates="datasets")
    data_source = relationship("DataSource", back_populates="datasets")


class Conversation(Base):
    """对话会话"""
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True)
    title = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    workspace = relationship("Workspace", back_populates="conversations")
    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation")


class Message(Base):
    """对话消息"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String(20), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    extra_data = Column(JSON, nullable=True)  # 额外信息

    # 关系
    conversation = relationship("Conversation", back_populates="messages")


class QueryHistory(Base):
    """查询历史"""
    __tablename__ = "query_histories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True)

    # 问题与解析
    question = Column(Text, nullable=False)
    normalized_question = Column(Text, nullable=True)
    intent = Column(String(50), nullable=True)

    # SQL 信息
    semantic_sql = Column(Text, nullable=True)  # 业务逻辑SQL
    executable_sql = Column(Text, nullable=True)  # 可执行SQL
    sql_params = Column(JSON, nullable=True)

    # 执行结果
    result_schema = Column(JSON, nullable=True)
    result_rows = Column(JSON, nullable=True)
    row_count = Column(Integer, default=0)
    execution_time_ms = Column(Float, nullable=True)

    # 状态
    status = Column(String(20), default="pending")  # pending, success, error
    error_message = Column(Text, nullable=True)
    warnings = Column(JSON, nullable=True)

    # 可追溯
    trace_id = Column(String(100), unique=True, index=True)
    audit_id = Column(String(100), unique=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    user = relationship("User", back_populates="query_histories")


class AuditLog(Base):
    """审计日志"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True)
    action = Column(String(100), nullable=False)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(Integer, nullable=True)
    details = Column(JSON, nullable=True)
    ip_address = Column(String(50), nullable=True)
    user_agent = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
