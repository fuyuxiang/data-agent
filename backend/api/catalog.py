from __future__ import annotations

import pandas as pd
from flask import Blueprint, current_app, request, send_file

from ..services.analytics import clean_frame, profile
from ..services.authorization import filter_authorized_sources, inherited_source_policy
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
from ..services.saas import assert_collection_limit, assert_feature_enabled, assert_limit_available
from ..services.semantic import (
    compile_metric_query, execute_metric_query, save_metric, save_model, visible_metrics,
)
from ..services.skills import get_skill, load_skills, public_skill, read_skill_resource
from .common import (
    api_errors, body, current_user_id, db, ok, require_workspace_access,
    require_query_result_access, require_session_access, require_source_access,
    require_workspace_record, workspace_id,
)


bp = Blueprint("catalog", __name__)


def _source_set(set_id: str) -> dict:
    item = require_workspace_record("source_sets", set_id)
    for source_id in item.get("source_ids") or []:
        require_source_access(str(source_id), item["workspace_id"])
    return item


@bp.get("/api/sources")
def list_sources():
    wid = workspace_id()
    page = db().page(
        "sources", workspace_id=wid, limit=int(request.args.get("limit", 100)),
        cursor=str(request.args.get("cursor") or ""), search=str(request.args.get("q") or ""),
        category=str(request.args.get("category") or ""),
    )
    visible = filter_authorized_sources(
        db(), page["items"], workspace_id=wid, actor_id=current_user_id(),
    )
    return ok(items=[public_source(item) for item in visible], next_cursor=page["next_cursor"])


@bp.get("/api/source-sets")
def list_source_sets():
    items = []
    for item in db().list("source_sets", workspace_id=workspace_id()):
        try:
            for source_id in item.get("source_ids") or []:
                require_source_access(str(source_id), item["workspace_id"])
        except (FileNotFoundError, PermissionError):
            continue
        items.append(item)
    return ok(items=items)


@bp.post("/api/source-sets")
@api_errors
def create_source_set():
    payload = body()
    source_ids = [str(item) for item in payload.get("source_ids", [])]
    if not payload.get("name") or not source_ids:
        raise ValueError("数据组合需要名称和至少一个数据源")
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    for source_id in source_ids:
        require_source_access(source_id, wid)
    item = db().put(
        "source_sets",
        {"id": db().new_id("set"), "workspace_id": wid, "name": str(payload["name"])[:100], "description": str(payload.get("description") or "")[:500], "source_ids": source_ids},
        workspace_id=wid,
    )
    return ok(item=item), 201


@bp.patch("/api/source-sets/<set_id>")
@api_errors
def update_source_set(set_id: str):
    item = _source_set(set_id)
    assert_feature_enabled(db(), item["workspace_id"], "data_sources")
    if "source_ids" in body():
        for source_id in body()["source_ids"]:
            require_source_access(str(source_id))
    return ok(item=db().patch(
        "source_sets", set_id,
        {key: value for key, value in body().items() if key in {"name", "description", "source_ids"}},
        workspace_id=item["workspace_id"],
    ))


@bp.post("/api/source-sets/<set_id>/apply")
@api_errors
def apply_source_set(set_id: str):
    item = _source_set(set_id)
    assert_feature_enabled(db(), item["workspace_id"], "data_sources")
    session_id = str(body().get("session_id") or "")
    session_record = require_session_access(session_id)
    if session_record.get("workspace_id") != item.get("workspace_id"):
        raise ValueError("数据组合与会话不属于同一工作空间")
    session = db().patch("sessions", session_id, {"source_ids": item["source_ids"]})
    return ok(session=session, item=item)


@bp.delete("/api/source-sets/<set_id>")
@api_errors
def archive_source_set(set_id: str):
    _source_set(set_id)
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
    assert_feature_enabled(db(), wid, "data_sources")
    assert_collection_limit(db(), wid, limit_key="sources", collection="sources", adding=len(files))
    items = [public_source(register_upload(file, wid)) for file in files]
    return ok(items=items), 201


@bp.post("/api/sources/database")
@api_errors
def connect_database():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    assert_collection_limit(db(), wid, limit_key="sources", collection="sources")
    item = register_database(body(), wid)
    return ok(item=item), 201


@bp.post("/api/sources/http")
@api_errors
def connect_http():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    assert_collection_limit(db(), wid, limit_key="sources", collection="sources")
    item = register_http(body(), wid)
    return ok(item=item), 201


@bp.post("/api/sources/google-sheets")
@api_errors
def connect_google_sheets():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    assert_collection_limit(db(), wid, limit_key="sources", collection="sources")
    return ok(item=register_google_sheet(body(), wid)), 201


@bp.post("/api/sources/lark-table")
@api_errors
def connect_lark_table():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    assert_collection_limit(db(), wid, limit_key="sources", collection="sources")
    return ok(item=register_lark_table(body(), wid)), 201


@bp.get("/api/sources/<source_id>")
@api_errors
def get_source(source_id: str):
    return ok(item=public_source(require_source_access(source_id)))


@bp.patch("/api/sources/<source_id>")
@api_errors
def update_source(source_id: str):
    source = require_source_access(source_id, action="update")
    payload = body()
    allowed = {
        key: payload[key]
        for key in ("name", "description", "classification", "sensitivity", "retention_policy")
        if key in payload
    }
    if "authorized_user_ids" in payload:
        require_workspace_access(source["workspace_id"], owner=True)
        values = payload["authorized_user_ids"]
        if values is not None and not isinstance(values, list):
            raise ValueError("authorized_user_ids 必须是用户 ID 数组或 null")
        if isinstance(values, list):
            member_ids = {
                str(item.get("user_id"))
                for item in db().list("workspace_members", workspace_id=source["workspace_id"], limit=5000)
                if item.get("enabled", True)
            }
            if not db().list("users", include_archived=True, limit=1):
                member_ids.add("local-default")
            normalized = list(dict.fromkeys(str(value) for value in values if str(value)))
            if any(value not in member_ids for value in normalized):
                raise ValueError("数据源授权用户必须是当前工作空间成员")
            if current_user_id() not in normalized:
                raise ValueError("不能在本次操作中移除自己的数据源访问权限")
            allowed["authorized_user_ids"] = normalized
        else:
            allowed["authorized_user_ids"] = None
    if "name" in allowed:
        allowed["name"] = str(allowed["name"]).strip()[:120]
        if not allowed["name"]:
            raise ValueError("数据源名称不能为空")
    item = db().patch("sources", source_id, allowed, workspace_id=source["workspace_id"])
    db().audit(
        "source.updated", workspace_id=source["workspace_id"], actor=current_user_id(),
        object_type="source", object_id=source_id, detail={"fields": sorted(allowed)},
    )
    return ok(item=public_source(item or source))


@bp.delete("/api/sources/<source_id>")
@api_errors
def archive_source(source_id: str):
    require_source_access(source_id, action="delete")
    if not db().archive("sources", source_id):
        raise FileNotFoundError("数据源不存在")
    return ok(archived=True)


@bp.post("/api/sources/<source_id>/refresh")
@api_errors
def refresh(source_id: str):
    source = require_source_access(source_id, action="refresh")
    assert_feature_enabled(db(), source["workspace_id"], "data_sources")
    return ok(item=public_source(refresh_source(source)))


@bp.get("/api/sources/<source_id>/schema")
@api_errors
def source_schema(source_id: str):
    return ok(schema=schema_for_source(require_source_access(source_id)))


@bp.get("/api/sources/<source_id>/preview")
@api_errors
def source_preview(source_id: str):
    limit = int(request.args.get("limit", "100"))
    return ok(preview=preview_source(require_source_access(source_id), request.args.get("table"), min(limit, 500)))


@bp.get("/api/sources/<source_id>/profile")
@api_errors
def source_profile(source_id: str):
    _, frame = source_table(require_source_access(source_id), request.args.get("table"))
    return ok(profile=profile(frame))


@bp.post("/api/sources/<source_id>/clean/preview")
@api_errors
def clean_preview(source_id: str):
    payload = body()
    source = require_source_access(source_id, action="analyze")
    assert_feature_enabled(db(), source["workspace_id"], "data_sources")
    _, frame = source_table(source, payload.get("table"))
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
    source = require_source_access(source_id, action="analyze")
    wid = source.get("workspace_id", workspace_id())
    assert_feature_enabled(db(), wid, "data_sources")
    assert_collection_limit(db(), wid, limit_key="sources", collection="sources")
    _, frame = source_table(source, payload.get("table"))
    cleaned, log = clean_frame(frame, payload.get("operations") or [])
    derived_id = db().new_id("src")
    target = current_app.config["SETTINGS"].upload_dir / f"{derived_id}.csv"
    cleaned.to_csv(target, index=False)
    item = db().put(
        "sources",
        {
            "id": derived_id,
            "workspace_id": wid,
            "name": str(payload.get("name") or f"{source['name']} · 清洗版")[:120],
            "kind": "derived",
            "format": "csv",
            "path": str(target),
            "parent_source_id": source_id,
            "lineage": {"operation": "clean", "steps": log},
            "tables": [{"name": "data", "source_name": "data", "rows": len(cleaned), "columns": len(cleaned.columns)}],
            "status": "ready",
            **inherited_source_policy(source),
        },
        workspace_id=wid,
    )
    return ok(item=public_source(item), operations=log), 201


@bp.post("/api/query")
@api_errors
def query():
    payload = body()
    assert_feature_enabled(db(), workspace_id(), "data_sources")
    source_ids = payload.get("source_ids") or ([payload["source_id"]] if payload.get("source_id") else [])
    if not source_ids:
        raise ValueError("请选择数据源")
    result = execute_query(
        [str(item) for item in source_ids], str(payload.get("sql") or ""), workspace_id(),
        int(payload.get("limit", 1000)), actor_id=current_user_id(),
    )
    public = {key: value for key, value in result.items() if key != "path"}
    return ok(result=public)


@bp.get("/api/query-results/<result_id>")
@api_errors
def get_query_result(result_id: str):
    item = require_query_result_access(result_id)
    item = {key: value for key, value in item.items() if key != "path"}
    return ok(result=item)


@bp.get("/api/semantic/models")
def list_semantic_models():
    wid = workspace_id()
    items = []
    for item in db().list("semantic_models", workspace_id=wid, limit=5000):
        try:
            require_source_access(str(item.get("source_id") or ""), wid)
        except (FileNotFoundError, PermissionError):
            continue
        items.append(item)
    return ok(items=items)


@bp.post("/api/semantic/models")
@api_errors
def create_semantic_model():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "semantic_layer")
    return ok(item=save_model(db(), body(), wid, current_user_id())), 201


@bp.patch("/api/semantic/models/<model_id>")
@api_errors
def update_semantic_model(model_id: str):
    current = require_workspace_record("semantic_models", model_id)
    require_source_access(str(current.get("source_id") or ""), current["workspace_id"], action="update")
    has_approved_metrics = any(
        item.get("model_id") == model_id and item.get("status") == "approved"
        for item in db().list("semantic_metrics", workspace_id=current["workspace_id"], limit=5000)
    )
    if has_approved_metrics:
        require_workspace_access(current["workspace_id"], owner=True)
    return ok(item=save_model(db(), body(), current["workspace_id"], current_user_id(), model_id))


@bp.delete("/api/semantic/models/<model_id>")
@api_errors
def archive_semantic_model(model_id: str):
    current = require_workspace_record("semantic_models", model_id)
    require_source_access(str(current.get("source_id") or ""), current["workspace_id"], action="delete")
    referenced = [
        item for item in db().list("semantic_metrics", workspace_id=current["workspace_id"], limit=5000)
        if item.get("model_id") == model_id
    ]
    if referenced:
        raise ValueError("语义模型仍被指标引用，请先删除或迁移这些指标")
    if not db().archive("semantic_models", model_id):
        raise FileNotFoundError("语义模型不存在")
    return ok(archived=True)


@bp.get("/api/semantic/metrics")
def list_semantic_metrics():
    return ok(items=visible_metrics(db(), workspace_id(), current_user_id()))


@bp.post("/api/semantic/metrics")
@api_errors
def create_semantic_metric():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "semantic_layer")
    assert_collection_limit(db(), wid, limit_key="semantic_metrics", collection="semantic_metrics")
    if str(body().get("status") or "draft") == "approved":
        require_workspace_access(wid, owner=True)
    return ok(item=save_metric(db(), body(), wid, current_user_id())), 201


@bp.patch("/api/semantic/metrics/<metric_id>")
@api_errors
def update_semantic_metric(metric_id: str):
    current = require_workspace_record("semantic_metrics", metric_id)
    if current.get("status") == "approved" or str(body().get("status") or "") == "approved":
        require_workspace_access(current["workspace_id"], owner=True)
    return ok(item=save_metric(db(), body(), current["workspace_id"], current_user_id(), metric_id))


@bp.delete("/api/semantic/metrics/<metric_id>")
@api_errors
def archive_semantic_metric(metric_id: str):
    current = require_workspace_record("semantic_metrics", metric_id)
    require_workspace_access(current["workspace_id"], owner=True)
    if not db().archive("semantic_metrics", metric_id):
        raise FileNotFoundError("语义指标不存在")
    return ok(archived=True)


@bp.post("/api/semantic/compile")
@api_errors
def compile_semantic_metric():
    assert_feature_enabled(db(), workspace_id(), "semantic_layer")
    return ok(plan=compile_metric_query(db(), body(), workspace_id(), current_user_id()))


@bp.post("/api/semantic/query")
@api_errors
def query_semantic_metric():
    assert_feature_enabled(db(), workspace_id(), "semantic_layer")
    output = execute_metric_query(db(), body(), workspace_id(), current_user_id())
    result = {key: value for key, value in output["result"].items() if key != "path"}
    return ok(plan=output["plan"], result=result)


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
    assert_feature_enabled(db(), wid, "knowledge_base")
    assert_limit_available(
        db(), wid, limit_key="knowledge_entries",
        current_count=len(db().list("knowledge_documents", workspace_id=wid, limit=5000))
        + len(db().list("knowledge_entries", workspace_id=wid, limit=5000)),
    )
    return ok(item=add_document(request.files["file"], wid, tags)), 201


@bp.patch("/api/knowledge/documents/<document_id>")
@api_errors
def update_document(document_id: str):
    document = require_workspace_record("knowledge_documents", document_id)
    assert_feature_enabled(db(), document["workspace_id"], "knowledge_base")
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
    assert_feature_enabled(db(), workspace_id(), "knowledge_base")
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
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "knowledge_base")
    assert_limit_available(
        db(), wid, limit_key="knowledge_entries",
        current_count=len(db().list("knowledge_documents", workspace_id=wid, limit=5000))
        + len(db().list("knowledge_entries", workspace_id=wid, limit=5000)),
    )
    return ok(item=_public_knowledge_entry(save_entry(body(), wid))), 201


@bp.patch("/api/knowledge/entries/<entry_id>")
@api_errors
def update_knowledge_entry(entry_id: str):
    entry = require_workspace_record("knowledge_entries", entry_id)
    assert_feature_enabled(db(), entry["workspace_id"], "knowledge_base")
    return ok(item=_public_knowledge_entry(save_entry(body(), entry["workspace_id"], entry_id)))


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
    assert_feature_enabled(db(), wid, "knowledge_base")
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
    known = {str(item.get("id")) for item in items}
    items.extend(
        public_skill(item) for item in db().list("skills", workspace_id=wid, limit=5000)
        if str(item.get("id")) not in known
    )
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
            "enabled": True, "status": "candidate", "version": 1,
            "created_by": current_user_id(), "approved_by": None,
        },
        workspace_id=wid,
    )
    db().put("skill_versions", {
        "id": db().new_id("skillver"), "workspace_id": wid, "skill_id": item["id"],
        "version": 1, "payload": item, "status": "candidate", "created_by": current_user_id(),
    }, workspace_id=wid)
    return ok(item=item), 201


@bp.patch("/api/skills/<skill_id>")
@api_errors
def update_skill(skill_id: str):
    if skill_id in {item["id"] for item in DEFAULT_SKILLS}:
        raise ValueError("内置技能不能修改")
    current = require_workspace_record("skills", skill_id)
    payload = body()
    allowed = {
        key: payload[key] for key in (
            "name", "description", "instruction", "input_schema", "slug", "allowed_tools", "resources",
        ) if key in payload
    }
    version = int(current.get("version") or 1) + 1
    item = db().patch("skills", skill_id, {
        **allowed, "version": version, "status": "candidate", "approved_by": None,
        "approval_at": None, "published_at": None,
    }, workspace_id=current["workspace_id"])
    db().put("skill_versions", {
        "id": db().new_id("skillver"), "workspace_id": current["workspace_id"],
        "skill_id": skill_id, "version": version, "payload": item,
        "status": "candidate", "created_by": current_user_id(),
    }, workspace_id=current["workspace_id"])
    return ok(item=item)


@bp.post("/api/skills/<skill_id>/evaluate")
@api_errors
def evaluate_skill(skill_id: str):
    skill = require_workspace_record("skills", skill_id)
    cases = body().get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 50:
        raise ValueError("Skill 评估需要 1–50 个确定性用例")
    from ..services.advanced_agent import FORMAL_AGENT_TOOLS

    allowed = set(str(value) for value in skill.get("allowed_tools") or [])
    issues = []
    results = []
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict) or not str(case.get("input") or "").strip():
            issues.append(f"第 {index} 个用例缺少 input")
            continue
        required = set(str(value) for value in case.get("required_tools") or [])
        forbidden = set(str(value) for value in case.get("forbidden_tools") or [])
        unknown = required - FORMAL_AGENT_TOOLS
        missing = required - allowed if allowed else set()
        conflict = forbidden & allowed
        passed = not (unknown or missing or conflict)
        results.append({
            "index": index, "passed": passed, "required_tools": sorted(required),
            "unknown_tools": sorted(unknown), "missing_allowed_tools": sorted(missing),
            "forbidden_exposed_tools": sorted(conflict),
        })
        if not passed:
            issues.append(f"第 {index} 个用例的工具权限合同失败")
    if not str(skill.get("instruction") or "").strip():
        issues.append("Skill 指令为空")
    status = "PASS" if len(results) == len(cases) and not issues else "FAIL"
    evaluation = db().put("skill_evaluations", {
        "id": db().new_id("skilleval"), "workspace_id": skill["workspace_id"],
        "skill_id": skill_id, "skill_version": int(skill.get("version") or 1),
        "status": status, "results": results, "issues": issues, "evaluated_by": current_user_id(),
    }, workspace_id=skill["workspace_id"])
    db().patch("skills", skill_id, {
        "status": "tested" if status == "PASS" else "candidate",
        "latest_evaluation_id": evaluation["id"], "latest_evaluation_status": status,
    }, workspace_id=skill["workspace_id"])
    return ok(item=evaluation)


@bp.post("/api/skills/<skill_id>/publish")
@api_errors
def publish_skill(skill_id: str):
    skill = require_workspace_record("skills", skill_id)
    require_workspace_access(skill["workspace_id"], owner=True)
    evaluation = db().get(
        "skill_evaluations", str(skill.get("latest_evaluation_id") or ""),
        workspace_id=skill["workspace_id"],
    )
    if not evaluation or evaluation.get("status") != "PASS" or int(evaluation.get("skill_version") or 0) != int(skill.get("version") or 1):
        raise ValueError("当前 Skill 版本未通过测试，不能发布")
    from ..core.database import utcnow

    item = db().patch("skills", skill_id, {
        "status": "published", "approved_by": current_user_id(),
        "approval_at": utcnow(), "published_at": utcnow(), "enabled": True,
    }, workspace_id=skill["workspace_id"])
    return ok(item=item)


@bp.post("/api/skills/<skill_id>/deprecate")
@api_errors
def deprecate_skill(skill_id: str):
    skill = require_workspace_record("skills", skill_id)
    require_workspace_access(skill["workspace_id"], owner=True)
    return ok(item=db().patch(
        "skills", skill_id, {"status": "deprecated", "enabled": False},
        workspace_id=skill["workspace_id"],
    ))


@bp.post("/api/skills/<skill_id>/rollback")
@api_errors
def rollback_skill(skill_id: str):
    skill = require_workspace_record("skills", skill_id)
    require_workspace_access(skill["workspace_id"], owner=True)
    target = int(body().get("version") or 0)
    version = next((
        item for item in db().list("skill_versions", workspace_id=skill["workspace_id"], limit=5000)
        if item.get("skill_id") == skill_id and int(item.get("version") or 0) == target
    ), None)
    if not version:
        raise FileNotFoundError("Skill 历史版本不存在")
    restored = dict(version.get("payload") or {})
    next_version = int(skill.get("version") or 1) + 1
    allowed = {key: restored.get(key) for key in (
        "name", "description", "instruction", "input_schema", "slug", "allowed_tools", "resources",
    )}
    item = db().patch("skills", skill_id, {
        **allowed, "version": next_version, "status": "candidate", "enabled": True,
        "approved_by": None, "latest_evaluation_id": None, "latest_evaluation_status": None,
        "rolled_back_from": target,
    }, workspace_id=skill["workspace_id"])
    db().put("skill_versions", {
        "id": db().new_id("skillver"), "workspace_id": skill["workspace_id"],
        "skill_id": skill_id, "version": next_version, "payload": item,
        "status": "candidate", "created_by": current_user_id(), "rolled_back_from": target,
    }, workspace_id=skill["workspace_id"])
    return ok(item=item)


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
