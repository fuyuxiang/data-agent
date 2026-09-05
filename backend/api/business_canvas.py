from __future__ import annotations

from flask import Blueprint, request

from ..services import business_canvas as canvas
from .common import api_errors, body, db, ok, require_workspace_record, workspace_id


bp = Blueprint("business_canvas", __name__)


def _workspace_for_session(session_id: str) -> str:
    session = require_workspace_record("sessions", session_id)
    return str(session.get("workspace_id") or workspace_id())


@bp.get("/api/session/<session_id>/business-canvas/templates")
@api_errors
def templates(session_id: str):
    _workspace_for_session(session_id)
    return ok(templates=canvas.list_templates())


@bp.get("/api/session/<session_id>/business-canvas/projects")
@api_errors
def projects(session_id: str):
    wid = _workspace_for_session(session_id)
    return ok(projects=canvas.list_projects(db(), wid))


@bp.post("/api/session/<session_id>/business-canvas/projects")
@api_errors
def create_project(session_id: str):
    wid = _workspace_for_session(session_id)
    payload = body()
    project = canvas.create_project(
        db(), workspace_id=wid, session_id=session_id,
        template_id=str(payload.get("template_id") or ""),
        title=str(payload.get("title") or ""),
    )
    return ok(project=project), 201


@bp.get("/api/session/<session_id>/business-canvas/projects/<project_id>")
@api_errors
def get_project(session_id: str, project_id: str):
    wid = _workspace_for_session(session_id)
    return ok(project=canvas.get_project(db(), project_id, wid))


@bp.patch("/api/session/<session_id>/business-canvas/projects/<project_id>")
@api_errors
def update_project(session_id: str, project_id: str):
    wid = _workspace_for_session(session_id)
    return ok(project=canvas.update_title(db(), project_id, wid, str(body().get("title") or "")))


@bp.delete("/api/session/<session_id>/business-canvas/projects/<project_id>")
@api_errors
def delete_project(session_id: str, project_id: str):
    wid = _workspace_for_session(session_id)
    canvas.delete_project(db(), project_id, wid)
    return ok(archived=True)


@bp.patch("/api/session/<session_id>/business-canvas/projects/<project_id>/blocks/<block_key>")
@api_errors
def update_block(session_id: str, project_id: str, block_key: str):
    wid = _workspace_for_session(session_id)
    payload = body()
    project = canvas.update_block(
        db(), project_id, wid, block_key, payload.get("content"),
        actor_type=str(payload.get("actor_type") or "user"),
        actor_label=str(payload.get("actor_label") or "user"),
        reason=str(payload.get("reason") or "用户编辑画布模块"),
    )
    return ok(project=project)


@bp.get("/api/session/<session_id>/business-canvas/projects/<project_id>/revisions")
@api_errors
def revisions(session_id: str, project_id: str):
    wid = _workspace_for_session(session_id)
    return ok(revisions=canvas.list_revisions(db(), project_id, wid))


@bp.get("/api/session/<session_id>/business-canvas/projects/<project_id>/diagram")
@api_errors
def get_diagram(session_id: str, project_id: str):
    wid = _workspace_for_session(session_id)
    return ok(diagram_xml=canvas.get_project(db(), project_id, wid).get("diagram_xml", ""))


@bp.patch("/api/session/<session_id>/business-canvas/projects/<project_id>/diagram")
@api_errors
def update_diagram(session_id: str, project_id: str):
    wid = _workspace_for_session(session_id)
    payload = body()
    project, fixes = canvas.update_diagram(
        db(), project_id, wid, str(payload.get("diagram_xml") or ""),
        actor_type=str(payload.get("actor_type") or "user"),
        reason=str(payload.get("reason") or "更新 draw.io 图表"),
    )
    return ok(project=project, fixes_applied=fixes)


@bp.patch("/api/session/<session_id>/business-canvas/projects/<project_id>/rendering-mode")
@api_errors
def rendering_mode(session_id: str, project_id: str):
    wid = _workspace_for_session(session_id)
    project = canvas.update_rendering_mode(
        db(), project_id, wid, str(body().get("rendering_mode") or "card"),
    )
    return ok(project=project)


@bp.get("/api/session/<session_id>/business-canvas/shape-libraries")
@api_errors
def shape_libraries(session_id: str):
    _workspace_for_session(session_id)
    name = str(request.args.get("library") or "")
    if name:
        return ok(**canvas.get_shape_library(name))
    return ok(items=sorted(path.stem for path in canvas.SHAPE_LIBS_DIR.glob("*.md")))
