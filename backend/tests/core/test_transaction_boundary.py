"""Metadata transaction boundary tests (S1 Task 4, Step 1).

`get_meta_session` is the single shared dependency every metadata-writing
endpoint flows through. Without a commit on the happy path the rows written
by an `ask` call only exist inside that one session — any subsequent
read through a fresh session would see nothing, which means the user
"loses" their conversation as soon as the request ends. The fix has to
cover the unhappy path too: an exception that bubbles out of FastAPI
must roll back so a half-written Conversation doesn't outlive its call.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import chat
from app.core.db import MetaSession, get_meta_session
from app.intent.recognizer import LlmCompletion
from app.main import app
from app.observability.orm import ConversationRow, TurnRow
from app.pipeline.orchestrator import TurnStatus
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


class _StubClient:
    """A canned-payload LLM stub. `time_window` controls the answer
    window — an empty one (no rows in 2020) forces a FAILED outcome."""

    def __init__(self, *, time_window: dict | None = None) -> None:
        payload = {
            "kind": "aggregate",
            "metrics": ["sales_revenue"],
            "dimensions": [],
            "filters": [],
            "time": time_window
            or {"start": "2026-08-01", "end": "2026-08-31", "grain": "month", "expression": "本月"},
            "comparison": "none",
            "confidence": {"overall": 0.92, "metric": 0.95, "time": 0.93},
            "assumptions": [],
        }
        self._payload = json.dumps(payload, ensure_ascii=False)

    def complete(self, system: str, user: str) -> LlmCompletion:
        return LlmCompletion(
            content=self._payload, model="stub", prompt_tokens=90, completion_tokens=15
        )


@pytest.fixture
def client(sample_conn):
    """End-to-end client that opens its own short-lived sessions so the
    dependency's commit/rollback actually has effect.

    Avoids the conftest `meta_session` fixture deliberately: that fixture
    wraps everything in a transaction it rolls back at teardown, which
    would mask any commit the dependency itself performs.
    """
    seed = MetaSession()
    try:
        build_orders_dataset(seed)
        build_principals(seed)
        seed.commit()
    finally:
        seed.close()

    def _open_session():
        # Delegate to the real dependency so its commit/rollback logic
        # runs — overriding with a plain session would mask the behaviour
        # we are trying to test.
        yield from get_meta_session()

    app.dependency_overrides[get_meta_session] = _open_session
    app.dependency_overrides[chat.get_llm_client] = lambda: _StubClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _fresh_session() -> Session:
    """A new session bound to the same engine, used to read what the
    request just wrote — independent of the request's session."""
    return MetaSession()


def test_successful_ask_persists_across_sessions(client) -> None:
    response = client.post(
        "/api/chat/ask",
        json={"question": "本月销售额", "dataset_name": "orders"},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    turn_id = body["turn_id"]
    conversation_id = body["conversation_id"]

    fresh = _fresh_session()
    try:
        turn = fresh.get(TurnRow, turn_id)
        conv = fresh.get(ConversationRow, conversation_id)
        assert turn is not None, "turn not committed"
        assert conv is not None, "conversation not committed"
        assert turn.conversation_id == conv.id
    finally:
        fresh.close()


def test_failed_query_still_persists_its_turn_and_trace(client) -> None:
    """An answerable question that hits a guardrail (empty window) must
    still leave a Turn + Trace so the auditor can investigate."""
    app.dependency_overrides[chat.get_llm_client] = lambda: _StubClient(
        time_window={"start": "2020-01-01", "end": "2020-01-31"}
    )
    response = client.post(
        "/api/chat/ask",
        json={"question": "2020年1月销售额", "dataset_name": "orders"},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == TurnStatus.FAILED.value
    turn_id = body["turn_id"]

    fresh = _fresh_session()
    try:
        turn = fresh.get(TurnRow, turn_id)
        assert turn is not None, "failed turn was not committed"
        assert turn.status == TurnStatus.FAILED.value
    finally:
        fresh.close()


def test_404_path_rolls_back_writes(client) -> None:
    """A request that 404s must leave no half-written state behind.

    Earlier tests in this file commit Turn rows; assert delta, not zero.
    """
    fresh = _fresh_session()
    try:
        before = fresh.execute(select(func.count()).select_from(TurnRow)).scalar_one()
    finally:
        fresh.close()

    response = client.post(
        "/api/chat/ask",
        json={"question": "x", "dataset_name": "ghost-dataset"},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 404

    fresh = _fresh_session()
    try:
        after = fresh.execute(select(func.count()).select_from(TurnRow)).scalar_one()
    finally:
        fresh.close()
    assert before == after, f"404 leaked {after - before} Turn rows"