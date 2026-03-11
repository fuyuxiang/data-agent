"""
企业级智能问数平台 - 后端入口
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import router as v1
from app.core.config import settings
from app.core.logging import setup_logging
from app.services.trace import init_trace_manager

# 设置日志
setup_logging()

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="企业级智能问数平台 API",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由
app.include_router(v1, prefix="/api/v1")


@app.on_event("startup")
async def setup_trace_system():
    """初始化 Trace 与 SQL 缓存"""
    trace_db_path = Path(settings.TRACE_DB_PATH)
    if not trace_db_path.is_absolute():
        trace_db_path = (Path(__file__).resolve().parent / trace_db_path).resolve()
    trace_db_path.parent.mkdir(parents=True, exist_ok=True)

    trace_log_dir = Path(settings.TRACE_LOG_DIR)
    if not trace_log_dir.is_absolute():
        trace_log_dir = (Path(__file__).resolve().parent / trace_log_dir).resolve()
    trace_log_dir.mkdir(parents=True, exist_ok=True)

    init_trace_manager(
        db_path=trace_db_path,
        enable_file_log=settings.TRACE_FILE_LOG_ENABLED,
        log_dir=trace_log_dir,
    )

# 健康检查
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=50805, reload=True)
