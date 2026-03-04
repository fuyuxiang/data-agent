"""
API 路由模块
"""
from fastapi import APIRouter

from app.api import auth, workspaces, data_sources, datasets, queries, history

router = APIRouter()

# 注册子路由
router.include_router(auth.router, prefix="/auth", tags=["认证"])
router.include_router(workspaces.router, prefix="/workspaces", tags=["工作空间"])
router.include_router(data_sources.router, prefix="/data-sources", tags=["数据源"])
router.include_router(datasets.router, prefix="/datasets", tags=["数据集"])
router.include_router(queries.router, prefix="/queries", tags=["查询"])
router.include_router(history.router, prefix="/history", tags=["历史记录"])
