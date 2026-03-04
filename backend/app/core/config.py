"""
核心配置模块
"""
import os
from functools import lru_cache
from typing import List, Optional

from pydantic import BaseModel
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置"""

    # 项目
    PROJECT_NAME: str = "智能问数平台"
    DEBUG: bool = True
    API_V1_STR: str = "/api/v1"

    # 安全
    SECRET_KEY: str = "your-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24小时

    # 数据库
    DATABASE_URL: str = "sqlite:///./data/chatbot.db"

    # Redis (可选)
    REDIS_URL: Optional[str] = None

    # LLM 配置
    LLM_MODEL: Optional[str] = os.getenv("LLM_MODEL")
    LLM_BASE_URL: Optional[str] = os.getenv("LLM_BASE_URL")
    LLM_API_KEY: Optional[str] = os.getenv("LLM_API_KEY")

    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:3000", "*"]

    # 数据源配置
    DEFAULT_SAMPLE_ROWS: int = 5
    MAX_QUERY_ROWS: int = 10000
    QUERY_TIMEOUT_SECONDS: int = 60

    # 加密配置
    ENCRYPTION_KEY: Optional[bytes] = None

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
