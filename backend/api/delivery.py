from __future__ import annotations

from pathlib import Path

import pandas as pd
from flask import Blueprint, current_app, request, send_file

from ..agent.store import RunStore
from ..services.exports import export_data, export_report
from ..services.authorization import actor_role, require_sources_access
from ..services.data_policy import policy_fingerprint
from ..services.results.delivery import ARTIFACT_KINDS, generate_artifacts, prepare_eml, send_email
from ..services.results.manifests import ResultService
from ..services.saas import assert_feature_enabled
from .common import (
    api_errors, current_user_id,
    body,
    db,
    ok,
    require_workspace_record,
    safe_child,
    workspace_id,
)


bp = Blueprint("delivery", __name__)


@bp.post("/api/exports/data")
@api_errors
def create_data_export():
    assert_feature_enabled(db(), workspace_id(), "result_delivery")
    artifact = export_data(body(), workspace_id(), current_user_id())
    return ok(artifact=_public_artifact(artifact)), 201


@bp.post("/api/exports/report")
@api_errors
def create_report_export():
    assert_feature_enabled(db(), workspace_id(), "result_delivery")
    artifact = export_report(body(), workspace_id(), current_user_id())
    return ok(artifact=_public_artifact(artifact)), 201


def _public_artifact(item: dict) -> dict:
    value = dict(item)
    value.pop("path", None)
    value.pop("policy_fingerprint", None)
    value["download_url"] = f"/api/artifacts/{item['id']}/download"
    return value


def _artifact_policy_access(item: dict, *, action: str = "read", source_ids: list[str] | None = None) -> None:
    wid = item["workspace_id"]
    actor = current_user_id()
    owner = str(item.get("actor_id") or "")
    if not item.get("run_id") and (
        owner and owner != actor or not owner and actor_role(db(), wid, actor) != "owner"
    ):
        raise FileNotFoundError("成果不存在")
    sources = require_sources_access(
        db(), source_ids if source_ids is not None else item.get("source_ids") or [],
        workspace_id=wid, actor_id=actor, action=action,
    )
    if not item.get("run_id") and sources:
        current = policy_fingerprint(
            sources, actor_id=actor, role=actor_role(db(), wid, actor),
        )
        recorded = item.get("policy_fingerprint")
        if (recorded and recorded != current) or (not recorded and any(
            source.get("row_policy") or source.get("column_policies") for source in sources
        )):
            raise PermissionError("数据权限已变更，请重新导出成果")


def _actor_artifacts(items: list[dict]) -> list[dict]:
    """Hide analysis-owned artifacts from every actor except the run owner."""
    actor_id = current_user_id()
    run_ids = {str(item["run_id"]) for item in items if item.get("run_id")}
    owned = {
        run_id for run_id in run_ids
        if (run := RunStore(db()).get_run(run_id, workspace_id=workspace_id()))
        and run.get("actor_id") == actor_id
    }
    visible = []
    for item in items:
        if item.get("run_id") and str(item["run_id"]) not in owned:
            continue
        try:
            _artifact_policy_access(item)
        except (FileNotFoundError, PermissionError):
            continue
        if item.get("run_id"):
            from ..services.advanced_agent import _source_authorized

            run = RunStore(db()).get_run(str(item["run_id"]), workspace_id=item["workspace_id"])
            if not run or not _source_authorized(db(), run):
                continue
        visible.append(item)
    return visible


@bp.get("/api/artifacts")
def list_artifacts():
    items = _actor_artifacts(db().list("artifacts", workspace_id=workspace_id()))
    return ok(items=[_public_artifact(item) for item in items])


@bp.get("/api/artifacts/<artifact_id>/download")
@api_errors
def download_artifact(artifact_id: str):
    item = require_workspace_record("artifacts", artifact_id)
    source_ids = item.get("source_ids") or []
    if item.get("run_id"):
        run = RunStore(db()).get_run(str(item["run_id"]), workspace_id=item["workspace_id"])
        if not run or run.get("actor_id") != current_user_id():
            raise FileNotFoundError("成果不存在")
        # Older artifacts did not persist source_ids. Always use the run's
        # current source scope so revocation also covers existing files.
        source_ids = run.get("source_scope") or []
    _artifact_policy_access(item, action="export", source_ids=source_ids)
    if item.get("run_id"):
        from ..services.advanced_agent import _source_authorized

        if not _source_authorized(db(), run):
            raise PermissionError("数据权限已变更，请重新运行分析")
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(item["path"]))
    if not path.exists():
        raise FileNotFoundError("成果文件已不存在")
    return send_file(path, as_attachment=True, download_name=item["filename"])


def _analysis_run(run_id: str) -> dict:
    run = RunStore(db()).get_run(run_id, workspace_id=workspace_id())
    if not run or run.get("actor_id") != current_user_id():
        raise FileNotFoundError("分析任务不存在")
    require_sources_access(
        db(), run.get("source_scope") or [], workspace_id=run["workspace_id"],
        actor_id=current_user_id(), action="read",
    )
    from ..services.advanced_agent import _source_authorized

    if not _source_authorized(db(), run):
        raise PermissionError("数据权限已变更，请重新发起分析")
    return run


@bp.get("/api/analyses/<run_id>/results")
@api_errors
def analysis_results(run_id: str):
    run = _analysis_run(run_id)
    service = ResultService(db())
    publication = service.publication(run_id, workspace_id=run["workspace_id"])
    if not publication:
        return ok(status="not_published", publication=None, manifest=None, artifacts=[])
    manifest = service.manifest(publication["manifest_id"], workspace_id=run["workspace_id"])
    artifacts = [
        _public_artifact(item) for item in db().list("artifacts", workspace_id=run["workspace_id"], limit=5000)
        if item.get("manifest_id") == publication["manifest_id"]
    ]
    return ok(status="published", publication=publication, manifest=manifest, artifacts=artifacts)


@bp.get("/api/analyses/<run_id>/details")
@api_errors
def analysis_details(run_id: str):
    run = _analysis_run(run_id)
    publication = ResultService(db()).publication(run_id, workspace_id=run["workspace_id"])
    if not publication:
        raise PermissionError("结果未通过发布门禁")
    manifest = ResultService(db()).manifest(publication["manifest_id"], workspace_id=run["workspace_id"])
    table = next(iter((manifest or {}).get("payload", {}).get("tables") or []), {})
    result = db().get("query_results", str(table.get("result_id") or ""), workspace_id=run["workspace_id"])
    if not result:
        return ok(items=[], columns=table.get("columns") or [], total=None, completeness="unknown", next_cursor=None)
    page_size = max(1, min(int(request.args.get("limit", 100)), 500))
    cursor = max(0, int(request.args.get("cursor", 0)))
    path = safe_child(current_app.config["SETTINGS"].export_dir, Path(result["path"]))
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("明细结果超过本地分页上限，应通过远程 DatasetRef 分页")
    frame = pd.read_csv(path, skiprows=range(1, cursor + 1), nrows=page_size)
    items = frame.where(pd.notna(frame), None).to_dict(orient="records")
    next_cursor = cursor + len(items) if cursor + len(items) < int(result.get("rows") or 0) else None
    return ok(
        items=items, columns=result.get("columns") or [], total=result.get("total_rows"),
        returned_total=result.get("rows"), completeness=result.get("completeness"), next_cursor=next_cursor,
    )


@bp.post("/api/analyses/<run_id>/artifacts")
@api_errors
def create_analysis_artifacts(run_id: str):
    run = _analysis_run(run_id)
    kinds = body().get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or any(str(item) not in ARTIFACT_KINDS for item in kinds)):
        raise ValueError("成果 kinds 无效")
    items = generate_artifacts(db(), run_id, run["workspace_id"], [str(item) for item in kinds] if kinds else None)
    return ok(items=[_public_artifact(item) for item in items]), 201


def _email_payload(run: dict) -> tuple[dict, str, str, list[str] | None]:
    payload = body()
    publication = ResultService(db()).publication(run["id"], workspace_id=run["workspace_id"])
    if not publication:
        raise PermissionError("成果未通过发布门禁")
    manifest = ResultService(db()).manifest(publication["manifest_id"], workspace_id=run["workspace_id"])
    subject = str(payload.get("subject") or f"数据分析成果 · {run['id']}")
    text = str(payload.get("body") or (manifest or {}).get("payload", {}).get("summary") or "")
    kinds = payload.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or any(str(item) not in ARTIFACT_KINDS for item in kinds)):
        raise ValueError("附件 kinds 无效")
    return payload, subject, text, [str(item) for item in kinds] if kinds else None


@bp.post("/api/analyses/<run_id>/email/eml")
@api_errors
def create_analysis_eml(run_id: str):
    run = _analysis_run(run_id)
    payload, subject, text, kinds = _email_payload(run)
    eml, artifacts = prepare_eml(
        db(), run_id, run["workspace_id"], recipients=payload.get("recipients") or "",
        subject=subject, text=text, kinds=kinds,
    )
    return ok(
        eml=_public_artifact(eml), attachments=[_public_artifact(item) for item in artifacts],
        compatibility_note=".eml 包含真实 MIME 附件；mailto 仅可作为不含附件的文本回退。",
    ), 201


@bp.post("/api/analyses/<run_id>/email/send")
@api_errors
def send_analysis_email(run_id: str):
    run = _analysis_run(run_id)
    payload, subject, text, kinds = _email_payload(run)
    connector = require_workspace_record("connectors", str(payload.get("connector_id") or ""), run["workspace_id"])
    delivery = send_email(
        db(), run_id, run["workspace_id"], connector=connector,
        recipients=payload.get("recipients") or "", subject=subject, text=text, kinds=kinds,
        idempotency_key=str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or ""),
    )
    return ok(delivery=delivery)


@bp.delete("/api/artifacts/<artifact_id>")
@api_errors
def archive_artifact(artifact_id: str):
    item = require_workspace_record("artifacts", artifact_id)
    if item.get("run_id"):
        run = _analysis_run(str(item["run_id"]))
        _artifact_policy_access(item, action="delete", source_ids=run.get("source_scope") or [])
    else:
        _artifact_policy_access(item, action="delete")
    if not db().archive("artifacts", artifact_id):
        raise FileNotFoundError("成果不存在")
    return ok(archived=True)
