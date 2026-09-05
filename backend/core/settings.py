from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    environment: str
    root: Path
    storage_dir: Path
    frontend_dir: Path
    database_path: Path
    upload_dir: Path
    export_dir: Path
    knowledge_dir: Path
    workspace_dir: Path
    trash_dir: Path
    secret_key: str
    encryption_key: str
    max_upload_bytes: int
    max_json_bytes: int
    max_ingest_rows: int
    max_ingest_cells: int
    max_query_rows: int
    source_sample_rows: int
    query_timeout_seconds: int
    max_analysis_rows: int
    max_analysis_cells: int
    daily_token_limit: int
    agent_max_iterations: int
    agent_max_run_seconds: int
    default_context_window: int
    default_max_output_tokens: int
    allowed_origins: list[str]
    trusted_hosts: list[str]

    @classmethod
    def from_environment(cls, root: Path) -> "Settings":
        environment = os.getenv("MERIDIAN_ENV", "development").strip().lower()
        if environment not in {"development", "production", "test"}:
            raise ValueError("MERIDIAN_ENV 必须是 development、production 或 test")
        storage = Path(os.getenv("MERIDIAN_STORAGE_DIR", root / "storage")).resolve()
        configured_secret = os.getenv("MERIDIAN_SECRET_KEY", "").strip()
        if configured_secret:
            secret_key = configured_secret
        else:
            storage.mkdir(parents=True, exist_ok=True)
            secret_path = storage / ".secret_key"
            if secret_path.is_file():
                secret_key = secret_path.read_text(encoding="utf-8").strip()
            else:
                secret_key = secrets.token_urlsafe(48)
                secret_path.write_text(secret_key, encoding="utf-8")
                try:
                    secret_path.chmod(0o600)
                except OSError:
                    pass
        encryption_key = os.getenv("MERIDIAN_ENCRYPTION_KEY", "").strip() or secret_key
        return cls(
            environment=environment,
            root=root,
            storage_dir=storage,
            frontend_dir=Path(os.getenv("MERIDIAN_FRONTEND_DIR", root / "frontend")).resolve(),
            database_path=storage / "meridian.sqlite3",
            upload_dir=storage / "uploads",
            export_dir=storage / "exports",
            knowledge_dir=storage / "knowledge",
            workspace_dir=storage / "workspaces",
            trash_dir=storage / "trash",
            secret_key=secret_key,
            encryption_key=encryption_key,
            max_upload_bytes=int(os.getenv("MERIDIAN_MAX_UPLOAD_MB", "100")) * 1024 * 1024,
            max_json_bytes=max(1, int(os.getenv("MERIDIAN_MAX_JSON_MB", "2"))) * 1024 * 1024,
            max_ingest_rows=max(1, int(os.getenv("MERIDIAN_MAX_INGEST_ROWS", "1000000"))),
            max_ingest_cells=max(1, int(os.getenv("MERIDIAN_MAX_INGEST_CELLS", "10000000"))),
            max_query_rows=max(1, int(os.getenv("MERIDIAN_MAX_QUERY_ROWS", "10000"))),
            source_sample_rows=max(1, int(os.getenv("MERIDIAN_SOURCE_SAMPLE_ROWS", "50000"))),
            query_timeout_seconds=max(1, int(os.getenv("MERIDIAN_QUERY_TIMEOUT_SECONDS", "30"))),
            max_analysis_rows=max(100, int(os.getenv("MERIDIAN_MAX_ANALYSIS_ROWS", "100000"))),
            max_analysis_cells=max(1000, int(os.getenv("MERIDIAN_MAX_ANALYSIS_CELLS", "2000000"))),
            daily_token_limit=max(1, int(os.getenv("MERIDIAN_DAILY_TOKEN_LIMIT", "1000000"))),
            agent_max_iterations=max(1, int(os.getenv("MERIDIAN_AGENT_MAX_ITERATIONS", "32"))),
            agent_max_run_seconds=max(10, int(os.getenv("MERIDIAN_AGENT_MAX_RUN_SECONDS", "600"))),
            default_context_window=max(4096, int(os.getenv("MERIDIAN_DEFAULT_CONTEXT_WINDOW", "32768"))),
            default_max_output_tokens=max(128, int(os.getenv("MERIDIAN_DEFAULT_MAX_OUTPUT_TOKENS", "4096"))),
            allowed_origins=[
                value.strip() for value in os.getenv(
                    "MERIDIAN_ALLOWED_ORIGINS",
                    "" if environment == "production" else (
                        "http://localhost:5001,http://127.0.0.1:5001,"
                        "http://localhost:5173,http://127.0.0.1:5173"
                    ),
                ).split(",") if value.strip()
            ],
            trusted_hosts=[
                value.strip() for value in os.getenv("MERIDIAN_TRUSTED_HOSTS", "").split(",")
                if value.strip()
            ],
        )

    @classmethod
    def for_tests(cls, root: Path, config: dict) -> "Settings":
        base = cls.from_environment(root)
        database_path = Path(config.get("DATABASE_PATH", base.database_path))
        storage_dir = Path(config.get("STORAGE_DIR", database_path.parent))
        return replace(
            base,
            storage_dir=storage_dir,
            database_path=database_path,
            upload_dir=storage_dir / "uploads",
            export_dir=storage_dir / "exports",
            knowledge_dir=storage_dir / "knowledge",
            workspace_dir=storage_dir / "workspaces",
            trash_dir=storage_dir / "trash",
        )

    def ensure_directories(self) -> None:
        for path in (
            self.storage_dir,
            self.upload_dir,
            self.export_dir,
            self.knowledge_dir,
            self.workspace_dir,
            self.trash_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
