from __future__ import annotations

from flask import Blueprint, current_app, request

from ..agent.store import RunStore
from ..services.data_plane.factory import livy_adapter, public_engine, trino_adapter
from ..services.data_plane.livy import LivyBatchAdapter, LivyConfig
from ..services.data_plane.trino import TrinoAdapter, TrinoConfig
from ..services.security import SecretVault
from .common import api_errors, body, current_user_id, db, ok, require_workspace_record, workspace_id


bp = Blueprint("warehouse", __name__)


def _engine(engine_id: str) -> dict:
    return require_workspace_record("warehouse_engines", engine_id)


@bp.get("/api/warehouse/engines")
def engines():
    return ok(items=[public_engine(item) for item in db().list("warehouse_engines", workspace_id=workspace_id())])


@bp.post("/api/warehouse/engines")
@api_errors
def create_engine():
    payload, wid = body(), workspace_id()
    engine_type = str(payload.get("type") or "trino").lower()
    engine_id = db().new_id("engine")
    vault = SecretVault(current_app.config["VAULT_KEY"])
    if engine_type == "trino":
        config = TrinoConfig.from_dict(payload | {"engine_id": engine_id})
        public = {
            "endpoint": config.endpoint, "catalog": config.catalog, "schema": config.schema,
            "user": config.user, "source": config.source, "scratch_catalog": config.scratch_catalog,
            "scratch_schema": config.scratch_schema, "scratch_prefix": config.scratch_prefix,
            "timeout_seconds": config.timeout_seconds, "max_preview_rows": config.max_preview_rows,
        }
        credential = vault.seal({"username": config.username, "password": config.password})
        capabilities = TrinoAdapter.capabilities
    elif engine_type == "livy":
        config = LivyConfig.from_dict(payload | {"engine_id": engine_id})
        public = {
            "endpoint": config.endpoint, "job_file": config.job_file, "proxy_user": config.proxy_user,
            "queue": config.queue, "result_prefix": config.result_prefix,
            "input_prefixes": list(config.input_prefixes), "driver_memory": config.driver_memory,
            "executor_memory": config.executor_memory, "executor_cores": config.executor_cores,
            "num_executors": config.num_executors, "timeout_seconds": config.timeout_seconds,
        }
        credential = vault.seal({})
        capabilities = LivyBatchAdapter.capabilities
    else:
        raise ValueError("远程引擎 type 必须是 trino 或 livy")
    item = db().put("warehouse_engines", {
        "id": engine_id, "workspace_id": wid, "name": str(payload.get("name") or engine_type.upper())[:100],
        "type": engine_type, **public, "credential": credential, "capabilities": capabilities,
        "native_limits_confirmed": bool(payload.get("native_limits_confirmed", False)),
        "enabled": bool(payload.get("enabled", True)), "status": "configured",
    }, workspace_id=wid)
    source = None
    if engine_type == "trino":
        source = db().put("sources", {
            "id": db().new_id("src"), "workspace_id": wid,
            "name": str(payload.get("source_name") or payload.get("name") or "Trino 数仓")[:120],
            "kind": "warehouse", "category": "warehouse", "engine_id": engine_id,
            "catalog": config.catalog, "schema": config.schema, "status": "ready",
            "tables": [], "lazy_catalog": True,
        }, workspace_id=wid)
        item = db().patch("warehouse_engines", engine_id, {"source_id": source["id"]}, workspace_id=wid) or item
    db().audit(
        "warehouse_engine.created", workspace_id=wid, actor=current_user_id(),
        object_type="warehouse_engine", object_id=engine_id,
        detail={"type": engine_type, "source_id": (source or {}).get("id")},
    )
    return ok(item=public_engine(item), source=source), 201


@bp.patch("/api/warehouse/engines/<engine_id>")
@api_errors
def update_engine(engine_id: str):
    current = _engine(engine_id)
    allowed = {key: body()[key] for key in ("name", "enabled", "native_limits_confirmed") if key in body()}
    if "enabled" in allowed:
        allowed["enabled"] = bool(allowed["enabled"])
    if "native_limits_confirmed" in allowed:
        allowed["native_limits_confirmed"] = bool(allowed["native_limits_confirmed"])
    return ok(item=public_engine(db().patch(
        "warehouse_engines", engine_id, allowed, workspace_id=current["workspace_id"],
    ) or current))


@bp.delete("/api/warehouse/engines/<engine_id>")
@api_errors
def disable_engine(engine_id: str):
    current = _engine(engine_id)
    item = db().patch("warehouse_engines", engine_id, {"enabled": False, "status": "disabled"}, workspace_id=current["workspace_id"])
    if current.get("source_id"):
        db().patch("sources", current["source_id"], {"status": "disabled"}, workspace_id=current["workspace_id"])
    return ok(item=public_engine(item or current))


@bp.get("/api/warehouse/engines/<engine_id>/capabilities")
@api_errors
def engine_capabilities(engine_id: str):
    item = _engine(engine_id)
    return ok(type=item["type"], capabilities=item.get("capabilities") or {})


@bp.get("/api/warehouse/engines/<engine_id>/catalog")
@api_errors
def engine_catalog(engine_id: str):
    _engine(engine_id)
    adapter = trino_adapter(db(), workspace_id(), engine_id)
    return ok(**adapter.discover(
        catalog=request.args.get("catalog"), schema=request.args.get("schema"),
        limit=int(request.args.get("limit", 100)), cursor=str(request.args.get("cursor") or ""),
    ))


@bp.get("/api/warehouse/engines/<engine_id>/search")
@api_errors
def search_engine_catalog(engine_id: str):
    _engine(engine_id)
    return ok(**trino_adapter(db(), workspace_id(), engine_id).search(
        str(request.args.get("q") or ""), catalog=request.args.get("catalog"),
        schema=request.args.get("schema"), limit=int(request.args.get("limit", 50)),
    ))


@bp.get("/api/warehouse/engines/<engine_id>/describe")
@api_errors
def describe_engine_table(engine_id: str):
    _engine(engine_id)
    return ok(item=trino_adapter(db(), workspace_id(), engine_id).describe(
        str(request.args.get("catalog") or ""), str(request.args.get("schema") or ""),
        str(request.args.get("table") or ""),
    ))


def _owned_run(run_id: str, wid: str) -> dict:
    run = RunStore(db()).get_run(run_id, workspace_id=wid)
    if not run or run.get("actor_id") != current_user_id():
        raise FileNotFoundError("分析任务不存在")
    return run


@bp.get("/api/warehouse/queries/<query_id>")
@api_errors
def warehouse_query(query_id: str):
    query = require_workspace_record("warehouse_queries", query_id)
    _owned_run(str(query.get("run_id") or ""), query["workspace_id"])
    return ok(item=trino_adapter(db(), query["workspace_id"], query["engine_id"]).public_query(query))


@bp.post("/api/warehouse/queries/<query_id>/cancel")
@api_errors
def cancel_warehouse_query(query_id: str):
    query = require_workspace_record("warehouse_queries", query_id)
    run = _owned_run(str(query.get("run_id") or ""), query["workspace_id"])
    RunStore(db()).update_status(run["id"], "cancelling", stop_reason="warehouse_cancel_requested")
    return ok(item=trino_adapter(db(), query["workspace_id"], query["engine_id"]).cancel(query_id))


@bp.get("/api/warehouse/queries/<query_id>/page")
@api_errors
def warehouse_query_page(query_id: str):
    query = require_workspace_record("warehouse_queries", query_id)
    _owned_run(str(query.get("run_id") or ""), query["workspace_id"])
    return ok(**trino_adapter(db(), query["workspace_id"], query["engine_id"]).read_page(
        query_id, offset=int(request.args.get("offset", 0)), limit=int(request.args.get("limit", 100)),
    ))


@bp.get("/api/warehouse/spark-jobs/<path:job_id>")
@api_errors
def spark_job(job_id: str):
    record = require_workspace_record("remote_batches", job_id)
    _owned_run(str(record.get("run_id") or ""), record["workspace_id"])
    return ok(item=livy_adapter(db(), record["workspace_id"], record["engine_id"]).reconcile(job_id))


@bp.get("/api/warehouse/spark-jobs/<path:job_id>/logs")
@api_errors
def spark_job_logs(job_id: str):
    record = require_workspace_record("remote_batches", job_id)
    _owned_run(str(record.get("run_id") or ""), record["workspace_id"])
    return ok(**livy_adapter(db(), record["workspace_id"], record["engine_id"]).logs(
        job_id, from_line=int(request.args.get("from", 0)), size=int(request.args.get("size", 100)),
    ))


@bp.post("/api/warehouse/spark-jobs/<path:job_id>/cancel")
@api_errors
def cancel_spark_job(job_id: str):
    record = require_workspace_record("remote_batches", job_id)
    run = _owned_run(str(record.get("run_id") or ""), record["workspace_id"])
    RunStore(db()).update_status(run["id"], "cancelling", stop_reason="spark_cancel_requested")
    return ok(item=livy_adapter(db(), record["workspace_id"], record["engine_id"]).cancel(job_id))
