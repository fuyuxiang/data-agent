"""Tests for health check endpoints (/livez, /readyz).

Tests:
- /livez endpoint (liveness probe)
- /readyz endpoint (readiness probe)
- OIDC JWKS availability checking
- Database connectivity validation
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client for health check endpoints."""
    from app.main import app

    return TestClient(app)


class TestLivenessProbe:
    """Test /livez endpoint (liveness)."""

    def test_livez_returns_ok(self, client):
        """GET /livez returns 200 and 'alive' status."""
        response = client.get("/livez")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    def test_livez_no_dependencies(self, client):
        """Liveness probe doesn't require any external dependencies."""
        # Even with all deps failing, liveness should still succeed
        with patch("app.main.meta_engine") as mock_meta, \
             patch("app.main.sample_engine") as mock_sample:
            mock_meta.connect.side_effect = Exception("DB down")
            mock_sample.connect.side_effect = Exception("DB down")

            response = client.get("/livez")
            assert response.status_code == 200
            assert response.json()["status"] == "alive"


class TestReadinessProbe:
    """Test /readyz endpoint (readiness)."""

    def test_readyz_returns_ready(self, client):
        """GET /readyz returns 200 when all checks pass."""
        with patch("app.main.meta_engine"), \
             patch("app.main.sample_engine"), \
             patch("app.main.get_meta_session"), \
             patch("app.main.load_dataset"):
            response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    def test_readyz_checks_metadata_db(self, client):
        """Readiness verifies metadata database connectivity."""
        with patch("app.main.meta_engine") as mock_meta:
            mock_conn = MagicMock()
            mock_meta.connect.return_value.__enter__.return_value = mock_conn
            mock_conn.execute.side_effect = Exception("Connection refused")

            response = client.get("/readyz")
            assert response.status_code == 503

    def test_readyz_checks_sample_db(self, client):
        """Readiness verifies sample database connectivity."""
        with patch("app.main.sample_engine") as mock_sample:
            mock_conn = MagicMock()
            mock_sample.connect.return_value.__enter__.return_value = mock_conn
            # First call succeeds (metadata), second fails (sample)
            mock_conn.execute.side_effect = [
                None,  # metadata SELECT 1
                Exception("Sample DB connection refused"),  # sample SELECT 1
            ]

            with patch("app.main.meta_engine"):
                response = client.get("/readyz")
                assert response.status_code == 503


class TestOIDCHealthCheck:
    """Test OIDC JWKS availability checking in /readyz."""

    @patch("app.main.httpx.Client")
    @patch("app.main.load_dataset")
    @patch("app.main.get_meta_session")
    def test_oidc_jwks_check_succeeds(
        self, mock_ms, mock_load, mock_client_class, client
    ):
        """JWKS endpoint reachable → readyz returns 200."""
        # Mock successful HTTP HEAD response
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_client.head.return_value = mock_response

        # Force OIDC mode in settings
        with patch("app.main.settings") as mock_settings:
            mock_settings.auth_mode = "oidc"
            mock_settings.oidc_jwks_url = "https://auth.example.com/jwks"

            # Reset OIDC cache before test
            with patch("app.main._oidc_jwks_cache", {
                "checked_at": 0.0, "healthy": False, "error": None
            }):
                response = client.get("/readyz")
                assert response.status_code == 200

    @patch("app.main.httpx.Client")
    @patch("app.main.load_dataset")
    @patch("app.main.get_meta_session")
    def test_oidc_jwks_check_fails(
        self, mock_ms, mock_load, mock_client_class, client
    ):
        """JWKS endpoint unreachable → readyz returns 503."""
        # Mock failed HTTP HEAD response
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        mock_client.head.side_effect = Exception("JWKS server unreachable")

        with patch("app.main.settings") as mock_settings:
            mock_settings.auth_mode = "oidc"
            mock_settings.oidc_jwks_url = "https://auth.example.com/jwks"

            # Reset OIDC cache before test
            with patch("app.main._oidc_jwks_cache", {
                "checked_at": 0.0, "healthy": False, "error": None
            }):
                response = client.get("/readyz")
                assert response.status_code == 503

    @patch("app.main.httpx.Client")
    @patch("app.main.load_dataset")
    @patch("app.main.get_meta_session")
    def test_jwks_cache_reused(
        self, mock_ms, mock_load, mock_client_class, client
    ):
        """JWKS check results are cached for 60 seconds."""
        with patch("app.main.httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_client.head.return_value = mock_response

            with patch("app.main.settings") as mock_settings:
                mock_settings.auth_mode = "oidc"
                mock_settings.oidc_jwks_url = "https://auth.example.com/jwks"

                # First call triggers JWKS check
                with patch("app.main._oidc_jwks_cache", {
                    "checked_at": 0.0, "healthy": False, "error": None
                }):
                    client.get("/readyz")
                    call_count_first = mock_client.head.call_count

                # Second call should use cache (no new HTTP call)
                with patch("app.main._oidc_jwks_cache", {
                    "checked_at": time.time(),  # Recently cached
                    "healthy": True, "error": None
                }):
                    client.get("/readyz")
                    # No new HTTP calls made
                    assert mock_client.head.call_count == call_count_first

    @patch("app.main.httpx.Client")
    @patch("app.main.load_dataset")
    @patch("app.main.get_meta_session")
    def test_jwks_cache_expires_after_60s(
        self, mock_ms, mock_load, mock_client_class, client
    ):
        """JWKS check refreshes after 60 seconds."""
        with patch("app.main.httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_client.head.return_value = mock_response

            with patch("app.main.settings") as mock_settings:
                mock_settings.auth_mode = "oidc"
                mock_settings.oidc_jwks_url = "https://auth.example.com/jwks"

                # Cache expired (checked_at is too old)
                with patch("app.main._oidc_jwks_cache", {
                    "checked_at": time.time() - 120,  # 2 minutes ago
                    "healthy": True, "error": None
                }):
                    client.get("/readyz")
                    # Should make a new HTTP call
                    assert mock_client.head.call_count >= 1


class TestSemanticModelLoading:
    """Test semantic model loading check in /readyz."""

    @patch("app.main.load_dataset")
    def test_semantic_model_load_fails(self, mock_load, client):
        """Semantic model load failure → readyz returns 503."""
        mock_load.side_effect = Exception("Model load failed")

        response = client.get("/readyz")
        assert response.status_code == 503
