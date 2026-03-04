"""
数据库连接与会话管理
"""
from typing import AsyncGenerator, Optional

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import settings

import os

# 同步引擎（用于迁移）
db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "chatbot.db")
engine = create_engine(
    f"sqlite:///{db_path}",
    connect_args={"check_same_thread": False},
    echo=settings.DEBUG,
)

# 异步引擎
async_engine = create_async_engine(
    f"sqlite+aiosqlite:///{db_path}",
    echo=settings.DEBUG,
)

# 会话工厂
AsyncSessionLocal = sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# 基础模型
Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
