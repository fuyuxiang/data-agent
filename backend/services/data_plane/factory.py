from __future__ import annotations

import os
from typing import Any

from flask import current_app

from ...core.database import Database
from ..security import SecretVault
from .livy import LivyBatchAdapter, LivyConfig
from .sandbox_client import SandboxClient
from .trino import TrinoAdapter, TrinoConfig


def public_engine(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"credential"}}


def trino_adapter(database: Database, workspace_id: str, engine_id: str) -> TrinoAdapter:
    record = database.get("warehouse_engines", engine_id, workspace_id=workspace_id)
    if not record or record.get("type") != "trino" or not record.get("enabled", True):
        raise FileNotFoundError("Trino 引擎不存在或已禁用")
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(record.get("credential", ""), {}) or {}
    return TrinoAdapter(database, workspace_id, TrinoConfig.from_dict({**record, **secret, "engine_id": record["id"]}))


def livy_adapter(database: Database, workspace_id: str, engine_id: str) -> LivyBatchAdapter:
    record = database.get("warehouse_engines", engine_id, workspace_id=workspace_id)
    if not record or record.get("type") != "livy" or not record.get("enabled", True):
        raise FileNotFoundError("Livy 引擎不存在或已禁用")
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(record.get("credential", ""), {}) or {}
    return LivyBatchAdapter(database, workspace_id, LivyConfig.from_dict({**record, **secret, "engine_id": record["id"]}))


def sandbox_client() -> SandboxClient:
    settings = current_app.config["SETTINGS"]
    return SandboxClient(
        endpoint=os.getenv("MERIDIAN_SANDBOX_PROXY_URL", ""),
        token=os.getenv("MERIDIAN_SANDBOX_PROXY_TOKEN", ""),
        input_root=settings.workspace_dir / "sandbox-inputs",
        output_root=settings.export_dir / "sandbox",
        timeout_seconds=max(5, int(os.getenv("MERIDIAN_SANDBOX_TIMEOUT_SECONDS", "120"))),
        expected_image=os.getenv("MERIDIAN_SANDBOX_IMAGE", "meridian-sandbox:py311-20260906"),
    )
