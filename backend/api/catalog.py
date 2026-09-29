from __future__ import annotations

import pandas as pd
from flask import Blueprint, current_app, request

from ..services.analytics import clean_frame, profile
from ..services.authorization import actor_role, filter_authorized_sources, inherited_source_policy
from ..services.data_policy import normalize_policies
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
from ..services.product import assert_feature_enabled
from ..services.semantic import (
    compile_metric_query, execute_metric_query, save_metric, save_model, visible_metrics,
)
from ..services.skills import DEFAULT_SKILLS, get_skill, public_skill
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


def _remove_source_from_scopes(source_id: str, workspace_id: str) -> dict[str, int]:
    counters = {"sessions": 0, "source_sets": 0}
    for collection in counters:
        for item in db().list(collection, workspace_id=workspace_id, limit=5000):
            source_ids = [str(value) for value in item.get("source_ids") or []]
            if source_id not in source_ids:
                continue
            next_ids = [value for value in source_ids if value != source_id]
            db().patch(collection, item["id"], {"source_ids": next_ids}, workspace_id=workspace_id)
            counters[collection] += 1
    return counters


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
    items = [public_source(register_upload(file, wid)) for file in files]
    return ok(items=items), 201


@bp.post("/api/sources/database")
@api_errors
def connect_database():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    item = register_database(body(), wid)
    return ok(item=item), 201


@bp.post("/api/sources/http")
@api_errors
def connect_http():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    item = register_http(body(), wid)
    return ok(item=item), 201


@bp.post("/api/sources/google-sheets")
@api_errors
def connect_google_sheets():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    return ok(item=register_google_sheet(body(), wid)), 201


@bp.post("/api/sources/lark-table")
@api_errors
def connect_lark_table():
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "data_sources")
    return ok(item=register_lark_table(body(), wid)), 201


@bp.get("/api/sources/<source_id>")
@api_errors
def get_source(source_id: str):
    source = require_source_access(source_id)
    owner = actor_role(db(), source["workspace_id"], current_user_id()) == "owner"
    return ok(item=public_source(source, include_policy=owner))


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
    if "row_policy" in payload or "column_policies" in payload:
        require_workspace_access(source["workspace_id"], owner=True)
        if source.get("kind") == "warehouse":
            raise ValueError("数仓行列权限须由目标仓身份或安全视图执行，当前连接不接受本地规则")
        policy_source = {**source, "row_policy": None, "column_policies": None}
        rows, columns = normalize_policies(
            payload.get("row_policy", source.get("row_policy")),
            payload.get("column_policies", source.get("column_policies")),
            schema_for_source(policy_source),
        )
        allowed["row_policy"] = rows
        allowed["column_policies"] = columns
    if "analysis_tables" in payload:
        if source.get("kind") != "database":
            raise ValueError("仅数据库数据源支持设置分析表范围")
        values = payload["analysis_tables"]
        if not isinstance(values, list):
            raise ValueError("analysis_tables 必须是表名数组")
        available = {
            str(alias): str(table.get("source_name") or table.get("name") or "")
            for table in source.get("tables") or []
            for alias in (table.get("name"), table.get("source_name"))
            if alias
        }
        requested = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        unknown = [value for value in requested if value not in available]
        if unknown:
            raise ValueError(f"数据表不存在或不在当前连接中：{', '.join(unknown[:5])}")
        allowed["analysis_tables"] = list(dict.fromkeys(available[value] for value in requested))
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
    owner = actor_role(db(), source["workspace_id"], current_user_id()) == "owner"
    return ok(item=public_source(item or source, include_policy=owner))


@bp.delete("/api/sources/<source_id>")
@api_errors
def archive_source(source_id: str):
    source = require_source_access(source_id, action="delete")
    if not db().archive("sources", source_id):
        raise FileNotFoundError("数据源不存在")
    cleaned = _remove_source_from_scopes(source_id, source["workspace_id"])
    return ok(archived=True, cleaned=cleaned)


@bp.post("/api/sources/<source_id>/refresh")
@api_errors
def refresh(source_id: str):
    source = require_source_access(source_id, action="refresh")
    assert_feature_enabled(db(), source["workspace_id"], "data_sources")
    return ok(item=public_source(refresh_source(source)))


@bp.get("/api/sources/<source_id>/schema")
@api_errors
def source_schema(source_id: str):
    return ok(schema=schema_for_source(require_source_access(source_id), actor_id=current_user_id()))


@bp.get("/api/sources/<source_id>/preview")
@api_errors
def source_preview(source_id: str):
    limit = int(request.args.get("limit", "100"))
    return ok(preview=preview_source(require_source_access(source_id), request.args.get("table"), min(limit, 500), actor_id=current_user_id()))


@bp.get("/api/sources/<source_id>/profile")
@api_errors
def source_profile(source_id: str):
    _, frame = source_table(require_source_access(source_id), request.args.get("table"), actor_id=current_user_id())
    return ok(profile=profile(frame))


@bp.post("/api/sources/<source_id>/clean/preview")
@api_errors
def clean_preview(source_id: str):
    payload = body()
    source = require_source_access(source_id, action="analyze")
    assert_feature_enabled(db(), source["workspace_id"], "data_sources")
    _, frame = source_table(source, payload.get("table"), actor_id=current_user_id())
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
    _, frame = source_table(source, payload.get("table"), actor_id=current_user_id())
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
    return ok(items=[
        public_document(item) for item in db().list("knowledge_documents", workspace_id=workspace_id())
        if item.get("visibility") != "analysis_attachment"
    ])


@bp.post("/api/knowledge/documents")
@api_errors
def upload_document():
    if "file" not in request.files:
        raise ValueError("请选择知识文档")
    tags_raw = request.form.get("tags", "")
    tags = [item.strip() for item in tags_raw.split(",") if item.strip()]
    wid = workspace_id()
    assert_feature_enabled(db(), wid, "knowledge_base")
    return ok(item=add_document(request.files["file"], wid, tags)), 201


@bp.patch("/api/knowledge/documents/<document_id>")
@api_errors
def update_document(document_id: str):
    document = require_workspace_record("knowledge_documents", document_id)
    if document.get("visibility") == "analysis_attachment":
        raise FileNotFoundError("知识文档不存在")
    assert_feature_enabled(db(), document["workspace_id"], "knowledge_base")
    allowed = {key: value for key, value in body().items() if key in {"name", "tags", "enabled"}}
    return ok(item=public_document(db().patch("knowledge_documents", document_id, allowed)))


@bp.delete("/api/knowledge/documents/<document_id>")
@api_errors
def archive_document(document_id: str):
    document = require_workspace_record("knowledge_documents", document_id)
    if document.get("visibility") == "analysis_attachment":
        raise FileNotFoundError("知识文档不存在")
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


@bp.get("/api/skills")
def list_skills():
    items = [public_skill(item) for item in DEFAULT_SKILLS]
    return ok(items=items, skills=items, diagnostics=[])


@bp.get("/api/skills/<skill_id>")
@api_errors
def get_skill_detail(skill_id: str):
    skill = get_skill(skill_id, workspace_id())
    if not skill:
        raise FileNotFoundError("Skill 不存在")
    item = public_skill(skill, include_prompt=True)
    return ok(item=item, skill={**item, "raw": item.get("instruction", "")})


@bp.get("/api/commands")
def commands():
    return ok(items=[
        {"name": "data", "description": "打开当前数据源和表预览", "usage": "/data"},
        {"name": "help", "aliases": ["h", "?"], "description": "查看可用命令", "usage": "/help [命令]"},
        {"name": "instruction", "aliases": ["i"], "description": "设置当前会话临时指令", "usage": "/instruction [指令]"},
        {"name": "knowledge", "aliases": ["kb"], "description": "打开业务知识库", "usage": "/knowledge"},
        {"name": "mcp", "description": "打开 MCP 连接与工具管理", "usage": "/mcp"},
        {"name": "new", "aliases": ["n"], "description": "新建一个干净分析会话", "usage": "/new [会话名]"},
        {"name": "sessions", "aliases": ["session"], "description": "管理已保存对话", "usage": "/sessions [new]"},
        {"name": "status", "aliases": ["s"], "description": "查看模型、数据源和上下文状态", "usage": "/status"},
        {"name": "workspace", "aliases": ["ws"], "description": "管理工作目录和权限", "usage": "/workspace"},
    ])
