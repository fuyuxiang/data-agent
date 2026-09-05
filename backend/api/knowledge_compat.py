from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from ..services.knowledge import (
    index_document_path,
    parse_knowledge_path,
    save_entry,
    search,
    strip_temp_prompt_thinking,
)
from ..services.models import resolve_provider
from .common import api_errors, body, db, ok, require_workspace_record, workspace_id


log = logging.getLogger(__name__)
bp = Blueprint("knowledge_compat", __name__)
TEMP_PROMPT_MAX_CHARS = 4000


def _public(item: dict) -> dict:
    return {key: value for key, value in item.items() if key not in {"tokens", "embedding"}}


def _import_record(filename: str, wid: str | None = None) -> dict:
    record = next(
        (
            item for item in db().list("knowledge_imports", workspace_id=wid or workspace_id(), limit=5000)
            if item.get("filename") == Path(filename).name
        ),
        None,
    )
    if not record:
        raise FileNotFoundError("知识源文件不存在")
    return record


@bp.post("/api/knowledge/parse")
@api_errors
def parse_knowledge_file():
    uploaded = request.files.get("file")
    if not uploaded:
        raise ValueError("请上传知识文件")
    original_name = uploaded.filename or "upload"
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".xlsx", ".xls", ".docx"}:
        raise ValueError(f"不支持的文件格式 {suffix}，请上传 .xlsx / .xls / .docx")
    wid = str(request.form.get("workspace_id") or request.headers.get("X-Workspace-Id") or "default")[:128]
    stem = "".join(character if character.isalnum() or character in "-_." else "_" for character in Path(original_name).stem)[:60] or "knowledge"
    import_id = db().new_id("kbimp")
    filename = f"{import_id.split('_', 1)[-1][:8]}_{stem}{suffix}"
    target = current_app.config["SETTINGS"].knowledge_dir / filename
    uploaded.save(target)
    result = parse_knowledge_path(target, wid, str(request.form.get("provider") or ""))
    db().put(
        "knowledge_imports",
        {
            "id": import_id, "workspace_id": wid, "filename": filename,
            "original_name": original_name[:255], "path": str(target), "size": target.stat().st_size,
            "format": result["format"], "status": "preview",
        },
        workspace_id=wid,
    )
    return jsonify({**result, "filename": filename})


@bp.get("/api/knowledge/files")
def knowledge_files():
    items = []
    for record in db().list("knowledge_imports", workspace_id=workspace_id(), limit=5000):
        path = Path(str(record.get("path") or ""))
        if path.is_file():
            stat = path.stat()
            items.append({"filename": record["filename"], "size": stat.st_size, "mtime": stat.st_mtime})
    return jsonify(items)


@bp.delete("/api/knowledge/files/<path:filename>")
@api_errors
def delete_knowledge_file(filename: str):
    record = _import_record(Path(filename).name)
    Path(record["path"]).unlink(missing_ok=True)
    db().archive("knowledge_imports", record["id"])
    for document in db().list("knowledge_documents", workspace_id=record["workspace_id"], limit=5000):
        if document.get("source_import_filename") == record["filename"]:
            db().archive("knowledge_documents", document["id"])
    return ok()


def _entry_identity(record: dict) -> tuple[str, str]:
    table = str(record.get("table") or "")
    if table == "metrics":
        return "metric", str(record.get("name") or "").strip()
    if table == "business_rules":
        return "business_rule", str(record.get("rule_id") or "").strip()
    if table == "context_notes":
        return "context_note", str(record.get("topic") or "").strip()
    return "", ""


def _save_preview_record(record: dict, wid: str, category_id: str) -> str | None:
    entry_type, identity = _entry_identity(record)
    if not entry_type or not identity:
        return None
    existing = next(
        (
            item for item in db().list("knowledge_entries", workspace_id=wid, limit=5000)
            if item.get("type") == entry_type
            and str(item.get("rule_id") or item.get("topic") or item.get("name") or "").strip().lower() == identity.lower()
        ),
        None,
    )
    payload = {**record, "type": entry_type, "category_id": category_id}
    if entry_type == "business_rule":
        payload["name"] = identity
    elif entry_type == "context_note":
        payload["name"] = identity
    save_entry(payload, wid, existing["id"] if existing else None)
    return {"metric": "metrics", "business_rule": "rules", "context_note": "notes"}[entry_type]


@bp.post("/api/knowledge/confirm")
@api_errors
def confirm_knowledge_records():
    payload, wid = body(), workspace_id()
    records = payload.get("records") or []
    filename = Path(str(payload.get("filename") or "")).name
    if not isinstance(records, list):
        raise ValueError("records 必须是数组")
    if not records and not filename:
        raise ValueError("没有可确认的知识记录或源文件")
    category_id = str(payload.get("category_id") or "default")
    counts = {"metrics": 0, "rules": 0, "notes": 0}
    for record in records:
        if isinstance(record, dict):
            bucket = _save_preview_record(record, wid, category_id)
            if bucket:
                counts[bucket] += 1
    rag = {"chunks": 0}
    if filename:
        import_record = _import_record(filename, wid)
        document = index_document_path(
            Path(import_record["path"]), wid, original_name=import_record["original_name"],
            import_filename=import_record["filename"],
        )
        import_record.update({"status": "confirmed", "document_id": document["id"]})
        db().put("knowledge_imports", import_record, workspace_id=wid)
        rag = {"chunks": int(document.get("chunk_count") or 0), "document_id": document["id"]}
    return jsonify({"ok": True, "inserted": counts, "rag": rag})


def _entries(entry_type: str) -> list[dict]:
    category = request.args.get("category_id")
    items = [item for item in db().list("knowledge_entries", workspace_id=workspace_id(), limit=5000) if item.get("type") == entry_type]
    if category:
        items = [item for item in items if str(item.get("category_id") or "default") == category]
    return [_public(item) for item in items]


def _create_entry(entry_type: str):
    payload = body()
    if entry_type == "business_rule":
        payload = {**payload, "name": payload.get("rule_id")}
    elif entry_type == "context_note":
        payload = {**payload, "name": payload.get("topic")}
    try:
        return jsonify(_public(save_entry({**payload, "type": entry_type}, workspace_id()))), 201
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


def _update_entry(entry_id: str, entry_type: str):
    try:
        current = require_workspace_record("knowledge_entries", entry_id)
        payload = {**current, **body(), "type": entry_type}
        payload["name"] = payload.get("rule_id") if entry_type == "business_rule" else payload.get("topic") if entry_type == "context_note" else payload.get("name")
        return jsonify(_public(save_entry(payload, current["workspace_id"], entry_id)))
    except FileNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


def _delete_entry(entry_id: str):
    try:
        require_workspace_record("knowledge_entries", entry_id)
    except FileNotFoundError:
        return jsonify({"error": "Not found"}), 404
    db().archive("knowledge_entries", entry_id)
    return ok()


def _toggle_entry(entry_id: str):
    try:
        item = require_workspace_record("knowledge_entries", entry_id)
    except FileNotFoundError:
        return jsonify({"error": "Not found"}), 404
    item = db().patch("knowledge_entries", entry_id, {"enabled": not item.get("enabled", True)})
    return jsonify(_public(item))


@bp.get("/api/knowledge/metrics")
def metrics_list(): return jsonify(_entries("metric"))


@bp.post("/api/knowledge/metrics")
def metrics_create(): return _create_entry("metric")


@bp.put("/api/knowledge/metrics/<entry_id>")
def metrics_update(entry_id: str): return _update_entry(entry_id, "metric")


@bp.delete("/api/knowledge/metrics/<entry_id>")
def metrics_delete(entry_id: str): return _delete_entry(entry_id)


@bp.post("/api/knowledge/metrics/<entry_id>/toggle")
def metrics_toggle(entry_id: str): return _toggle_entry(entry_id)


@bp.get("/api/knowledge/rules")
def rules_list(): return jsonify(_entries("business_rule"))


@bp.post("/api/knowledge/rules")
def rules_create(): return _create_entry("business_rule")


@bp.put("/api/knowledge/rules/<entry_id>")
def rules_update(entry_id: str): return _update_entry(entry_id, "business_rule")


@bp.delete("/api/knowledge/rules/<entry_id>")
def rules_delete(entry_id: str): return _delete_entry(entry_id)


@bp.post("/api/knowledge/rules/<entry_id>/toggle")
def rules_toggle(entry_id: str): return _toggle_entry(entry_id)


@bp.get("/api/knowledge/notes")
def notes_list(): return jsonify(_entries("context_note"))


@bp.post("/api/knowledge/notes")
def notes_create(): return _create_entry("context_note")


@bp.put("/api/knowledge/notes/<entry_id>")
def notes_update(entry_id: str): return _update_entry(entry_id, "context_note")


@bp.delete("/api/knowledge/notes/<entry_id>")
def notes_delete(entry_id: str): return _delete_entry(entry_id)


@bp.post("/api/knowledge/notes/<entry_id>/toggle")
def notes_toggle(entry_id: str): return _toggle_entry(entry_id)


@bp.get("/api/knowledge/search")
def knowledge_search_compat():
    rows = search(request.args.get("q", ""), workspace_id(), 20)
    result = {"metrics": [], "rules": [], "notes": []}
    for row in rows:
        bucket = "metrics" if row.get("kind") == "metric" else "rules" if row.get("kind") == "business_rule" else "notes"
        result[bucket].append(row)
    return jsonify(result)


@bp.put("/api/knowledge/categories/<category_id>")
@api_errors
def category_update(category_id: str):
    item = require_workspace_record("knowledge_categories", category_id)
    changes = {key: body()[key] for key in ("name", "enabled") if key in body()}
    return jsonify(db().patch("knowledge_categories", item["id"], changes))


@bp.post("/api/knowledge/categories/<category_id>/toggle")
@api_errors
def category_toggle(category_id: str):
    item = require_workspace_record("knowledge_categories", category_id)
    return jsonify(db().patch("knowledge_categories", item["id"], {"enabled": not item.get("enabled", True)}))


@bp.delete("/api/knowledge/categories/<category_id>")
@api_errors
def category_delete(category_id: str):
    item = require_workspace_record("knowledge_categories", category_id)
    if any(str(entry.get("category_id")) == category_id for entry in db().list("knowledge_entries", workspace_id=item["workspace_id"], limit=5000)):
        raise ValueError("该分类下仍有知识条目，不能删除")
    db().archive("knowledge_categories", category_id)
    return ok()


def _temp_state(session: dict) -> dict:
    text = strip_temp_prompt_thinking(session.get("temporary_instruction"))
    if text != session.get("temporary_instruction", ""):
        session = db().patch("sessions", session["id"], {"temporary_instruction": text}) or session
    return {"temp_prompt": text, "enabled": bool(text and session.get("temp_prompt_enabled", bool(text))), "max_chars": TEMP_PROMPT_MAX_CHARS}


@bp.get("/api/session/<session_id>/temp-prompt")
@api_errors
def temp_prompt_get(session_id: str):
    return jsonify(_temp_state(require_workspace_record("sessions", session_id)))


@bp.post("/api/session/<session_id>/temp-prompt")
@api_errors
def temp_prompt_set(session_id: str):
    session = require_workspace_record("sessions", session_id)
    payload = body()
    raw_text = strip_temp_prompt_thinking(payload.get("text"))
    if len(raw_text) > TEMP_PROMPT_MAX_CHARS:
        raise ValueError(f"内容过长（超过 {TEMP_PROMPT_MAX_CHARS} 字），请精简后再保存。")
    warning = ""
    final_text = raw_text
    if raw_text and not bool(payload.get("raw", True)):
        provider, client = resolve_provider(str(payload.get("provider") or "") or None, session["workspace_id"])
        if not provider or not client:
            warning = "未能调用模型整理（已按原文保存）：没有可用模型"
        else:
            try:
                from ..services.usage import ensure_quota, record_usage, response_usage

                quota = ensure_quota(db(), session["workspace_id"])
                response = client.chat.completions.create(
                    model=provider["model"], temperature=0,
                    max_tokens=max(1, min(1024, quota["remaining"])),
                    messages=[
                        {"role": "system", "content": "将用户的临时分析指令整理为精炼中文祈使句；不新增意图，不输出标题、解释或思考标签。"},
                        {"role": "user", "content": raw_text},
                    ],
                )
                record_usage(
                    db(), session["workspace_id"], response_usage(response, provider["model"]),
                    session_id=session_id, operation="prompt_refinement",
                )
                refined = strip_temp_prompt_thinking(response.choices[0].message.content)
                if refined:
                    final_text = refined
                else:
                    warning = "模型未返回可用正文，已按原文保存。"
            except Exception as exc:
                log.warning("Temporary prompt refinement failed: %s", type(exc).__name__)
                warning = "整理失败，已按原文保存。"
    session = db().patch(
        "sessions", session_id,
        {"temporary_instruction": final_text, "temp_prompt_enabled": bool(final_text)},
    )
    return jsonify({**_temp_state(session), "warning": warning})


@bp.post("/api/session/<session_id>/temp-prompt/toggle")
@api_errors
def temp_prompt_toggle(session_id: str):
    session = require_workspace_record("sessions", session_id)
    text = strip_temp_prompt_thinking(session.get("temporary_instruction"))
    if not text:
        session = db().patch("sessions", session_id, {"temp_prompt_enabled": False})
        return jsonify({**_temp_state(session), "warning": "临时指令为空，无法启用。"})
    session = db().patch("sessions", session_id, {"temp_prompt_enabled": not session.get("temp_prompt_enabled", True)})
    return jsonify(_temp_state(session))
