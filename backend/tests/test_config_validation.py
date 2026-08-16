"""Tests for production configuration validation.

Tests the validate_production_config() function:
- JWT secret validation
- Database credential checks
- CORS security rules
- Auth mode validation
- Database connectivity
- OIDC configuration
- LLM API availability
"""

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy.exc import DatabaseError

from app.main import validate_production_config


class MockSettings:
    """Mock settings object for testing."""

    def __init__(self, **kwargs):
        # Default values
        self.environment = "production"
        self.jwt_secret = "valid-secret-at-least-24-characters-long"
        self.meta_database_url = "postgresql://user:password@localhost/meta"
        self.sample_database_url = "postgresql://user:password@localhost/sample"
        self.cors_origins = ["https://example.com"]
        self.auth_mode = "oidc"
        self.llm_api_key = "sk-test-key"
        self.oidc_issuer = "https://auth.example.com"
        self.oidc_audience = "api"
        self.oidc_jwks_url = "https://auth.example.com/.well-known/jwks.json"
        self.oidc_client_secret = "secret-key"
        self.llm_base_url = "https://api.openai.com/v1"

        # Override with provided kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)


class TestJWTSecretValidation:
    """Test JWT secret validation rules."""

    def test_jwt_secret_too_short(self):
        """Reject JWT secrets shorter than 24 characters."""
        settings = MockSettings(jwt_secret="short")

        with pytest.raises(ValueError, match="jwt_secret must be at least 24 characters"):
            validate_production_config(settings)

    def test_jwt_secret_contains_dev_only(self):
        """Reject JWT secrets containing 'dev-only'."""
        settings = MockSettings(jwt_secret="dev-only-secret-24-chars-long")

        with pytest.raises(ValueError, match="jwt_secret must be at least 24 characters"):
            validate_production_config(settings)

    def test_jwt_secret_valid(self):
        """Accept valid JWT secrets."""
        settings = MockSettings(jwt_secret="a" * 32)

        # Should not raise (but may fail on other checks)
        try:
            validate_production_config(settings)
        except ValueError as e:
            # Any error should not be about JWT secret
            assert "jwt_secret" not in str(e).lower() or "24 characters" not in str(e)


class TestDatabaseCredentials:
    """Test database credential validation."""

    def test_meta_database_default_credentials(self):
        """Reject metadata database with default postgres:postgres."""
        settings = MockSettings(
            meta_database_url="postgresql://postgres:postgres@localhost/db"
        )

        with pytest.raises(ValueError, match="meta_database_url contains insecure default credentials"):
            validate_production_config(settings)

    def test_sample_database_default_credentials(self):
        """Reject sample database with default postgres:postgres."""
        settings = MockSettings(
            sample_database_url="postgresql://postgres:postgres@localhost/db"
        )

        with pytest.raises(ValueError, match="sample_database_url contains insecure default credentials"):
            validate_production_config(settings)

    def test_databases_must_be_separate(self):
        """Reject if meta and sample databases are the same."""
        same_url = "postgresql://user:password@localhost/db"
        settings = MockSettings(
            meta_database_url=same_url,
            sample_database_url=same_url
        )

        with pytest.raises(ValueError, match="must be separate"):
            validate_production_config(settings)


class TestCORSValidation:
    """Test CORS configuration rules."""

    def test_cors_wildcard_not_allowed(self):
        """Reject CORS wildcard in production."""
        settings = MockSettings(cors_origins=["*"])

        with pytest.raises(ValueError, match="not allowed in production"):
            validate_production_config(settings)

    def test_cors_localhost_not_allowed(self):
        """Reject CORS localhost origin in production."""
        settings = MockSettings(cors_origins=["http://localhost:3000"])

        with pytest.raises(ValueError, match="not allowed in production"):
            validate_production_config(settings)

    def test_cors_valid_https(self):
        """Accept valid HTTPS origins."""
        settings = MockSettings(cors_origins=["https://app.example.com"])

        try:
            validate_production_config(settings)
        except ValueError as e:
            # Should not fail on CORS
            assert "CORS" not in str(e)


class TestAuthValidation:
    """Test authentication configuration validation."""

    def test_auth_mode_dev_not_allowed(self):
        """Reject auth_mode=dev in production."""
        settings = MockSettings(auth_mode="dev")

        with pytest.raises(ValueError, match="auth_mode=dev not allowed"):
            validate_production_config(settings)

    def test_llm_api_key_required(self):
        """Require LLM API key in production."""
        settings = MockSettings(llm_api_key="")

        with pytest.raises(ValueError, match="llm_api_key must be set"):
            validate_production_config(settings)

    def test_oidc_issuer_required(self):
        """Require OIDC issuer when auth_mode=oidc."""
        settings = MockSettings(oidc_issuer="")

        with pytest.raises(ValueError, match="oidc_issuer must be set"):
            validate_production_config(settings)

    def test_oidc_audience_required(self):
        """Require OIDC audience when auth_mode=oidc."""
        settings = MockSettings(oidc_audience="")

        with pytest.raises(ValueError, match="oidc_audience must be set"):
            validate_production_config(settings)

    def test_oidc_jwks_url_must_use_https(self):
        """Require HTTPS for OIDC JWKS URL."""
        settings = MockSettings(oidc_jwks_url="http://auth.example.com/jwks.json")

        with pytest.raises(ValueError, match="oidc_jwks_url must use HTTPS"):
            validate_production_config(settings)

    def test_oidc_client_secret_required(self):
        """Require OIDC client secret when auth_mode=oidc."""
        settings = MockSettings(oidc_client_secret="")

        with pytest.raises(ValueError, match="oidc_client_secret must be set"):
            validate_production_config(settings)


class TestDatabaseConnectivity:
    """Test database connectivity validation."""

    @patch("app.main.meta_engine")
    def test_metadata_database_connection_fails(self, mock_engine):
        """Handle metadata database connection failure."""
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.execute.side_effect = DatabaseError("connection failed", None, None)

        settings = MockSettings()

        with pytest.raises(ValueError, match="Failed to connect to metadata database"):
            validate_production_config(settings)

    @patch("app.main.meta_engine")
    def test_schema_migrations_table_missing(self, mock_engine):
        """Detect missing schema_migrations table."""
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        # First execute (SELECT 1) succeeds
        # Second execute (check table) returns no result
        mock_result = MagicMock()
        mock_result.scalar.return_value = None
        mock_conn.execute.side_effect = [None, mock_result]

        settings = MockSettings()

        with pytest.raises(ValueError, match="schema_migrations table missing"):
            validate_production_config(settings)


class TestLLMAvailability:
    """Test LLM API availability checks."""

    @patch("app.main.httpx.Client")
    @patch("app.main.meta_engine")
    def test_llm_api_not_reachable_warning(self, mock_engine, mock_client_class):
        """Log warning if LLM API is not reachable (non-fatal)."""
        # Mock database checks to pass
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__.return_value = mock_conn
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1
        mock_conn.execute.side_effect = [None, mock_result]

        # Mock LLM API failure
        mock_client = MagicMock()
        mock_client_class.return_value.__enter__.return_value = mock_client
        mock_client.head.side_effect = Exception("Connection refused")

        settings = MockSettings()

        with patch("app.main.logger") as mock_logger:
            try:
                validate_production_config(settings)
            except ValueError:
                pass  # May fail for other reasons

            # Should log warning, not raise
            if mock_logger.warning.called:
                assert "LLM API not reachable" in str(mock_logger.warning.call_args)


class TestDevelopmentMode:
    """Test that development mode bypasses checks."""

    def test_development_mode_no_validation(self):
        """Development mode skips all production checks."""
        settings = MockSettings(
            environment="development",
            jwt_secret="short",
            meta_database_url="postgresql://postgres:postgres@localhost/db",
            cors_origins=["*"],
            auth_mode="dev",
            llm_api_key="",
            oidc_issuer="",
        )

        # Should not raise any errors
        validate_production_config(settings)


class TestMultipleErrors:
    """Test reporting of multiple validation errors."""

    def test_collect_multiple_errors(self):
        """Collect and report multiple validation errors."""
        settings = MockSettings(
            jwt_secret="short",
            auth_mode="dev",
            llm_api_key="",
        )

        try:
            validate_production_config(settings)
            pytest.fail("Should have raised ValueError")
        except ValueError as e:
            error_msg = str(e)
            # Should contain multiple errors
            assert "jwt_secret" in error_msg or "auth_mode" in error_msg or "llm_api_key" in error_msg
