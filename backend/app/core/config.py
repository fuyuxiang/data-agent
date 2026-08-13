"""Application settings loaded from environment / .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    OIDC settings live alongside the rest of the runtime config so the
    verifier and the API surface see a consistent picture; the secrets are
    declared here, never logged.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- runtime environment -------------------------------------------------
    environment: str = "development"  # development | test | production
    auth_mode: str = "dev"  # dev | oidc — dev keeps the X-Username fallback
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)

    # --- metadata + sample databases ---------------------------------------
    meta_database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/data_agent"
    sample_database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/data_agent"

    # --- OIDC ----------------------------------------------------------------
    oidc_issuer: str = "https://issuer.test"
    oidc_audience: str = "data-agent"
    oidc_jwks_url: str = "https://issuer.test/.well-known/jwks.json"
    oidc_client_id: str = "data-agent"
    oidc_client_secret: str = ""

    # --- JWT (legacy/dev only; production uses OIDC) -----------------------
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    # --- LLM ---------------------------------------------------------------
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = 30

    # Clarification thresholds are global for this iteration (spec 5.2).
    clarify_confidence_threshold: float = 0.7
    clarify_max_rounds: int = 2

    # Cost guardrails (spec M-13).
    max_result_rows: int = 1000
    query_timeout_seconds: int = 30
    cost_warn_rows: int = 1_000_000
    cost_reject_rows: int = 50_000_000

    # Execution retry budget for transient failures only (spec 5.4).
    execution_retry_attempts: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()