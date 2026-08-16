from contextlib import asynccontextmanager
import logging
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app.api import chat, trace
from app.api import errors as api_errors
from app.api.semantic import router as semantic_router
from app.core.config import get_settings
from app.core.db import get_meta_session, meta_engine, sample_engine
from app.semantic.loader import load_dataset

logger = logging.getLogger(__name__)


def validate_production_config(settings) -> None:
    """Validate configuration for production safety.

    Raises ValueError if any critical configuration is weak or missing.
    Development mode bypasses all checks.
    """
    if settings.environment != "production":
        return

    errors = []

    # 1. JWT_SECRET validation
    if len(settings.jwt_secret) < 24 or "dev-only" in settings.jwt_secret.lower():
        errors.append("jwt_secret must be at least 24 characters and not contain 'dev-only'")

    # 2. Meta database credentials
    if "postgres:postgres" in settings.meta_database_url:
        errors.append("meta_database_url contains insecure default credentials (postgres:postgres)")

    # 3. Sample database credentials
    if "postgres:postgres" in settings.sample_database_url:
        errors.append("sample_database_url contains insecure default credentials (postgres:postgres)")

    # 4. CORS configuration
    for origin in settings.cors_origins:
        if origin == "*" or "localhost" in origin:
            errors.append(f"CORS origin '{origin}' not allowed in production (no wildcard, no localhost)")

    # 5. AUTH_MODE must not be dev
    if settings.auth_mode == "dev":
        errors.append("auth_mode=dev not allowed in production")

    # 6. LLM API key required
    if not settings.llm_api_key:
        errors.append("llm_api_key must be set in production")

    # 7. OIDC configuration required
    if not settings.oidc_issuer:
        errors.append("oidc_issuer must be set in production")
    if not settings.oidc_audience:
        errors.append("oidc_audience must be set in production")

    # 8. Database URLs must be separate (different roles)
    if settings.meta_database_url == settings.sample_database_url:
        errors.append("meta_database_url and sample_database_url must be separate (use different roles)")

    # 9. OIDC JWKS URL must use HTTPS
    if settings.auth_mode == "oidc":
        if not settings.oidc_jwks_url.startswith("https://"):
            errors.append("oidc_jwks_url must use HTTPS in production")

    # 10. OIDC client_secret must be non-empty
    if settings.auth_mode == "oidc" and not settings.oidc_client_secret:
        errors.append("oidc_client_secret must be set in production (non-empty)")

    # 11. Test metadata database connection
    try:
        with meta_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except DatabaseError as e:
        errors.append(f"Failed to connect to metadata database: {str(e)[:100]}")

    # 12. Verify schema_migrations table exists (required for migration tracking)
    try:
        with meta_engine.connect() as conn:
            result = conn.execute(text("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = '_meta' AND table_name = 'schema_migrations'
            """))
            if not result.scalar():
                errors.append("schema_migrations table missing in _meta schema (run migrations first)")
    except DatabaseError as e:
        errors.append(f"Failed to check schema_migrations table: {str(e)[:100]}")

    # 13. Verify LLM API is reachable (non-fatal warning)
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.head(settings.llm_base_url, follow_redirects=True)
            response.raise_for_status()
    except Exception as e:
        logger.warning(f"LLM API not reachable (non-fatal): {str(e)[:100]}")

    if errors:
        raise ValueError("Production configuration validation failed:\n" + "\n".join(f"  • {e}" for e in errors))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan hook for startup/shutdown."""
    settings = get_settings()

    # Startup: validate production config
    try:
        validate_production_config(settings)
    except ValueError as e:
        logger.error(f"Startup validation failed: {e}")
        raise

    logger.info(f"Started in {settings.environment} mode (auth_mode={settings.auth_mode})")
    yield
    # Shutdown: no cleanup needed
    logger.info("Shutdown complete")


app = FastAPI(title="Data Agent", version="0.1.0", lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(semantic_router)
app.include_router(chat.router)
app.include_router(trace.router)

api_errors.register(app)

# OIDC JWKS availability cache (TTL: 60 seconds)
_oidc_jwks_cache = {
    "checked_at": 0.0,
    "healthy": False,
    "error": None,
}


@app.get("/livez")
def liveness() -> dict[str, str]:
    """Liveness probe: process is running."""
    return {"status": "alive"}


@app.get("/readyz")
def readiness() -> dict[str, str]:
    """Readiness probe: service is ready to handle requests.

    Checks:
    1. Metadata database connectivity
    2. Sample database connectivity
    3. Semantic model can load
    4. OIDC JWKS endpoint is reachable (if OIDC auth mode)
    """
    try:
        # Check metadata database
        with meta_engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # Check sample database
        with sample_engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # Check semantic model can load
        try:
            with get_meta_session() as session:
                load_dataset(session, "orders")
        except Exception as e:
            raise RuntimeError(f"Semantic model load failed: {e}")

        # Check OIDC JWKS availability (cached, 60s TTL)
        if settings.auth_mode == "oidc":
            now = time.time()
            cache_ttl = 60  # seconds

            # Refresh cache if expired
            if now - _oidc_jwks_cache["checked_at"] > cache_ttl:
                try:
                    with httpx.Client(timeout=5.0) as client:
                        response = client.head(
                            settings.oidc_jwks_url,
                            follow_redirects=True
                        )
                        response.raise_for_status()
                        _oidc_jwks_cache["healthy"] = True
                        _oidc_jwks_cache["error"] = None
                except Exception as e:
                    _oidc_jwks_cache["healthy"] = False
                    _oidc_jwks_cache["error"] = str(e)
                finally:
                    _oidc_jwks_cache["checked_at"] = now

            # Fail readiness if OIDC JWKS is not healthy
            if not _oidc_jwks_cache["healthy"]:
                error_msg = _oidc_jwks_cache.get("error", "unknown error")
                raise RuntimeError(f"OIDC JWKS endpoint not reachable: {error_msg}")

        return {"status": "ready"}
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Service not ready: {str(e)}"
        )


@app.get("/api/health")
def health() -> dict[str, str]:
    """Deprecated: use /livez and /readyz instead."""
    return {"status": "ok"}