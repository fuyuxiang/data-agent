from __future__ import annotations

from flask import Blueprint, current_app, request

from ..services.authorization import filter_authorized_jobs
from ..services.jobs import get_job_manager
from .common import api_errors, current_user_id, db, ok, require_job_access, workspace_id


bp = Blueprint("jobs", __name__)


@bp.get("/api/jobs")
def list_jobs():
    wid = workspace_id()
    items = filter_authorized_jobs(
        db(),
        db().list("jobs", workspace_id=wid, limit=int(request.args.get("limit", "200"))),
        workspace_id=wid,
        actor_id=current_user_id(),
    )
    if request.args.get("active") == "true":
        items = [item for item in items if item.get("status") in {"queued", "running", "waiting_approval"}]
    return ok(items=items)


@bp.get("/api/jobs/events")
def job_events():
    wid = workspace_id()
    allowed = {
        item["id"]
        for item in filter_authorized_jobs(
            db(),
            db().list("jobs", workspace_id=wid, limit=5000),
            workspace_id=wid,
            actor_id=current_user_id(),
        )
    }
    items = db().job_events(int(request.args.get("after", "0")), int(request.args.get("limit", "500")))
    return ok(items=[item for item in items if item.get("job_id") in allowed])


@bp.get("/api/jobs/<job_id>")
@api_errors
def get_job(job_id: str):
    return ok(item=require_job_access(job_id))


@bp.post("/api/jobs/<job_id>/cancel")
@api_errors
def cancel_job(job_id: str):
    require_job_access(job_id)
    accepted = get_job_manager(current_app._get_current_object()).cancel(job_id)
    return ok(accepted=accepted)


@bp.delete("/api/jobs/completed")
def clear_jobs():
    count = 0
    wid = workspace_id()
    items = filter_authorized_jobs(
        db(),
        db().list("jobs", workspace_id=wid),
        workspace_id=wid,
        actor_id=current_user_id(),
    )
    for item in items:
        if item.get("status") in {"completed", "failed", "cancelled"} and db().archive("jobs", item["id"]):
            count += 1
    return ok(archived=count)
