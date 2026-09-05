from __future__ import annotations

import html
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..core.database import Database, utcnow
from .diagram_templates import DIAGRAM_TEMPLATES
from .diagram_xml import (
    MAX_XML_SIZE,
    apply_diagram_operations,
    is_mxcell_xml_complete,
    validate_and_fix_xml,
    wrap_with_mxfile,
)


DEFAULT_CONTENT = {
    "summary": "",
    "assumptions": [],
    "evidence_refs": [],
    "risks": [],
    "next_actions": [],
}
BLANK_DIAGRAM_XML = (
    '<mxfile><diagram name="Blank" id="blank"><mxGraphModel><root>'
    '<mxCell id="0"/><mxCell id="1" parent="0"/>'
    "</root></mxGraphModel></diagram></mxfile>"
)
TEMPLATE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "blank_canvas", "name": "空白画布",
        "description": "从零开始的空白 draw.io 画布，自由绘制任意图形。",
        "priority": "P0", "rendering_mode": "diagram", "blocks": (),
        "diagram_xml": BLANK_DIAGRAM_XML,
    },
    {
        "id": "business_model_canvas", "name": "商业模式画布",
        "description": "用 9 个模块描绘商业模式，适合从用户、价值、渠道、收入和成本整体拆解产品。",
        "priority": "P0", "rendering_mode": "both",
        "blocks": (
            ("customer_segments", "客户细分"), ("value_proposition", "价值主张"),
            ("channels", "渠道"), ("customer_relationships", "客户关系"),
            ("revenue_streams", "收入来源"), ("key_resources", "关键资源"),
            ("key_activities", "关键活动"), ("key_partners", "关键伙伴"),
            ("cost_structure", "成本结构"),
        ),
    },
    {
        "id": "bcg_matrix", "name": "BCG 矩阵",
        "description": "2×2 矩阵：市场增长率与相对市场份额，对业务单元分类。",
        "priority": "P0", "rendering_mode": "diagram",
        "blocks": (("stars", "明星业务"), ("question_marks", "问题业务"),
                   ("cash_cows", "现金牛业务"), ("dogs", "瘦狗业务")),
    },
    {
        "id": "swot_analysis", "name": "SWOT 分析",
        "description": "4 象限：内部优势/劣势与外部机会/威胁。",
        "priority": "P0", "rendering_mode": "diagram",
        "blocks": (("strengths", "优势"), ("weaknesses", "劣势"),
                   ("opportunities", "机会"), ("threats", "威胁")),
    },
    {
        "id": "value_proposition", "name": "价值主张速写",
        "description": "拆解价值主张的六个组成部分，说清产品为谁、解决什么、有何不同。",
        "priority": "P0", "rendering_mode": "diagram",
        "blocks": (
            ("product_service", "产品与服务"), ("customer_segments", "客户细分"),
            ("customer_jobs", "客户任务"), ("pain_relievers", "痛点缓解"),
            ("gain_creators", "收益创造"), ("competitive_alternatives", "竞争价值主张"),
        ),
    },
)
TEMPLATE_MAP = {item["id"]: item for item in TEMPLATE_DEFINITIONS}
LIST_FIELDS = {"assumptions", "evidence_refs", "risks", "next_actions"}
SHAPE_LIBS_DIR = Path(__file__).with_name("shape_libs")


def _template_xml(template_id: str) -> str:
    if template_id == "blank_canvas":
        return BLANK_DIAGRAM_XML
    return str(DIAGRAM_TEMPLATES.get(template_id, {}).get("xml") or "")


def list_templates() -> list[dict[str, Any]]:
    result = []
    for item in TEMPLATE_DEFINITIONS:
        result.append({
            "id": item["id"], "name": item["name"], "description": item["description"],
            "priority": item["priority"], "rendering_mode": item["rendering_mode"],
            "blocks": [
                {"key": key, "title": title, "content": deepcopy(DEFAULT_CONTENT)}
                for key, title in item["blocks"]
            ],
            "diagram_xml": _template_xml(item["id"]),
        })
    return result


def template(template_id: str) -> dict[str, Any] | None:
    return next((item for item in list_templates() if item["id"] == template_id), None)


def normalize_content(content: Any) -> dict[str, Any]:
    source = content if isinstance(content, dict) else {}
    result = deepcopy(DEFAULT_CONTENT)
    result["summary"] = str(source.get("summary") or "").strip()[:50_000]
    for field in LIST_FIELDS:
        raw = source.get(field, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []
        result[field] = [str(value).strip()[:4000] for value in raw[:500] if str(value).strip()]
    return result


def get_project(db: Database, project_id: str, workspace_id: str) -> dict[str, Any]:
    item = db.get("business_canvas_projects", project_id)
    if not item or item.get("workspace_id", "default") != workspace_id:
        raise FileNotFoundError("画布项目不存在")
    item["template"] = template(str(item.get("template_id") or ""))
    return item


def create_project(
    db: Database, *, workspace_id: str, session_id: str, template_id: str, title: str = "",
) -> dict[str, Any]:
    selected = TEMPLATE_MAP.get(str(template_id or ""))
    if not selected:
        raise ValueError(f"未知画布模板：{template_id}")
    if not session_id:
        raise ValueError("session_id 不能为空")
    now = utcnow()
    blocks = [
        {
            "id": db.new_id("block"), "key": key, "title": block_title,
            "content": deepcopy(DEFAULT_CONTENT), "updated_by": "", "updated_at": now,
        }
        for key, block_title in selected["blocks"]
    ]
    item = db.put(
        "business_canvas_projects",
        {
            "id": db.new_id("canvas"), "workspace_id": workspace_id,
            "session_id": session_id, "template_id": selected["id"],
            "title": str(title or selected["name"]).strip()[:200] or selected["name"],
            "status": "active", "rendering_mode": selected["rendering_mode"],
            "diagram_xml": _template_xml(selected["id"]), "blocks": blocks, "revision": 1,
        },
        workspace_id=workspace_id,
    )
    item["template"] = template(selected["id"])
    return item


def list_projects(db: Database, workspace_id: str) -> list[dict[str, Any]]:
    return db.list("business_canvas_projects", workspace_id=workspace_id, limit=500)


def _save_revision(
    db: Database, project: dict[str, Any], *, kind: str, actor_type: str,
    actor_label: str = "", reason: str = "", block_key: str = "",
    before: Any = None, after: Any = None,
) -> dict[str, Any]:
    return db.put(
        "business_canvas_revisions",
        {
            "id": db.new_id("rev"), "workspace_id": project["workspace_id"],
            "project_id": project["id"], "kind": kind, "block_key": block_key,
            "actor_type": actor_type, "actor_label": str(actor_label or "")[:200],
            "before": deepcopy(before), "after": deepcopy(after), "reason": str(reason or "")[:1000],
            "project_revision": int(project.get("revision") or 1) + 1,
        },
        workspace_id=project["workspace_id"],
    )


def update_title(db: Database, project_id: str, workspace_id: str, title: str) -> dict[str, Any]:
    project = get_project(db, project_id, workspace_id)
    value = str(title or "").strip()
    if not value:
        raise ValueError("title 不能为空")
    _save_revision(db, project, kind="title", actor_type="user", before=project["title"], after=value)
    return db.patch("business_canvas_projects", project_id, {
        "title": value[:200], "revision": int(project.get("revision") or 1) + 1,
    }) or project


def update_block(
    db: Database, project_id: str, workspace_id: str, block_key: str, content: Any,
    *, actor_type: str = "user", actor_label: str = "", reason: str = "",
) -> dict[str, Any]:
    project = get_project(db, project_id, workspace_id)
    if actor_type not in {"user", "agent"}:
        raise ValueError("actor_type 必须是 user 或 agent")
    blocks = deepcopy(project.get("blocks") or [])
    block = next((item for item in blocks if item.get("key") == block_key), None)
    if not block:
        raise ValueError(f"当前模板不包含模块：{block_key}")
    before = deepcopy(block.get("content") or {})
    after = normalize_content(content)
    _save_revision(
        db, project, kind="block", block_key=block_key, actor_type=actor_type,
        actor_label=actor_label, reason=reason, before=before, after=after,
    )
    block.update({"content": after, "updated_by": actor_type, "updated_at": utcnow()})
    saved = db.patch("business_canvas_projects", project_id, {
        "blocks": blocks, "revision": int(project.get("revision") or 1) + 1,
    }) or project
    saved["template"] = template(saved["template_id"])
    return saved


def _validated_xml(xml: str) -> tuple[str, list[str]]:
    value = str(xml or "")
    if len(value.encode("utf-8")) > MAX_XML_SIZE:
        raise ValueError(f"画布 XML 不能超过 {MAX_XML_SIZE} 字节")
    if "<!DOCTYPE" in value.upper() or "<!ENTITY" in value.upper():
        raise ValueError("画布 XML 不允许 DTD 或外部实体")
    if "<mxfile" not in value:
        value = wrap_with_mxfile(value)
    result = validate_and_fix_xml(value)
    if not result["valid"]:
        raise ValueError(f"画布 XML 无效：{result['error']}")
    value = str(result.get("fixed") or value)
    if not is_mxcell_xml_complete(value):
        raise ValueError("画布 XML 不完整，可能被截断")
    return value, list(result.get("fixes") or [])


def update_diagram(
    db: Database, project_id: str, workspace_id: str, diagram_xml: str,
    *, actor_type: str = "user", reason: str = "",
) -> tuple[dict[str, Any], list[str]]:
    project = get_project(db, project_id, workspace_id)
    if actor_type not in {"user", "agent"}:
        raise ValueError("actor_type 必须是 user 或 agent")
    value, fixes = _validated_xml(diagram_xml)
    _save_revision(
        db, project, kind="diagram", actor_type=actor_type, reason=reason,
        before=project.get("diagram_xml", ""), after=value,
    )
    saved = db.patch("business_canvas_projects", project_id, {
        "diagram_xml": value, "revision": int(project.get("revision") or 1) + 1,
    }) or project
    saved["template"] = template(saved["template_id"])
    return saved, fixes


def update_rendering_mode(
    db: Database, project_id: str, workspace_id: str, rendering_mode: str,
) -> dict[str, Any]:
    project = get_project(db, project_id, workspace_id)
    mode = str(rendering_mode or "card")
    if mode not in {"card", "diagram", "both"}:
        raise ValueError("rendering_mode 必须是 card、diagram 或 both")
    _save_revision(db, project, kind="rendering_mode", actor_type="user", before=project.get("rendering_mode"), after=mode)
    saved = db.patch("business_canvas_projects", project_id, {
        "rendering_mode": mode, "revision": int(project.get("revision") or 1) + 1,
    }) or project
    saved["template"] = template(saved["template_id"])
    return saved


def list_revisions(db: Database, project_id: str, workspace_id: str) -> list[dict[str, Any]]:
    get_project(db, project_id, workspace_id)
    return [
        item for item in db.list("business_canvas_revisions", workspace_id=workspace_id, limit=5000)
        if item.get("project_id") == project_id
    ]


def delete_project(db: Database, project_id: str, workspace_id: str) -> None:
    get_project(db, project_id, workspace_id)
    if not db.archive("business_canvas_projects", project_id):
        raise FileNotFoundError("画布项目不存在")


def fill_template_content(template_id: str, content: dict[str, Any]) -> str:
    selected = DIAGRAM_TEMPLATES.get(template_id)
    if not selected:
        raise ValueError(f"未知图表模板：{template_id}")
    value = str(selected["xml"])
    for key, text in content.items():
        cell_id = selected.get("content_cells", {}).get(key)
        if not cell_id:
            continue
        escaped = html.escape(str(text), quote=True).replace("\n", "&#xa;").replace("\r", "")
        pattern = re.compile(r'(<mxCell\s+id="' + re.escape(cell_id) + r'"\s+value=")([^"]*)(")')
        value = pattern.sub(lambda match: match.group(1) + escaped + match.group(3), value, count=1)
    return value


def edit_diagram(
    db: Database, project_id: str, workspace_id: str, operations: list[dict[str, Any]],
) -> dict[str, Any]:
    project = get_project(db, project_id, workspace_id)
    if not isinstance(operations, list) or not operations or len(operations) > 200:
        raise ValueError("operations 必须是 1–200 项的数组")
    result = apply_diagram_operations(str(project.get("diagram_xml") or ""), operations)
    if result.get("errors") and not result.get("result"):
        raise ValueError("所有图表编辑操作均失败")
    saved, fixes = update_diagram(
        db, project_id, workspace_id, str(result.get("result") or ""),
        actor_type="agent", reason="Agent 通过 cell id 编辑图表",
    )
    return {"project": saved, "xml": saved["diagram_xml"],
            "operation_errors": result.get("errors") or [], "fixes_applied": fixes}


def get_shape_library(library: str) -> dict[str, Any]:
    name = str(library or "").strip().lower()
    available = sorted(path.stem for path in SHAPE_LIBS_DIR.glob("*.md"))
    if not re.fullmatch(r"[a-z0-9_-]+", name) or name not in available:
        raise ValueError(f"形状库不存在。可用：{', '.join(available)}")
    return {"library": name, "content": (SHAPE_LIBS_DIR / f"{name}.md").read_text(encoding="utf-8"),
            "available": available}


def display_diagram(
    db: Database, *, workspace_id: str, session_id: str, title: str = "Diagram",
    template_id: str = "", content: dict[str, Any] | None = None, xml: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    if content is not None:
        if not template_id:
            raise ValueError("使用 content 时必须提供 template_id")
        xml = fill_template_content(template_id, content)
    if not xml:
        raise ValueError("必须提供 xml 或 template_id + content")
    if project_id:
        project, fixes = update_diagram(db, project_id, workspace_id, xml, actor_type="agent")
    else:
        chosen = template_id or "business_model_canvas"
        project = create_project(
            db, workspace_id=workspace_id, session_id=session_id,
            template_id=chosen, title=title,
        )
        project, fixes = update_diagram(db, project["id"], workspace_id, xml, actor_type="agent")
    if project.get("rendering_mode") != "diagram":
        project = update_rendering_mode(db, project["id"], workspace_id, "diagram")
    return {"project": project, "xml": project["diagram_xml"], "fixes_applied": fixes}
