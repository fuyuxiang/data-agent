from __future__ import annotations

import os
from typing import Any

from flask import current_app

from ...core.database import Database
from ..security import SecretVault
from .livy import LivyBatchAdapter, LivyConfig
from .local_analysis import LocalAnalysisRunner
from .trino import TrinoAdapter, TrinoConfig


def public_engine(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"credential"}}


def trino_adapter(database: Database, workspace_id: str, engine_id: str, *, for_cancellation: bool = False) -> TrinoAdapter:
    record = database.get("warehouse_engines", engine_id, workspace_id=workspace_id, include_archived=for_cancellation)
    if not record or record.get("type") != "trino" or (not for_cancellation and not record.get("enabled", True)):
        raise FileNotFoundError("Trino 引擎不存在或已禁用")
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(record.get("credential", ""), {}) or {}
    return TrinoAdapter(database, workspace_id, TrinoConfig.from_dict({**record, **secret, "engine_id": record["id"]}))


def livy_adapter(database: Database, workspace_id: str, engine_id: str, *, for_cancellation: bool = False) -> LivyBatchAdapter:
    record = database.get("warehouse_engines", engine_id, workspace_id=workspace_id, include_archived=for_cancellation)
    if not record or record.get("type") != "livy" or (not for_cancellation and not record.get("enabled", True)):
        raise FileNotFoundError("Livy 引擎不存在或已禁用")
    secret = SecretVault(current_app.config["VAULT_KEY"]).open(record.get("credential", ""), {}) or {}
    return LivyBatchAdapter(database, workspace_id, LivyConfig.from_dict({**record, **secret, "engine_id": record["id"]}))


def local_analysis_runner() -> LocalAnalysisRunner:
    settings = current_app.config["SETTINGS"]
    return LocalAnalysisRunner(
        input_root=settings.workspace_dir / "analysis-inputs",
        output_root=settings.export_dir / "analysis",
        timeout_seconds=max(5, int(os.getenv("MERIDIAN_LOCAL_ANALYSIS_TIMEOUT_SECONDS", "120"))),
    )
