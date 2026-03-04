"""
配置管理
"""
import os
from typing import List, Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 项目
    PROJECT_NAME: str = "Data Chat Bot"
    VERSION: str = "2.0.0"

    # LLM 配置（用于 SQL 生成）
    LLM_MODEL: Optional[str] = os.getenv("LLM_MODEL")
    LLM_BASE_URL: Optional[str] = os.getenv("LLM_BASE_URL")
    LLM_API_KEY: Optional[str] = os.getenv("LLM_API_KEY")

    # DuckDB 配置
    DUCKDB_PATH: str = os.getenv("DUCKDB_PATH", ":memory:")
    DUCKDB_MAX_LIMIT: int = 1000

    # SQL Guardrails
    SQL_MAX_LIMIT: int = 1000
    SQL_DEFAULT_LIMIT: int = 10
    ENABLE_SQL_GUARDRAILS: bool = True

    # 语义层 Feature Flag
    SEMANTIC_LAYER_ENABLED: bool = False
    SEMANTIC_LAYER_URL: Optional[str] = None

    # 允许的表（白名单）
    ALLOWED_TABLES: List[str] = []

    # 重试配置
    MAX_RETRIES: int = 3

    class Config:
        env_file = ".env"


settings = Settings()
