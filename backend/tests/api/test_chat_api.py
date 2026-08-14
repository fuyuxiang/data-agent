"""Chat HTTP API: ask, list conversations, list turns, feedback."""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_meta_session, get_sample_connection
from app.intent.recognizer import LlmCompletion
from app.main import app
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


def _payload(**overrides) -> str:
    base = {
        "kind": "aggregate",
        "metrics": ["sales_revenue"],
        "dimensions": [],
        "filters": [],
        "time": {
            "start": "2026-08-01",
            "end": "2026-08-31",
            "grain": "month",
            "expression": "本月",
        },
        "comparison": "none",
        "confidence": {"overall": 0.92, "metric": 0.95, "time": 0.93},
        "assumptions": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


class StubClient:
    payloads_default = _payload()

    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)
        self.last_user_prompt = ""

    def complete(self, system: str, user: str):
        self.last_user_prompt = user
        content = self.payloads.pop(0) if self.payloads else self.payloads_default
        return LlmCompletion(
            content=content, model="stub", prompt_tokens=90, completion_tokens=15
        )


@pytest.fixture
def stub():
    return StubClient()


@pytest.fixture
def client(meta_session, sample_conn, stub):
    from app.api import chat

    build_orders_dataset(meta_session)
    build_principals(meta_session)
    app.dependency_overrides[get_meta_session] = lambda: meta_session
    app.dependency_overrides[get_sample_connection] = lambda: sample_conn
    app.dependency_overrides[chat.get_llm_client] = lambda: stub
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ask(client, question="本月销售额", username="admin", **extra):
    body = {"question": question, "dataset_name": "orders", **extra}
    return client.post("/api/chat/ask", json=body, headers={"X-Username": username})


def test_ask_returns_answer_with_citation(client):
    response = _ask(client)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["answer"]["citation"]["metric"].startswith("sales_revenue v3")


def test_ask_without_identity_is_401(client):
    response = client.post(
        "/api/chat/ask", json={"question": "本月销售额", "dataset_name": "orders"}
    )
    assert response.status_code == 401


def test_ask_returns_turn_and_conversation_ids(client):
    body = _ask(client).json()

    assert body["turn_id"] > 0
    assert body["conversation_id"] > 0


def test_permission_denied_answers_200_with_refused_status(client, stub):
    """A refusal is a normal turn, not an HTTP error: it must be traceable."""
    stub.payloads.append(_payload(metrics=["total_cost"]))
    body = _ask(client, username="east_manager").json()

    assert body["status"] == "refused"
    assert body["answer"] is None
    assert "权限" in body["refusal_reason"]


def test_refusal_response_leaks_no_metadata(client, stub):
    stub.payloads.append(_payload(metrics=["total_cost"]))
    raw = _ask(client, username="east_manager").text

    for leak in ("total_cost", "sample.orders", "region_code"):
        assert leak not in raw


def test_clarification_is_returned_with_options(client, stub):
    stub.payloads.append(_payload(confidence={"overall": 0.4, "metric": 0.3, "time": 0.9}))
    body = _ask(client, question="业绩怎么样").json()

    assert body["status"] == "clarifying"
    assert body["clarifications"][0]["options"]


def test_followup_continues_the_same_conversation(client):
    first = _ask(client).json()
    second = _ask(
        client, question="那按省份看", conversation_id=first["conversation_id"]
    ).json()

    assert second["conversation_id"] == first["conversation_id"]


def test_slot_state_is_returned_for_the_condition_panel(client):
    body = _ask(client).json()

    assert body["slot_state"]["metrics"] == ["sales_revenue"]
    assert body["slot_state"]["time"]["start"] == "2026-08-01"


def test_answer_carries_row_data_for_the_result_pane(client):
    body = _ask(client).json()

    assert body["answer"]["columns"]
    assert body["answer"]["rows"]


def test_unknown_dataset_returns_404(client):
    response = client.post(
        "/api/chat/ask",
        json={"question": "本月销售额", "dataset_name": "ghost"},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 404


def test_conversation_list_is_scoped_to_the_caller(client):
    _ask(client, username="admin")
    _ask(client, username="analyst")

    mine = client.get("/api/chat/conversations", headers={"X-Username": "analyst"}).json()

    assert len(mine) == 1
    assert mine[0]["title"]


def test_turns_of_a_conversation_are_ordered(client):
    first = _ask(client).json()
    _ask(client, question="那按省份看", conversation_id=first["conversation_id"])

    turns = client.get(
        f"/api/chat/conversations/{first['conversation_id']}/turns",
        headers={"X-Username": "admin"},
    ).json()

    assert [item["question"] for item in turns] == ["本月销售额", "那按省份看"]


def test_other_users_conversation_turns_are_404(client):
    mine = _ask(client, username="admin").json()

    response = client.get(
        f"/api/chat/conversations/{mine['conversation_id']}/turns",
        headers={"X-Username": "analyst"},
    )
    assert response.status_code == 404


def test_negative_feedback_requires_a_category(client):
    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": False},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 422


def test_negative_feedback_with_attribution_is_stored(client, meta_session):
    from app.observability.orm import FeedbackRow

    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": False, "category": "metric", "comment": "口径不对"},
        headers={"X-Username": "admin"},
    )

    assert response.status_code == 201
    stored = meta_session.query(FeedbackRow).filter_by(turn_id=turn_id).one()
    assert stored.category == "metric"


def test_positive_feedback_needs_no_category(client):
    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": True},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 201


def test_unknown_feedback_category_is_rejected(client):
    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": False, "category": "vibes"},
        headers={"X-Username": "admin"},
    )
    assert response.status_code == 422


# --- Authentication Tests (S8 Phase 0) ---


def test_list_conversations_without_identity_is_401(client):
    """GET /api/chat/conversations requires X-Username header."""
    response = client.get("/api/chat/conversations")
    assert response.status_code == 401


def test_list_turns_without_identity_is_401(client):
    """GET /api/chat/conversations/{id}/turns requires X-Username header."""
    # Create a conversation first to get a valid ID
    conv_id = _ask(client).json()["conversation_id"]

    response = client.get(f"/api/chat/conversations/{conv_id}/turns")
    assert response.status_code == 401


def test_feedback_without_identity_is_401(client):
    """POST /api/chat/turns/{id}/feedback requires X-Username header."""
    turn_id = _ask(client).json()["turn_id"]

    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": True},
    )
    assert response.status_code == 401


def test_feedback_for_other_users_turn_is_404(client):
    """A user cannot give feedback on another user's turn."""
    # Admin creates a turn
    turn_id = _ask(client, username="admin").json()["turn_id"]

    # Analyst tries to give feedback on admin's turn
    response = client.post(
        f"/api/chat/turns/{turn_id}/feedback",
        json={"is_positive": True},
        headers={"X-Username": "analyst"},
    )
    assert response.status_code == 404