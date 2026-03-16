from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

import app.core.database as db_module
import app.services.media_processing as media_processing_module
import app.services.media_tasks as media_tasks_module
import app.services.media_utils as media_utils_module
import app.services.nl2multimodal as nl2multimodal_module
from app.api.auth import get_current_user
from app.core.database import Base, get_db
from app.models.models import User, UserWorkspace, Workspace
from main import app
from scripts.migrate_media_schema import migrate_media_schema


@pytest.fixture()
def test_context(tmp_path, monkeypatch):
    db_file = tmp_path / "test_chatbot.db"
    sync_engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        echo=False,
    )
    async_session_maker = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    media_root = tmp_path / "media"
    query_root = tmp_path / "query"
    for path in (
        media_root,
        media_root / "uploads",
        media_root / "derived",
        media_root / "derived" / "keyframes",
        query_root,
    ):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(db_module, "engine", sync_engine)
    monkeypatch.setattr(db_module, "async_engine", async_engine)
    monkeypatch.setattr(db_module, "AsyncSessionLocal", async_session_maker)
    monkeypatch.setattr(media_tasks_module, "AsyncSessionLocal", async_session_maker)
    monkeypatch.setattr(nl2multimodal_module, "engine", sync_engine)
    monkeypatch.setattr(media_utils_module, "MEDIA_ROOT", media_root)
    monkeypatch.setattr(media_utils_module, "MEDIA_UPLOAD_ROOT", media_root / "uploads")
    monkeypatch.setattr(media_utils_module, "MEDIA_DERIVED_ROOT", media_root / "derived")
    monkeypatch.setattr(media_utils_module, "MEDIA_KEYFRAME_ROOT", media_root / "derived" / "keyframes")
    monkeypatch.setattr(media_utils_module, "QUERY_UPLOAD_ROOT", query_root)
    monkeypatch.setattr(media_processing_module, "MEDIA_KEYFRAME_ROOT", media_root / "derived" / "keyframes")

    queued_dataset_ids: list[int] = []

    async def noop_startup():
        return None

    async def noop_shutdown():
        return None

    async def fake_enqueue(dataset_id: int):
        queued_dataset_ids.append(dataset_id)

    monkeypatch.setattr(media_tasks_module.media_task_manager, "startup", noop_startup)
    monkeypatch.setattr(media_tasks_module.media_task_manager, "shutdown", noop_shutdown)
    monkeypatch.setattr(media_tasks_module.media_task_manager, "enqueue_dataset", fake_enqueue)

    Base.metadata.create_all(sync_engine)
    migrate_media_schema(sync_engine)

    with Session(sync_engine) as session:
        workspace = Workspace(name="测试工作空间", description="test")
        user = User(
            username="tester",
            email="tester@example.com",
            hashed_password="not-used",
            is_active=True,
        )
        session.add_all([workspace, user])
        session.flush()
        session.add(UserWorkspace(user_id=user.id, workspace_id=workspace.id, role="owner"))
        session.commit()
        workspace_id = workspace.id
        user_id = user.id

    async def override_get_db():
        async with async_session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def override_get_current_user():
        return User(id=user_id, username="tester", hashed_password="not-used", is_active=True)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    yield {
        "sync_engine": sync_engine,
        "async_session_maker": async_session_maker,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "queued_dataset_ids": queued_dataset_ids,
        "tmp_path": tmp_path,
    }

    app.dependency_overrides.clear()
    asyncio.run(async_engine.dispose())
    sync_engine.dispose()


@pytest.fixture()
def client(test_context):
    with TestClient(app) as test_client:
        yield test_client
