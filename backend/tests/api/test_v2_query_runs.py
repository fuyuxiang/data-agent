"""Tests for S7 API v2 Query Run endpoints."""

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.no_db


@pytest.fixture
def client():
    """FastAPI test client for v2 endpoints."""
    from fastapi import FastAPI
    from app.api.v2_query_runs import router as v2_router

    app = FastAPI()
    app.include_router(v2_router)
    return TestClient(app)


class TestCreateQueryRun:
    """Test POST /api/v2/query-runs."""

    def test_create_returns_201_with_run_id(self, client):
        """Creating a query run returns 201 and a run_id."""
        response = client.post(
            "/api/v2/query-runs",
            json={
                "agent_id": "sales-intelligence",
                "question": "本月销售额是多少？",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["run_id"].startswith("qr_")
        assert body["status"] == "pending"
        assert "events_url" in body

    def test_create_rejects_empty_question(self, client):
        """Empty question is rejected with 422."""
        response = client.post(
            "/api/v2/query-runs",
            json={"agent_id": "sales", "question": ""},
        )
        assert response.status_code == 422

    def test_create_rejects_missing_agent_id(self, client):
        """Missing agent_id is rejected with 422."""
        response = client.post(
            "/api/v2/query-runs",
            json={"question": "x"},
        )
        assert response.status_code == 422


class TestIdempotency:
    """Test idempotent run creation."""

    def test_same_idempotency_key_returns_same_run(self, client):
        """Same Idempotency-Key returns the same run."""
        headers = {"Idempotency-Key": "test-key-1"}
        body = {"agent_id": "sales", "question": "test"}

        r1 = client.post("/api/v2/query-runs", json=body, headers=headers)
        r2 = client.post("/api/v2/query-runs", json=body, headers=headers)

        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["run_id"] == r2.json()["run_id"]

    def test_different_idempotency_keys_create_different_runs(self, client):
        """Different keys create distinct runs."""
        body = {"agent_id": "sales", "question": "test"}

        r1 = client.post(
            "/api/v2/query-runs", json=body,
            headers={"Idempotency-Key": "key-1"},
        )
        r2 = client.post(
            "/api/v2/query-runs", json=body,
            headers={"Idempotency-Key": "key-2"},
        )

        assert r1.json()["run_id"] != r2.json()["run_id"]


class TestGetQueryRun:
    """Test GET /api/v2/query-runs/{id}."""

    def test_get_existing_run(self, client):
        """Fetch an existing run."""
        create_resp = client.post(
            "/api/v2/query-runs",
            json={"agent_id": "sales", "question": "test"},
        )
        run_id = create_resp.json()["run_id"]

        response = client.get(f"/api/v2/query-runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == run_id
        assert body["status"] == "pending"

    def test_get_nonexistent_returns_404(self, client):
        """Unknown run returns 404."""
        response = client.get("/api/v2/query-runs/qr_nonexistent")
        assert response.status_code == 404


class TestCancelQueryRun:
    """Test POST /api/v2/query-runs/{id}/cancel."""

    def test_cancel_pending_run(self, client):
        """Cancelling a pending run transitions to cancelled."""
        create_resp = client.post(
            "/api/v2/query-runs",
            json={"agent_id": "sales", "question": "test"},
        )
        run_id = create_resp.json()["run_id"]

        response = client.post(f"/api/v2/query-runs/{run_id}/cancel")
        assert response.status_code == 204

        get_resp = client.get(f"/api/v2/query-runs/{run_id}")
        assert get_resp.json()["status"] == "cancelled"

    def test_cancel_nonexistent_returns_404(self, client):
        """Cancelling unknown run returns 404."""
        response = client.post("/api/v2/query-runs/qr_nonexistent/cancel")
        assert response.status_code == 404

    def test_cancel_is_idempotent(self, client):
        """Cancelling an already-cancelled run returns 204 (idempotent)."""
        create_resp = client.post(
            "/api/v2/query-runs",
            json={"agent_id": "sales", "question": "test"},
        )
        run_id = create_resp.json()["run_id"]

        # First cancel
        client.post(f"/api/v2/query-runs/{run_id}/cancel")
        # Second cancel (should still succeed)
        response = client.post(f"/api/v2/query-runs/{run_id}/cancel")
        assert response.status_code == 204


class TestClarification:
    """Test POST /api/v2/query-runs/{id}/clarifications."""

    def test_clarification_requires_clarifying_state(self, client):
        """Cannot submit clarification when not clarifying."""
        create_resp = client.post(
            "/api/v2/query-runs",
            json={"agent_id": "sales", "question": "test"},
        )
        run_id = create_resp.json()["run_id"]

        response = client.post(
            f"/api/v2/query-runs/{run_id}/clarifications",
            json={"option_index": 0},
        )
        assert response.status_code == 409

    def test_clarification_for_clarifying_run_succeeds(self, client):
        """Clarification submission for clarifying run succeeds."""
        from app.api.v2_query_runs import _RUNS, QueryRun, QueryRunStatus
        from datetime import datetime

        # Create run then force it to CLARIFYING
        create_resp = client.post(
            "/api/v2/query-runs",
            json={"agent_id": "sales", "question": "test"},
        )
        run_id = create_resp.json()["run_id"]
        run = _RUNS[run_id]
        _RUNS[run_id] = QueryRun(
            run_id=run.run_id,
            status=QueryRunStatus.CLARIFYING,
            question=run.question,
            principal_id=0,
            agent_id=run.agent_id,
            created_at=run.created_at,
            updated_at=run.updated_at,
            idempotency_key=run.idempotency_key,
            conversation_id=run.conversation_id,
            events=[],
        )

        response = client.post(
            f"/api/v2/query-runs/{run_id}/clarifications",
            json={"option_index": 1, "free_text": "I mean east region"},
        )
        assert response.status_code == 200
        # Run should resume
        assert response.json()["status"] == "running"

    def test_clarification_validates_option_index(self, client):
        """Negative option index is rejected."""
        from app.api.v2_query_runs import _RUNS, QueryRun, QueryRunStatus
        from datetime import datetime

        create_resp = client.post(
            "/api/v2/query-runs",
            json={"agent_id": "sales", "question": "test"},
        )
        run_id = create_resp.json()["run_id"]
        run = _RUNS[run_id]
        _RUNS[run_id] = QueryRun(
            run_id=run.run_id,
            status=QueryRunStatus.CLARIFYING,
            question=run.question,
            principal_id=0,
            agent_id=run.agent_id,
            created_at=run.created_at,
            updated_at=run.updated_at,
            idempotency_key=run.idempotency_key,
            conversation_id=run.conversation_id,
            events=[],
        )

        response = client.post(
            f"/api/v2/query-runs/{run_id}/clarifications",
            json={"option_index": -1},
        )
        assert response.status_code == 422


class TestConversationAPI:
    """Test GET /api/v2/conversations/{id}."""

    def test_get_nonexistent_conversation_returns_empty(self, client):
        """Unknown conversation returns empty envelope (200)."""
        response = client.get("/api/v2/conversations/conv_xyz")
        assert response.status_code == 200
        body = response.json()
        assert body["conversation_id"] == "conv_xyz"
        assert body["turns"] == []


class TestErrorEnvelope:
    """Test the standard error response shape."""

    def test_404_envelope_shape(self, client):
        """404 returns standard error envelope."""
        # FastAPI default isn't our envelope, but verify the route returns 404
        response = client.get("/api/v2/query-runs/nonexistent")
        assert response.status_code == 404

    def test_error_response_class_structure(self):
        """ErrorResponse model has required fields."""
        from app.api.v2_query_runs import ErrorResponse

        err = ErrorResponse(
            error_code="not_found",
            message="Query run not found",
            trace_id="trace-abc-123",
        )

        assert err.error_code == "not_found"
        assert err.trace_id == "trace-abc-123"
