from __future__ import annotations

import pandas as pd
from flask import Blueprint, current_app, request, send_file

from ..services.analytics import clean_frame, profile
from ..services.datasets import (
    execute_query,
    preview_source,
    public_source,
    refresh_source,
    register_database,
    register_google_sheet,
    register_http,
    register_lark_table,
    register_upload,
    schema_for_source,
    source_table,
)
from ..services.knowledge import add_document, public_document, save_entry, search
from ..services.skills import get_skill, load_skills, public_skill, read_skill_resource
from .common import api_errors, body, db, ok, require_workspace_record, workspace_id


bp = Blueprint("catalog", __name__)


@bp.get("/api/sources")
def list_sources():
    return ok(items=[public_source(item) for item in db().list("sources", workspace_id=workspace_id())])


@bp.get("/api/source-sets")
def list_source_sets():
    return ok(items=db().list("source_sets", workspace_id=workspace_id()))


@bp.post("/api/source-sets")
@api_errors
def create_source_set():
    payload = body()
    source_ids = [str(item) for item in payload.get("source_ids", [])]
    if not payload.get("name") or not source_ids:
        raise ValueError("数据组合需要名称和至少一个数据源")
    wid = workspace_id()
    for source_id in source_ids:
        require_workspace_record("sources", source_id, wid)
    item = db().put(
        "source_sets",
        {"id": db().new_id("set"), "workspace_id": wid, "name": str(payload["name"])[:100], "description": str(payload.get("description") or "")[:500], "source_ids": source_ids},
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.patch("/api/source-sets/<set_id>")
@api_errors
def update_source_set(set_id: str):
    require_workspace_record("source_sets", set_id)
    if "source_ids" in body():
        for source_id in body()["source_ids"]:
            require_workspace_record("sources", str(source_id))
    return ok(item=db().patch("source_sets", set_id, {key: value for key, value in body().items() if key in {"name", "description", "source_ids"}}))


@bp.post("/api/source-sets/<set_id>/apply")
@api_errors
def apply_source_set(set_id: str):
    item = require_workspace_record("source_sets", set_id)
    session_id = str(body().get("session_id") or "")
    session_record = require_workspace_record("sessions", session_id)
    if session_record.get("workspace_id") != item.get("workspace_id"):
        raise ValueError("数据组合与会话不属于同一工作空间")
    session = db().patch("sessions", session_id, {"source_ids": item["source_ids"]})
    return ok(session=session, item=item)


@bp.delete("/api/source-sets/<set_id>")
@api_errors
def archive_source_set(set_id: str):
    require_workspace_record("source_sets", set_id)
    if not db().archive("source_sets", set_id):
        raise FileNotFoundError("数据组合不存在")
    return ok(archived=True)


@bp.post("/api/sources/upload")
@api_errors
def upload_source():
    files = request.files.getlist("files") or ([request.files["file"]] if "file" in request.files else [])
    if not files:
        raise ValueError("没有收到上传文件")
    wid = workspace_id()
    items = [public_source(register_upload(file, wid)) for file in files]
    return ok(items=items), 201


@bp.post("/api/sources/database")
@api_errors
def connect_database():
    item = register_database(body(), workspace_id())
    return ok(item=item), 201


@bp.post("/api/sources/http")
@api_errors
def connect_http():
    item = register_http(body(), workspace_id())
    return ok(item=item), 201


@bp.post("/api/sources/google-sheets")
@api_errors
def connect_google_sheets():
    return ok(item=register_google_sheet(body(), workspace_id())), 201


@bp.post("/api/sources/lark-table")
@api_errors
def connect_lark_table():
    return ok(item=register_lark_table(body(), workspace_id())), 201


@bp.get("/api/sources/<source_id>")
@api_errors
def get_source(source_id: str):
    return ok(item=public_source(require_workspace_record("sources", source_id)))


@bp.delete("/api/sources/<source_id>")
@api_errors
def archive_source(source_id: str):
    require_workspace_record("sources", source_id)
    if not db().archive("sources", source_id):
        raise FileNotFoundError("数据源不存在")
    return ok(archived=True)


@bp.post("/api/sources/<source_id>/refresh")
@api_errors
def refresh(source_id: str):
    return ok(item=public_source(refresh_source(require_workspace_record("sources", source_id))))


@bp.get("/api/sources/<source_id>/schema")
@api_errors
def source_schema(source_id: str):
    return ok(schema=schema_for_source(require_workspace_record("sources", source_id)))


@bp.get("/api/sources/<source_id>/preview")
@api_errors
def source_preview(source_id: str):
    limit = int(request.args.get("limit", "100"))
    return ok(preview=preview_source(require_workspace_record("sources", source_id), request.args.get("table"), min(limit, 500)))


@bp.get("/api/sources/<source_id>/profile")
@api_errors
def source_profile(source_id: str):
    _, frame = source_table(require_workspace_record("sources", source_id), request.args.get("table"))
    return ok(profile=profile(frame))


@bp.post("/api/sources/<source_id>/clean/preview")
@api_errors
def clean_preview(source_id: str):
    payload = body()
    _, frame = source_table(require_workspace_record("sources", source_id), payload.get("table"))
    cleaned, log = clean_frame(frame, payload.get("operations") or [])
    return ok(
        before=profile(frame),
        after=profile(cleaned),
        operations=log,
        preview={"columns": list(cleaned.columns), "data": cleaned.head(100).where(pd.notna(cleaned), None).to_dict(orient="records")},
    )


@bp.post("/api/sources/<source_id>/clean/apply")
@api_errors
def clean_apply(source_id: str):
    payload = body()
    source = require_workspace_record("sources", source_id)
    _, frame = source_table(source, payload.get("table"))
    cleaned, log = clean_frame(frame, payload.get("operations") or [])
    derived_id = db().new_id("src")
    target = current_app.config["SETTINGS"].upload_dir / f"{derived_id}.csv"
    cleaned.to_csv(target, index=False)
    item = db().put(
        "sources",
        {
            "id": derived_id,
            "workspace_id": source.get("workspace_id", workspace_id()),
            "name": str(payload.get("name") or f"{source['name']} · 清洗版")[:120],
            "kind": "derived",
            "format": "csv",
            "path": str(target),
            "parent_source_id": source_id,
            "lineage": {"operation": "clean", "steps": log},
            "tables": [{"name": "data", "source_name": "data", "rows": len(cleaned), "columns": len(cleaned.columns)}],
            "status": "ready",
        },
        workspace_id=source.get("workspace_id", workspace_id()),
    )
    return ok(item=public_source(item), operations=log), 201


@bp.post("/api/query")
@api_errors
def query():
    payload = body()
    source_ids = payload.get("source_ids") or ([payload["source_id"]] if payload.get("source_id") else [])
    if not source_ids:
        raise ValueError("请选择数据源")
    result = execute_query([str(item) for item in source_ids], str(payload.get("sql") or ""), workspace_id(), int(payload.get("limit", 1000)))
    public = {key: value for key, value in result.items() if key != "path"}
    return ok(result=public)


@bp.get("/api/query-results/<result_id>")
@api_errors
def get_query_result(result_id: str):
    item = require_workspace_record("query_results", result_id)
    item = {key: value for key, value in item.items() if key != "path"}
    return ok(result=item)


@bp.get("/api/knowledge/documents")
def list_documents():
    return ok(items=[public_document(item) for item in db().list("knowledge_documents", workspace_id=workspace_id())])


@bp.post("/api/knowledge/documents")
@api_errors
def upload_document():
    if "file" not in request.files:
        raise ValueError("请选择知识文档")
    tags_raw = request.form.get("tags", "")
    tags = [item.strip() for item in tags_raw.split(",") if item.strip()]
    wid = workspace_id()
    return ok(item=add_document(request.files["file"], wid, tags)), 201


@bp.patch("/api/knowledge/documents/<document_id>")
@api_errors
def update_document(document_id: str):
    require_workspace_record("knowledge_documents", document_id)
    allowed = {key: value for key, value in body().items() if key in {"name", "tags", "enabled"}}
    return ok(item=public_document(db().patch("knowledge_documents", document_id, allowed)))


@bp.delete("/api/knowledge/documents/<document_id>")
@api_errors
def archive_document(document_id: str):
    require_workspace_record("knowledge_documents", document_id)
    if not db().archive("knowledge_documents", document_id):
        raise FileNotFoundError("知识文档不存在")
    return ok(archived=True)


@bp.post("/api/knowledge/search")
@api_errors
def knowledge_search():
    query_text = str(body().get("query") or "").strip()
    if not query_text:
        raise ValueError("检索词不能为空")
    return ok(items=search(query_text, workspace_id(), int(body().get("limit", 6))))


def _public_knowledge_entry(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in {"tokens", "embedding"}}


@bp.get("/api/knowledge/entries")
def list_knowledge_entries():
    items = db().list("knowledge_entries", workspace_id=workspace_id())
    entry_type = request.args.get("type")
    if entry_type:
        items = [item for item in items if item.get("type") == entry_type]
    return ok(items=[_public_knowledge_entry(item) for item in items])


@bp.post("/api/knowledge/entries")
@api_errors
def create_knowledge_entry():
    return ok(item=_public_knowledge_entry(save_entry(body(), workspace_id()))), 201


@bp.patch("/api/knowledge/entries/<entry_id>")
@api_errors
def update_knowledge_entry(entry_id: str):
    require_workspace_record("knowledge_entries", entry_id)
    return ok(item=_public_knowledge_entry(save_entry(body(), workspace_id(), entry_id)))


@bp.delete("/api/knowledge/entries/<entry_id>")
@api_errors
def archive_knowledge_entry(entry_id: str):
    require_workspace_record("knowledge_entries", entry_id)
    if not db().archive("knowledge_entries", entry_id):
        raise FileNotFoundError("知识条目不存在")
    return ok(archived=True)


@bp.get("/api/knowledge/categories")
def list_knowledge_categories():
    items = db().list("knowledge_categories", workspace_id=workspace_id())
    if not items:
        items = [{"id": "default", "workspace_id": workspace_id(), "name": "默认业务", "enabled": True}]
    return ok(items=items)


@bp.post("/api/knowledge/categories")
@api_errors
def create_knowledge_category():
    name = str(body().get("name") or "").strip()
    if not name:
        raise ValueError("知识分类名称不能为空")
    wid = workspace_id()
    item = db().put(
        "knowledge_categories",
        {"id": db().new_id("kbcat"), "workspace_id": wid, "name": name[:100], "enabled": True},
        workspace_id=wid,
    )
    return ok(item=item), 201


DEFAULT_SKILLS = [
    {"id": "executive-summary", "name": "经营摘要", "description": "提炼变化、原因、风险和建议", "instruction": "按结论、证据、风险、行动建议四段输出。", "enabled": True},
    {"id": "quality-audit", "name": "数据质量审计", "description": "检查缺失、重复、异常和类型问题", "instruction": "先量化质量问题，再给出不破坏原始数据的处理建议。", "enabled": True},
    {"id": "trend-diagnosis", "name": "趋势诊断", "description": "识别趋势、季节性和突变", "instruction": "比较环比与同比，标注异常点和可能原因。", "enabled": True},
]


def _skills(wid: str) -> list[dict]:
    loaded, _ = load_skills(wid)
    names = {item.get("id") for item in loaded}
    return [*loaded, *(item for item in DEFAULT_SKILLS if item["id"] not in names)]


@bp.get("/api/skills")
def list_skills():
    wid = workspace_id()
    loaded, diagnostics = load_skills(wid)
    names = {item.get("id") for item in loaded}
    compatibility = [item for item in DEFAULT_SKILLS if item["id"] not in names]
    items = [public_skill(item) for item in loaded] + compatibility
    return ok(items=items, skills=items, diagnostics=diagnostics)


@bp.get("/api/skills/<skill_id>")
@api_errors
def get_skill_detail(skill_id: str):
    skill = get_skill(skill_id, workspace_id())
    if not skill:
        skill = next((item for item in DEFAULT_SKILLS if item["id"] == skill_id), None)
    if not skill:
        raise FileNotFoundError("Skill 不存在")
    item = public_skill(skill, include_prompt=True)
    return ok(item=item, skill={**item, "raw": item.get("instruction", "")})


@bp.post("/api/skills/reload")
def reload_skills():
    loaded, diagnostics = load_skills(workspace_id())
    return ok(items=[public_skill(item) for item in loaded], diagnostics=diagnostics)


@bp.get("/api/skills/<skill_id>/resources/<path:resource_path>")
@api_errors
def skill_resource(skill_id: str, resource_path: str):
    path, _suffix = read_skill_resource(skill_id, resource_path, workspace_id())
    return send_file(path, as_attachment=request.args.get("download") == "true", download_name=path.name)


@bp.post("/api/skills")
@api_errors
def create_skill():
    payload = body()
    instruction = payload.get("instruction") or payload.get("prompt")
    if not payload.get("name") or not instruction:
        raise ValueError("技能名称和执行指令不能为空")
    wid = workspace_id()
    item = db().put(
        "skills",
        {
            "id": db().new_id("skill"), "workspace_id": wid,
            "name": str(payload["name"])[:100], "description": str(payload.get("description") or "")[:500],
            "instruction": str(instruction)[:50000], "input_schema": payload.get("input_schema", {}),
            "slug": str(payload.get("slug") or payload["name"])[:100],
            "allowed_tools": payload.get("allowed_tools", []), "resources": payload.get("resources", []),
            "enabled": bool(payload.get("enabled", True)),
        },
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.patch("/api/skills/<skill_id>")
@api_errors
def update_skill(skill_id: str):
    if skill_id in {item["id"] for item in DEFAULT_SKILLS}:
        raise ValueError("内置技能不能修改")
    require_workspace_record("skills", skill_id)
    return ok(item=db().patch("skills", skill_id, body()))


@bp.delete("/api/skills/<skill_id>")
@api_errors
def archive_skill(skill_id: str):
    require_workspace_record("skills", skill_id)
    if not db().archive("skills", skill_id):
        raise FileNotFoundError("技能不存在")
    return ok(archived=True)


@bp.get("/api/commands")
def commands():
    return ok(items=[
        {"name": "checkpoint", "aliases": ["cp"], "description": "查看对话快照和文件历史", "usage": "/checkpoint"},
        {"name": "clear", "description": "清除当前对话，保留数据源和工作区", "usage": "/clear"},
        {"name": "compact", "aliases": ["c"], "description": "压缩当前上下文", "usage": "/compact"},
        {"name": "data", "description": "打开当前数据源和表预览", "usage": "/data"},
        {"name": "help", "aliases": ["h", "?"], "description": "查看可用命令", "usage": "/help [命令]"},
        {"name": "instruction", "aliases": ["i"], "description": "设置当前会话临时指令", "usage": "/instruction [指令]"},
        {"name": "jobs", "description": "打开任务历史和运行状态", "usage": "/jobs"},
        {"name": "knowledge", "aliases": ["kb"], "description": "打开业务知识库", "usage": "/knowledge"},
        {"name": "mcp", "description": "打开 MCP 连接与工具管理", "usage": "/mcp"},
        {"name": "new", "aliases": ["n"], "description": "新建一个干净分析会话", "usage": "/new [会话名]"},
        {"name": "robot", "aliases": ["bot"], "description": "打开飞书机器人连接", "usage": "/robot"},
        {"name": "sessions", "aliases": ["session"], "description": "管理已保存对话", "usage": "/sessions [new]"},
        {"name": "skills", "aliases": ["sk"], "description": "查看、选择或刷新分析 Skill", "usage": "/skills"},
        {"name": "status", "aliases": ["s"], "description": "查看模型、数据源和上下文状态", "usage": "/status"},
        {"name": "stop", "description": "停止当前正在生成的回复", "usage": "/stop"},
        {"name": "teams", "description": "打开分析团队和沟通记录", "usage": "/teams"},
        {"name": "workspace", "aliases": ["ws"], "description": "管理工作目录和权限", "usage": "/workspace"},
    ])
