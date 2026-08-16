"""Startup configuration validation tests.

Verifies that production environment fails fast on weak configurations.

These tests do not require database connectivity - they test configuration
validation logic only.
"""

import pytest
from pydantic_settings import BaseSettings

from app.core.config import Settings

pytestmark = pytest.mark.no_db


def create_settings(**overrides) -> Settings:
    """Create a Settings instance with overrides."""
    defaults = {
        "environment": "development",
        "auth_mode": "dev",
        "cors_origins": ("http://localhost:5173",),
        "meta_database_url": "postgresql+psycopg2://postgres:postgres@localhost/meta",
        "sample_database_url": "postgresql+psycopg2://postgres:postgres@localhost/sample",
        "jwt_secret": "dev-only-secret-change-me",
        "llm_api_key": "",
        "oidc_issuer": "https://issuer.test",
        "oidc_audience": "data-agent",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestConfigValidationProduction:
    """Production environment must reject weak configurations."""

    def test_weak_jwt_secret_fails(self):
        """JWT secret < 24 chars fails in production."""
        settings = create_settings(
            environment="production",
            jwt_secret="short"
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="jwt_secret"):
            validate_production_config(settings)

    def test_dev_only_jwt_secret_fails(self):
        """JWT secret containing 'dev-only' fails in production."""
        settings = create_settings(
            environment="production",
            jwt_secret="dev-only-secret-long-enough-24chars"
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="jwt_secret"):
            validate_production_config(settings)

    def test_insecure_meta_db_credentials_fails(self):
        """Meta database URL with postgres:postgres credentials fails."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            meta_database_url="postgresql+psycopg2://postgres:postgres@localhost/db"
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="postgres:postgres"):
            validate_production_config(settings)

    def test_insecure_sample_db_credentials_fails(self):
        """Sample database URL with postgres:postgres credentials fails."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            sample_database_url="postgresql+psycopg2://postgres:postgres@localhost/db"
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="postgres:postgres"):
            validate_production_config(settings)

    def test_wildcard_cors_fails(self):
        """CORS with wildcard '*' fails in production."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            cors_origins=("*",)
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="CORS"):
            validate_production_config(settings)

    def test_localhost_cors_fails(self):
        """CORS with localhost fails in production."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            cors_origins=("http://localhost:5173",)
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="CORS"):
            validate_production_config(settings)

    def test_dev_auth_mode_fails(self):
        """AUTH_MODE=dev fails in production."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            auth_mode="dev"
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="auth_mode"):
            validate_production_config(settings)

    def test_missing_llm_api_key_fails(self):
        """LLM API key must be set in production."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            llm_api_key=""
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="llm_api_key"):
            validate_production_config(settings)

    def test_missing_oidc_issuer_fails(self):
        """OIDC issuer must be set in production."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            oidc_issuer=""
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="oidc_issuer"):
            validate_production_config(settings)

    def test_missing_oidc_audience_fails(self):
        """OIDC audience must be set in production."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            oidc_audience=""
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="oidc_audience"):
            validate_production_config(settings)

    def test_same_database_url_fails(self):
        """Meta and sample databases must use different URLs."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            meta_database_url="postgresql+psycopg2://reader:pwd@localhost/db",
            sample_database_url="postgresql+psycopg2://reader:pwd@localhost/db"
        )

        from app.main import validate_production_config
        with pytest.raises(ValueError, match="separate"):
            validate_production_config(settings)

    @pytest.mark.skip(reason="Requires real DB connection for schema_migrations check")
    def test_all_valid_passes(self):
        """Valid production configuration passes."""
        settings = create_settings(
            environment="production",
            jwt_secret="this-is-a-strong-secret-of-at-least-24-characters-long",
            auth_mode="oidc",
            cors_origins=("https://app.example.com", "https://admin.example.com"),
            meta_database_url="postgresql+psycopg2://reader_meta:pwd@localhost/meta_db",
            sample_database_url="postgresql+psycopg2://reader_sample:pwd@localhost/sample_db",
            llm_api_key="sk-valid-key-12345",
            oidc_issuer="https://issuer.example.com",
            oidc_audience="data-agent",
            oidc_client_secret="this-is-a-strong-client-secret-for-oidc",
            oidc_jwks_url="https://issuer.example.com/.well-known/jwks.json",
        )

        from app.main import validate_production_config
        # Should not raise
        validate_production_config(settings)


class TestConfigValidationDevelopment:
    """Development environment allows weak configurations."""

    def test_dev_mode_allows_weak_config(self):
        """Development mode bypasses all validation."""
        settings = create_settings(
            environment="development",
            jwt_secret="weak",
            auth_mode="dev",
            cors_origins=("*",),
            llm_api_key=""
        )

        from app.main import validate_production_config
        # Should not raise
        validate_production_config(settings)
