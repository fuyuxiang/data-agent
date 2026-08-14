"""Trace HTTP API: list stages, replay turn."""

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_meta_session, get_sample_connection
from app.main import app
from tests.api.test_chat_api import StubClient, _payload
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def stub():
    return StubClient()


@pytest.fixture
def client(meta_session, sample_conn, stub):
    from sqlalchemy import select

    from app.api import chat
    from app.security.orm import RoleRow, UserRow
    from scripts.seed_roles import seed_roles

    seed_roles(meta_session)
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    # Grant trace_auditor to both `admin` and `east_manager` so the replay
    # tests can assert the SQL body without standing up a separate audit
    # identity. The "ordinary owners see no SQL" rule is covered by
    # test_replay_hides_sql_from_ordinary_owners, which uses analyst (no
    # role grants beyond the build).
    auditor = meta_session.execute(
        select(RoleRow).where(RoleRow.name == "trace_auditor")
    ).scalar_one()
    for username in ("admin", "east_manager"):
        user = meta_session.execute(
            select(UserRow).where(UserRow.username == username)
        ).scalar_one()
        if auditor not in user.roles:
            user.roles.append(auditor)
            meta_session.flush()

    app.dependency_overrides[get_meta_session] = lambda: meta_session
    app.dependency_overrides[get_sample_connection] = lambda: sample_conn
    app.dependency_overrides[chat.get_llm_client] = lambda: stub
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ask(client, username="admin", question="本月销售额"):
    return client.post(
        "/api/chat/ask",
        json={"question": question, "dataset_name": "orders"},
        headers={"X-Username": username},
    ).json()


def test_trace_lists_all_seven_stages(client):
    turn_id = _ask(client)["turn_id"]

    body = client.get(f"/api/trace/turns/{turn_id}", headers={"X-Username": "admin"}).json()

    assert [item["stage"] for item in body["stages"]] == [
        "verified_recall",
        "intent",
        "semantic_resolve",
        "compile",
        "security",
        "execute",
        "answer",
    ]


def test_trace_exposes_sql_and_tokens(client):
    turn_id = _ask(client)["turn_id"]

    stages = client.get(
        f"/api/trace/turns/{turn_id}", headers={"X-Username": "admin"}
    ).json()["stages"]

    intent = next(item for item in stages if item["stage"] == "intent")
    security = next(item for item in stages if item["stage"] == "security")
    assert intent["prompt_tokens"] == 90
    assert "SELECT" in security["output_payload"]["sql"].upper()


def test_trace_reports_elapsed_per_stage(client):
    turn_id = _ask(client)["turn_id"]

    stages = client.get(
        f"/api/trace/turns/{turn_id}", headers={"X-Username": "admin"}
    ).json()["stages"]
    assert all(item["elapsed_ms"] >= 0 for item in stages)


def test_trace_header_carries_question_and_status(client):
    turn_id = _ask(client)["turn_id"]

    body = client.get(f"/api/trace/turns/{turn_id}", headers={"X-Username": "admin"}).json()

    assert body["question"] == "本月销售额"
    assert body["status"] == "answered"


def test_trace_of_another_users_turn_is_404(client):
    turn_id = _ask(client, username="admin")["turn_id"]

    response = client.get(f"/api/trace/turns/{turn_id}", headers={"X-Username": "analyst"})
    assert response.status_code == 404


def test_unknown_turn_is_404(client):
    response = client.get("/api/trace/turns/999999", headers={"X-Username": "admin"})
    assert response.status_code == 404


def test_replay_recompiles_from_the_intent_snapshot(client):
    turn_id = _ask(client)["turn_id"]

    body = client.post(
        f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "admin"}
    ).json()

    assert "SELECT" in body["sql"].upper()
    assert body["matches_original"] is True


def test_replay_does_not_call_the_model(client, stub):
    turn_id = _ask(client)["turn_id"]
    before = stub.last_user_prompt

    client.post(f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "admin"})

    assert stub.last_user_prompt == before


def test_replay_applies_the_current_permissions(client, stub):
    """Replay is a diagnosis tool, not a permission bypass: rewriting happens
    again with the *current* permissions. `east_manager` is owner + auditor
    so it can both see and assert the rewritten SQL."""
    turn_id = _ask(client, username="east_manager")["turn_id"]

    body = client.post(
        f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "east_manager"}
    ).json()

    assert "'EC'" in body["sql"]


def test_replay_hides_sql_from_ordinary_owners(client):
    """A non-audit caller must see `matches_original` and `applied_row_filters`
    but not the physical SQL itself."""
    turn_id = _ask(client, username="analyst")["turn_id"]

    body = client.post(
        f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "analyst"}
    ).json()

    assert body["sql"] is None
    assert body["display_sql"] is None
    assert body["matches_original"] is True


def test_replay_of_a_turn_without_snapshot_is_409(client, stub):
    stub.payloads.append(_payload(kind="unsupported", metrics=[]))
    turn_id = _ask(client, question="帮我改一下这单")["turn_id"]

    response = client.post(
        f"/api/trace/turns/{turn_id}/replay", headers={"X-Username": "admin"}
    )
    assert response.status_code == 409


# --- Authentication Tests (S8 Phase 0) ---


def test_get_trace_without_identity_is_401(client):
    """GET /api/trace/turns/{id} requires X-Username header."""
    turn_id = _ask(client)["turn_id"]

    response = client.get(f"/api/trace/turns/{turn_id}")
    assert response.status_code == 401


def test_replay_without_identity_is_401(client):
    """POST /api/trace/turns/{id}/replay requires X-Username header."""
    turn_id = _ask(client)["turn_id"]

    response = client.post(f"/api/trace/turns/{turn_id}/replay")
    assert response.status_code == 401


def test_replay_of_other_users_turn_is_404(client):
    """A user cannot replay another user's turn."""
    turn_id = _ask(client, username="admin")["turn_id"]

    response = client.post(
        f"/api/trace/turns/{turn_id}/replay",
        headers={"X-Username": "analyst"},
    )
    assert response.status_code == 404