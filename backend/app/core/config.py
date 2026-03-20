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

    # Trace / SQL Cache
    TRACE_DB_PATH: str = "./data/traces.db"
    TRACE_FILE_LOG_ENABLED: bool = True
    TRACE_LOG_DIR: str = "./logs/traces"

    # Media ingestion / processing
    MEDIA_STORAGE_DIR: str = "./data/media"
    MEDIA_QUERY_UPLOAD_DIR: str = "./data/query_uploads"
    MEDIA_TASK_WORKERS: int = 1
    MEDIA_ENABLE_REMOTE_MODELS: bool = True
    MEDIA_IMAGE_MAX_MB: int = 50
    MEDIA_VIDEO_MAX_MB: int = 1024
    VIDEO_SEGMENT_WINDOW_SEC: float = 8.0
    VIDEO_SEGMENT_STRIDE_SEC: float = 4.0

    # Multimodal model endpoints
    VL_BASE_URL: Optional[str] = os.getenv("VL_BASE_URL")
    VL_API_KEY: Optional[str] = os.getenv("VL_API_KEY")
    VL_MODEL: Optional[str] = os.getenv("VL_MODEL")
    EMBEDDING_QWEN_API_URL: Optional[str] = os.getenv("EMBEDDING_QWEN_API_URL")
    RERANKER_API_URL: Optional[str] = os.getenv("RERANKER_API_URL")
    ANNOTATION_STORAGE_DIR: str = "./data/annotations"
    ANNOTATION_TASK_WORKERS: int = 1
    ANNOTATION_YOLO_MODEL_PATH: Optional[str] = os.getenv("ANNOTATION_YOLO_MODEL_PATH")

    # 加密配置
    ENCRYPTION_KEY: Optional[bytes] = None

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
