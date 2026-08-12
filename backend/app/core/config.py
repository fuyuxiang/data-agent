from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    meta_database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/data_agent"
    sample_database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/data_agent"

    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # Clarification thresholds are global for this iteration (spec 5.2).
    clarify_confidence_threshold: float = 0.7
    clarify_max_rounds: int = 2

    # Cost guardrails (spec M-13).
    max_result_rows: int = 1000
    query_timeout_seconds: int = 30
    cost_warn_rows: int = 1_000_000
    cost_reject_rows: int = 50_000_000


@lru_cache
def get_settings() -> Settings:
    return Settings()