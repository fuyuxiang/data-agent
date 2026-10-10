"""Skill management and resolution.

A Skill is a professional capability an Agent can discover and execute.  This
blueprint is the only place that writes skill definitions, so every mutation is
authorized here and audited — the runtime itself stays read-only.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import yaml
from flask import Blueprint, Response, request

from ..core.database import utcnow
from ..skills import evaluator as skill_evaluator
from ..skills.loader import read_package
from ..skills.models import (
    CATEGORIES,
    FORMAL_AGENT_TOOLS,
    SkillDefinition,
    SkillError,
    skill_from_payload,
    skill_from_record,
    skill_to_record,
    validate_id,
)
from ..skills.permissions import available_resources, filter_visible, unavailable_reason
from ..skills.registry import COLLECTION, VERSION_COLLECTION, SkillRegistry
from ..skills.resolver import SkillResolver, extract_explicit
from .common import (
    api_errors, body, current_user_id, db, ok, require_workspace_access, workspace_id,
)

bp = Blueprint("skills", __name__)


def _registry(wid: str | None = None) -> SkillRegistry:
    return SkillRegistry(db(), wid or workspace_id())


def _runtime_tools(wid: str) -> list[str]:
    """Tools a formal analysis run can actually reach in this workspace."""
    from ..services.advanced_agent import available_formal_tools

    try:
        extra = available_formal_tools(db(), wid, "skill-preview", [])
    except Exception:  # pragma: no cover - degraded preview must not block the list
        extra = []
    return sorted(set(FORMAL_AGENT_TOOLS) | set(extra))


def _require_manageable(skill_id: str, wid: str) -> SkillDefinition:
    definition = _registry(wid).get(skill_id)
    if definition is None:
        raise FileNotFoundError(f"技能不存在：{skill_id}")
    return definition


def _require_record(skill_id: str, wid: str) -> dict[str, Any]:
    """Resolve a skill by its logical id (slug), returning the stored record.

    A workspace skill is keyed by an opaque record id but addressed by the slug
    the administrator typed, so every mutation has to go through this lookup.
    """
    _require_manageable(skill_id, wid)
    record = _registry(wid).record_for(skill_id)
    if record is None:
        raise FileNotFoundError(f"技能不存在：{skill_id}")
    return record


def _require_editable(skill_id: str, wid: str) -> SkillDefinition:
    definition = _require_manageable(skill_id, wid)
    if not definition.editable:
        raise ValueError("内置技能不可直接修改，请先克隆为工作空间技能")
    return definition


def _snapshot(definition: SkillDefinition, wid: str, actor_id: str) -> None:
    db().put(VERSION_COLLECTION, {
        "id": f"{definition.id}:{definition.version}",
        "workspace_id": wid,
        "skill_id": definition.id,
        "version": definition.version,
        "snapshot": definition.to_public(),
        "changed_by": actor_id,
        "changed_at": utcnow(),
    }, workspace_id=wid)


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #

@bp.get("/api/skills")
@api_errors
def list_skills():
    wid = workspace_id()
    require_workspace_access(wid)
    actor = current_user_id()
    registry = _registry(wid)
    available = available_resources(db(), wid, actor)
    manage = _can_manage(wid)
    definitions = registry.definitions()
    visible = filter_visible(definitions, available, include_disabled=manage)
    hidden = [
        {
            "id": item.id, "name": item.name, "description": item.description,
            "category": item.category, "status": item.status, "source": item.source,
            "unavailable_reason": unavailable_reason(item, available),
        }
        for item in definitions
        if item not in visible
    ]
    return ok(
        items=[item.to_card() for item in visible],
        unavailable=hidden,
        categories=list(CATEGORIES),
        can_manage=manage,
        total=len(definitions),
    )


@bp.get("/api/skills/<skill_id>")
@api_errors
def get_skill(skill_id: str):
    wid = workspace_id()
    require_workspace_access(wid)
    definition = _require_manageable(skill_id, wid)
    available = available_resources(db(), wid, current_user_id())
    payload = definition.to_public()
    payload["unavailable_reason"] = unavailable_reason(definition, available)
    payload["agents_using"] = _registry(wid).agents_using(skill_id)
    return ok(item=payload)


def _can_manage(wid: str) -> bool:

    user_id = current_user_id()
    if user_id == "local-default" and not db().list("users", include_archived=True, limit=1):
        return True
    membership = next(
        (
            item for item in db().list("workspace_members", workspace_id=wid)
            if item.get("user_id") == user_id and item.get("enabled", True)
        ),
        None,
    )
    return bool(membership) and membership.get("role") in {"owner", "editor"}


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #

@bp.post("/api/skills")
@api_errors
def create_skill():
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    payload = body()
    skill_id = validate_id(payload.get("id") or f"skill-{len(_registry(wid).definitions()) + 1}")
    if _registry(wid).get(skill_id) is not None:
        raise SkillError(f"技能标识已存在：{skill_id}")
    definition = skill_from_payload({**payload, "id": skill_id})
    record = skill_to_record(definition, wid)
    item = db().put(COLLECTION, {
        **record, "id": db().new_id("skl"), "slug": definition.id,
        "version": 1, "status": "draft",
        "created_by": current_user_id(), "created_at": utcnow(),
    }, workspace_id=wid)
    db().audit("skill.created", workspace_id=wid, actor=current_user_id(),
               object_type="skill", object_id=item["id"], detail={"slug": definition.id})
    return ok(item=_stored_view(wid, item)), 201


@bp.patch("/api/skills/<skill_id>")
@api_errors
def update_skill(skill_id: str):
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    _require_editable(skill_id, wid)
    with db().transaction():
        item = _require_record(skill_id, wid)
        if item.get("status") == "published" and _registry(wid).agents_using(skill_id):
            raise ValueError("已发布技能仍被智能体的草稿或已发布版本使用，请解除绑定并发布后再编辑或停用")
        incoming = {key: value for key, value in body().items() if key != "id"}
        merged = skill_from_payload(
            {**item, **incoming, "id": item.get("slug") or item["id"]}, record=item,
        )
        payload = skill_to_record(merged, wid)
        payload.update({
            "version": str(int(item.get("version") or 1) + 1),
            "status": "draft" if item.get("status") == "published" else item.get("status", "draft"),
            "published_at": None,
            "updated_at": utcnow(),
        })
        saved = db().patch(COLLECTION, item["id"], payload, workspace_id=wid)
        _snapshot(skill_from_record(saved), wid, current_user_id())
    db().audit("skill.updated", workspace_id=wid, actor=current_user_id(),
               object_type="skill", object_id=item["id"], detail={"version": payload["version"]})
    return ok(item=_stored_view(wid, saved))


@bp.delete("/api/skills/<skill_id>")
@api_errors
def delete_skill(skill_id: str):
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    _require_editable(skill_id, wid)
    item = _require_record(skill_id, wid)
    definition = skill_from_record(item)
    with db().transaction():
        used_by = _registry(wid).agents_using(definition.id)
        if used_by:
            raise ValueError(f"技能仍被 {len(used_by)} 个智能体的草稿或已发布版本使用，请解除绑定并发布")
        db().archive(COLLECTION, item["id"], workspace_id=wid)
    db().audit("skill.deleted", workspace_id=wid, actor=current_user_id(),
               object_type="skill", object_id=item["id"], detail={"slug": definition.id})
    return ok(item=_stored_view(wid, item))


@bp.post("/api/skills/<skill_id>/publish")
@api_errors
def publish_skill(skill_id: str):
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    _require_editable(skill_id, wid)
    item = _require_record(skill_id, wid)
    definition = _require_manageable(skill_id, wid)
    report = skill_evaluator.evaluate(
        db(), wid, current_user_id(), definition,
        runtime_tools=_runtime_tools(wid), registry=_registry(wid), with_samples=False,
    )
    skill_evaluator.assert_publishable(report)
    saved = db().patch(COLLECTION, item["id"], {
        "status": "published", "published_at": utcnow(),
    }, workspace_id=wid)
    _snapshot(skill_from_record(saved), wid, current_user_id())
    db().audit("skill.published", workspace_id=wid, actor=current_user_id(),
               object_type="skill", object_id=item["id"], detail={"version": saved["version"]})
    return ok(item=_stored_view(wid, saved), evaluation=report.to_public())


@bp.post("/api/skills/<skill_id>/clone")
@api_errors
def clone_skill(skill_id: str):
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    source = _require_manageable(skill_id, wid)
    requested = str(body().get("id") or f"{source.id}-copy")
    new_id = validate_id(requested)
    if _registry(wid).get(new_id) is not None:
        raise SkillError(f"技能标识已存在：{new_id}")
    definition = skill_from_payload({
        **source.to_public(), "id": new_id, "name": f"{source.name}（副本）",
    })
    record = skill_to_record(definition, wid)
    item = db().put(COLLECTION, {
        **record, "id": db().new_id("skl"), "version": 1,
        "status": "draft", "created_by": current_user_id(), "created_at": utcnow(),
        "cloned_from": source.id,
    }, workspace_id=wid)
    db().audit("skill.cloned", workspace_id=wid, actor=current_user_id(),
               object_type="skill", object_id=item["id"], detail={"from": source.id})
    return ok(item=_stored_view(wid, item)), 201


def _stored_view(wid: str, record: dict[str, Any]) -> dict[str, Any]:
    definition = skill_from_record(record)
    payload = definition.to_public()
    payload["record_id"] = record["id"]
    payload["unavailable_reason"] = unavailable_reason(
        definition, available_resources(db(), wid, current_user_id()),
    )
    return payload


# --------------------------------------------------------------------------- #
# Test / resolve / generate
# --------------------------------------------------------------------------- #

@bp.post("/api/skills/<skill_id>/test")
@api_errors
def test_skill(skill_id: str):
    wid = workspace_id()
    require_workspace_access(wid)
    definition = _require_manageable(skill_id, wid)
    report = skill_evaluator.evaluate(
        db(), wid, current_user_id(), definition,
        runtime_tools=_runtime_tools(wid), registry=_registry(wid),
    )
    question = str(body().get("question") or "")
    resolution: dict[str, Any] | None = None
    if question.strip():
        resolution = _resolve(wid, question).to_public()
    return ok(evaluation=report.to_public(), resolution=resolution)


def _resolve(wid: str, question: str, *, explicit: list[str] | None = None):
    actor = current_user_id()
    registry = _registry(wid)
    available = available_resources(db(), wid, actor)
    visible = filter_visible(registry.definitions(include_disabled=False), available)
    resolver = SkillResolver(visible)
    resolution = resolver.resolve(question, explicit=explicit)
    rejected = [
        {"id": item.id, "name": item.name, "reason": unavailable_reason(item, available)}
        for item in registry.definitions()
        if item not in visible and unavailable_reason(item, available)
    ]
    return ResolutionWithRejected(resolution, rejected)


class ResolutionWithRejected:
    """Wrap a resolution so the rejected (permission-filtered) skills ride along."""

    def __init__(self, resolution, rejected: list[dict[str, Any]]):
        self._resolution = resolution
        self._rejected = rejected

    @property
    def selected(self):
        return self._resolution.selected

    def to_public(self) -> dict[str, Any]:
        return {**self._resolution.to_public(), "rejected": self._rejected}


@bp.post("/api/skills/resolve")
@api_errors
def resolve_skills():
    wid = workspace_id()
    require_workspace_access(wid)
    payload = body()
    question = str(payload.get("question") or "")
    explicit = payload.get("explicit")
    if explicit is None:
        explicit = list(extract_explicit(question))
    return ok(**_resolve(wid, question, explicit=[str(v) for v in explicit]).to_public())


@bp.post("/api/skills/generate")
@api_errors
def generate_skill():
    """Draft a skill from a natural-language description.

    The draft is deterministic and rule-based: it extracts capability terms and
    maps them onto triggers, examples and the required tools. The caller must
    review the editable result before saving it.
    """
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    description = str(body().get("description") or "").strip()
    if len(description) < 8:
        raise ValueError("请用一句话描述这个技能要做什么（至少 8 个字）")
    draft = _draft_from_description(description)
    return ok(draft=draft, categories=list(CATEGORIES))


_INTENT_TERMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "query": ("数据查询", ("多少", "是多少", "统计", "列出", "汇总", "取数", "查询")),
    "analysis": ("数据分析", ("分析", "统计", "分布", "占比", "排名", "结构")),
    "trend": ("趋势分析", ("趋势", "走势", "同比", "环比", "增长", "变化")),
    "attribution": ("归因分析", ("为什么", "原因", "归因", "导致", "影响")),
    "forecast": ("预测分析", ("预测", "预计", "未来", "展望")),
    "anomaly": ("异常分析", ("异常", "波动", "离群", "突变")),
    "research": ("深度研究", ("研究", "调研", "综述", "深入")),
    "report": ("报告生成", ("报告", "汇报", "文档", "简报")),
    "ppt": ("PPT 生成", ("ppt", "幻灯片", "演示文稿")),
    "excel": ("Excel 处理", ("excel", "表格", "csv")),
    "visual": ("数据可视化", ("图表", "可视化", "画图", "趋势图")),
    "export": ("数据导出", ("导出", "下载")),
}

_TOOL_SETS: dict[str, list[str]] = {
    "query": ["get_schema", "list_semantic_metrics", "query_metric", "query_data", "validate_result"],
    "analysis": ["get_schema", "list_semantic_metrics", "query_metric", "query_data",
                 "run_analysis", "select_chart", "generate_chart", "validate_result"],
    "trend": ["get_schema", "list_semantic_metrics", "query_metric", "query_data",
              "run_analysis", "select_chart", "generate_chart", "validate_result"],
    "attribution": ["get_schema", "list_semantic_metrics", "query_metric", "query_data",
                    "run_analysis", "generate_chart", "validate_result"],
    "forecast": ["get_schema", "list_semantic_metrics", "query_metric", "query_data",
                 "run_analysis", "select_chart", "generate_chart", "validate_result"],
    "anomaly": ["get_schema", "query_data", "profile_data", "run_analysis", "validate_result"],
    "research": ["query_knowledge", "get_schema", "list_semantic_metrics", "query_metric",
                 "query_data", "run_analysis", "generate_chart", "search_mcp_tools", "validate_result"],
    "report": ["read_tool_result", "generate_chart", "validate_result"],
    "ppt": ["read_tool_result", "generate_chart"],
    "excel": ["read_tool_result", "get_schema", "query_data", "profile_data", "validate_result"],
    "visual": ["get_schema", "list_semantic_metrics", "query_metric", "query_data",
               "select_chart", "generate_chart"],
    "export": ["read_tool_result", "query_data", "query_metric"],
}

_CATEGORY_BY_INTENT = {
    "query": "数据分析", "analysis": "数据分析", "trend": "数据分析",
    "attribution": "数据分析", "forecast": "数据分析", "anomaly": "数据分析",
    "research": "深度研究", "report": "报告", "ppt": "报告", "excel": "文件分析",
    "visual": "可视化", "export": "工具",
}


def _draft_from_description(description: str) -> dict[str, Any]:
    lowered = description.lower()
    matched = [
        (key, label, words)
        for key, (label, words) in _INTENT_TERMS.items()
        if any(word.lower() in lowered for word in words)
    ]
    if not matched:
        matched = [("analysis", "数据分析", ("分析",))]

    primary = matched[0][0]
    tools: list[str] = []
    triggers: list[str] = []
    for key, _label, words in matched:
        tools.extend(_TOOL_SETS[key])
        triggers.extend(words)
    example = description.rstrip("。.")[:60]
    return {
        "id": "",
        "name": matched[0][1],
        "category": _CATEGORY_BY_INTENT[primary],
        "description": example,
        "usage": f"当用户提出与「{'、'.join(dict.fromkeys(triggers[:3]))}」相关的问题时使用。",
        "instruction": (
            f"你负责完成以下任务：{description}\n\n"
            "执行要求：\n"
            "1. 先确认数据范围与时间口径，必要时向用户澄清。\n"
            "2. 有正式指标时优先使用指标定义；没有时再自行查询。\n"
            "3. 结论先行，每个数字都要能追溯到具体数据。\n"
            "4. 数据不足时如实说明，不用估算填补。\n"
            "5. 区分「数据表明的」与「推测的」，推测必须标注。"
        ),
        "triggers": list(dict.fromkeys(triggers))[:20],
        "example_questions": [example, f"{example}，并给出结论"],
        "allowed_tools": list(dict.fromkeys(tools)),
        "inputs": ["业务问题", "时间范围"],
        "outputs": ["结论", "支撑数据", "图表"],
        "notes": "根据描述中的任务关键词生成，请在发布前确认触发条件与工具范围。",
    }


# --------------------------------------------------------------------------- #
# Import / export
# --------------------------------------------------------------------------- #

@bp.get("/api/skills/<skill_id>/export")
@api_errors
def export_skill(skill_id: str):
    wid = workspace_id()
    require_workspace_access(wid)
    definition = _require_manageable(skill_id, wid)
    manifest = yaml.safe_dump(_manifest_payload(definition), allow_unicode=True, sort_keys=False)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{definition.id}/manifest.yaml", manifest)
        archive.writestr(f"{definition.id}/SKILL.md", definition.instruction or definition.usage)
        archive.writestr(
            f"{definition.id}/evals/questions.json",
            json.dumps(list(definition.example_questions), ensure_ascii=False, indent=2),
        )
    payload = buffer.getvalue()
    return Response(
        payload,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="skill-{definition.id}.zip"'},
    )


def _manifest_payload(definition: SkillDefinition) -> dict[str, Any]:
    value = definition.to_public()
    return {
        "id": value["id"], "name": value["name"], "description": value["description"],
        "category": value["category"], "version": value["version"],
        "usage": value["usage"], "instruction": value["instruction"],
        "triggers": value["triggers"], "example_questions": value["example_questions"],
        "allowed_tools": value["allowed_tools"], "metric_names": value["metric_names"],
        "mcp_server_ids": value["mcp_server_ids"],
        "inputs": value["inputs"], "outputs": value["outputs"], "notes": value["notes"],
    }


@bp.post("/api/skills/import")
@api_errors
def import_skill():
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    upload = request.files.get("file")
    if upload is None:
        raise ValueError("请选择要导入的技能包")
    payload = upload.read()
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise ValueError("技能包必须是包含 manifest.yaml 的 zip 文件")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.endswith("manifest.yaml")]
        if len(names) != 1:
            raise ValueError("技能包必须包含且仅包含一个 manifest.yaml")
        # 拒绝路径越界与符号链接条目。
        for name in archive.namelist():
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("技能包包含非法路径")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                target = (root / name).resolve()
                if root not in target.parents:
                    raise ValueError("技能包包含非法路径")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
            definition = read_package((root / names[0]).parent)
    payload_dict = definition.to_public()
    payload_dict["source"] = "workspace"
    if _registry(wid).get(definition.id) is not None:
        raise SkillError(f"技能标识已存在：{definition.id}")
    record = skill_to_record(skill_from_payload(payload_dict), wid)
    item = db().put(COLLECTION, {
        **record, "id": db().new_id("skl"), "version": 1,
        "status": "draft", "created_by": current_user_id(), "created_at": utcnow(),
    }, workspace_id=wid)
    db().audit("skill.imported", workspace_id=wid, actor=current_user_id(),
               object_type="skill", object_id=item["id"], detail={"slug": definition.id})
    return ok(item=_stored_view(wid, item)), 201
