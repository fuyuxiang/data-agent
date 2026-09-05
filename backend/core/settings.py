from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Settings:
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
    max_upload_bytes: int
    allowed_origins: list[str]

    @classmethod
    def from_environment(cls, root: Path) -> "Settings":
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
        return cls(
            root=root,
            storage_dir=storage,
            frontend_dir=root / "frontend",
            database_path=storage / "meridian.sqlite3",
            upload_dir=storage / "uploads",
            export_dir=storage / "exports",
            knowledge_dir=storage / "knowledge",
            workspace_dir=storage / "workspaces",
            trash_dir=storage / "trash",
            secret_key=secret_key,
            max_upload_bytes=int(os.getenv("MERIDIAN_MAX_UPLOAD_MB", "100")) * 1024 * 1024,
            allowed_origins=[
                "http://localhost:5001",
                "http://127.0.0.1:5001",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
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
