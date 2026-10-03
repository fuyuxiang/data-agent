"""Skill definition model and validation.

A Skill is stored as one of two shapes:

* a **builtin package** on disk (``skills/<id>/manifest.yaml``), or
* a **workspace record** (``records["skills"]``) authored by an administrator.

Both normalise to :class:`SkillDefinition` so the resolver, executor and UI all
see exactly one contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

MAX_NAME = 60
MAX_DESCRIPTION = 600
MAX_INSTRUCTION = 16_000
MAX_EXAMPLES = 20
MAX_TRIGGERS = 60
MAX_LIST = 100

CATEGORIES = (
    "数据分析",
    "深度研究",
    "文件分析",
    "可视化",
    "报告",
    "办公",
    "工具",
    "自定义",
)
CATEGORY_VALUES = frozenset(CATEGORIES)
STATUSES = ("draft", "published", "disabled")
STATUS_VALUES = frozenset(STATUSES)

# Skills may only reach for tools that the governed analysis runtime knows how
# to authorise.  Anything outside this set is rejected at write time rather
# than failing later inside a run.
FORMAL_AGENT_TOOLS = frozenset({
    "query_knowledge", "get_schema", "get_table_detail", "list_semantic_metrics",
    "query_metric", "query_data", "profile_data", "run_analysis", "select_chart",
    "generate_chart", "ask_user", "structured_output",
    "load_analysis_skill", "read_tool_result", "validate_result", "update_plan",
    "search_mcp_tools", "warehouse_catalog", "warehouse_explain",
    "warehouse_query", "warehouse_spark_submit",
})

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


class SkillError(ValueError):
    """Raised when a skill definition cannot be accepted."""


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    description: str = ""
    category: str = "自定义"
    version: str = "1.0.0"
    status: str = "draft"
    source: str = "builtin"
    instruction: str = ""
    usage: str = ""
    triggers: tuple[str, ...] = ()
    example_questions: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    knowledge_document_ids: tuple[str, ...] = ()
    mcp_server_ids: tuple[str, ...] = ()
    metric_names: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    notes: str = ""
    agent_ids: tuple[str, ...] = ()
    record_id: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    published_at: str = ""
    package_path: str = ""

    @property
    def editable(self) -> bool:
        return self.source == "workspace"

    def to_public(self) -> dict[str, Any]:
        """Full definition for the skill builder (admin view)."""
        return {
            "id": self.id,
            "record_id": self.record_id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "status": self.status,
            "source": self.source,
            "instruction": self.instruction,
            "usage": self.usage,
            "triggers": list(self.triggers),
            "example_questions": list(self.example_questions),
            "allowed_tools": list(self.allowed_tools),
            "source_ids": list(self.source_ids),
            "knowledge_document_ids": list(self.knowledge_document_ids),
            "mcp_server_ids": list(self.mcp_server_ids),
            "metric_names": list(self.metric_names),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "notes": self.notes,
            "agent_ids": list(self.agent_ids),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "published_at": self.published_at,
            "editable": self.editable,
            "categories": list(CATEGORIES),
        }

    def to_card(self) -> dict[str, Any]:
        """Compact definition for the user-facing skill picker."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "status": self.status,
            "source": self.source,
            "usage": self.usage,
            "example_questions": list(self.example_questions),
            "outputs": list(self.outputs),
        }

    def to_model_block(self) -> dict[str, Any]:
        """The shape ``AgentLoop`` consumes as a ``skills=[...]`` entry."""
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "description": self.description,
            "instruction": self.instruction or self.usage,
            "triggers": list(self.triggers),
            "outputs": list(self.outputs),
            "allowed_tools": list(self.allowed_tools),
        }


def _clean_text(value: Any, *, limit: int, field_name: str, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise SkillError(f"{field_name}不能为空")
    if len(text) > limit:
        raise SkillError(f"{field_name}超过 {limit} 字上限")
    return text


def _clean_list(value: Any, *, limit: int, field_name: str) -> tuple[str, ...]:
    if value in (None, "", []):
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise SkillError(f"{field_name}必须是文本列表")
    items: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 200:
            raise SkillError(f"{field_name}中的单项超过 200 字上限")
        items.append(text)
    if len(items) > limit:
        raise SkillError(f"{field_name}最多 {limit} 项")
    return tuple(dict.fromkeys(items))


def validate_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _ID_PATTERN.match(text):
        raise SkillError("技能标识只允许小写字母、数字与中划线，长度 3–64")
    return text


def skill_from_payload(payload: dict[str, Any], *, record: dict[str, Any] | None = None) -> SkillDefinition:
    """Validate and normalise an administrator-submitted skill payload."""
    if not isinstance(payload, dict):
        raise SkillError("技能定义必须是对象")
    base = dict(record or {})
    merged = {**base, **payload}

    skill_id = validate_id(merged.get("id") or (record or {}).get("id") or "")
    name = _clean_text(merged.get("name"), limit=MAX_NAME, field_name="技能名称", required=True)
    description = _clean_text(merged.get("description"), limit=MAX_DESCRIPTION, field_name="技能描述")
    category = str(merged.get("category") or "自定义").strip()
    if category not in CATEGORY_VALUES:
        raise SkillError(f"技能分类必须是：{'、'.join(CATEGORIES)}")
    status = str(merged.get("status") or "draft").strip()
    if status not in STATUS_VALUES:
        raise SkillError("技能状态必须是 draft / published / disabled")

    # 描述决定 Agent 能否判断该不该用这个技能，缺失就不该让它进入工作空间。
    if not description:
        raise SkillError("技能描述不能为空：它决定智能体何时选用这个技能")
    instruction = _clean_text(merged.get("instruction"), limit=MAX_INSTRUCTION, field_name="使用说明")
    usage = _clean_text(merged.get("usage"), limit=MAX_DESCRIPTION, field_name="使用说明摘要") or description
    if not instruction:
        raise SkillError("技能至少需要一句使用说明")

    triggers = _clean_list(merged.get("triggers"), limit=MAX_TRIGGERS, field_name="触发场景")
    examples = _clean_list(
        merged.get("example_questions"), limit=MAX_EXAMPLES, field_name="示例问题",
    )
    tools = _clean_list(merged.get("allowed_tools"), limit=MAX_LIST, field_name="可使用工具")
    unsupported = sorted(set(tools) - FORMAL_AGENT_TOOLS)
    if unsupported:
        raise SkillError(f"技能使用了正式分析不支持的工具：{'、'.join(unsupported)}")

    definition = SkillDefinition(
        id=skill_id,
        record_id=str(record.get("id") or "") if record else "",
        name=name,
        description=description,
        category=category,
        version=str((record or {}).get("version") or "1.0.0"),
        status=status,
        source=str(record.get("source") or "workspace") if record else "workspace",
        instruction=instruction,
        usage=usage,
        triggers=triggers,
        example_questions=examples,
        allowed_tools=tools,
        source_ids=_clean_list(merged.get("source_ids"), limit=MAX_LIST, field_name="可使用数据"),
        knowledge_document_ids=_clean_list(
            merged.get("knowledge_document_ids"), limit=MAX_LIST, field_name="可使用知识",
        ),
        mcp_server_ids=_clean_list(merged.get("mcp_server_ids"), limit=MAX_LIST, field_name="可使用 MCP"),
        metric_names=_clean_list(merged.get("metric_names"), limit=MAX_LIST, field_name="关联指标"),
        inputs=_clean_list(merged.get("inputs"), limit=MAX_LIST, field_name="输入"),
        outputs=_clean_list(merged.get("outputs"), limit=MAX_LIST, field_name="输出"),
        notes=_clean_text(merged.get("notes"), limit=4_000, field_name="备注"),
        agent_ids=(),
        created_by=str((record or {}).get("created_by") or ""),
        created_at=str((record or {}).get("created_at") or ""),
        updated_at=str((record or {}).get("updated_at") or ""),
        published_at=str((record or {}).get("published_at") or ""),
        package_path=str((record or {}).get("package_path") or ""),
    )
    return definition


def skill_from_record(record: dict[str, Any]) -> SkillDefinition:
    """Rebuild a definition from a stored record without re-running user validation."""
    # The logical id is the slug; the physical record id is kept alongside it.
    skill_id = str(record.get("slug") or record.get("id") or "")
    if not skill_id:
        raise SkillError("技能记录缺少标识")
    definition = SkillDefinition(
        id=skill_id,
        record_id=str(record.get("id") or ""),
        name=str(record.get("name") or skill_id),
        description=str(record.get("description") or ""),
        category=str(record.get("category") or "自定义"),
        version=str(record.get("version") or "1.0.0"),
        status=str(record.get("status") or "draft"),
        source=str(record.get("source") or "workspace"),
        instruction=str(record.get("instruction") or ""),
        usage=str(record.get("usage") or ""),
        triggers=tuple(record.get("triggers") or ()),
        example_questions=tuple(record.get("example_questions") or ()),
        allowed_tools=tuple(record.get("allowed_tools") or ()),
        source_ids=tuple(record.get("source_ids") or ()),
        knowledge_document_ids=tuple(record.get("knowledge_document_ids") or ()),
        mcp_server_ids=tuple(record.get("mcp_server_ids") or ()),
        metric_names=tuple(record.get("metric_names") or ()),
        inputs=tuple(record.get("inputs") or ()),
        outputs=tuple(record.get("outputs") or ()),
        notes=str(record.get("notes") or ""),
        agent_ids=tuple(record.get("agent_ids") or ()),
        created_by=str(record.get("created_by") or ""),
        created_at=str(record.get("created_at") or ""),
        updated_at=str(record.get("updated_at") or ""),
        published_at=str(record.get("published_at") or ""),
        package_path=str(record.get("package_path") or ""),
    )
    if definition.category not in CATEGORY_VALUES:
        return replace(definition, category="自定义")
    return definition


def skill_to_record(definition: SkillDefinition, workspace_id: str) -> dict[str, Any]:
    """Serialise a definition back into the KV record shape.

    ``slug`` carries the logical id; the physical ``id`` stays with the database
    so records can be created, cloned and re-keyed without losing history.
    """
    return {
        "slug": definition.id,
        "name": definition.name,
        "description": definition.description,
        "category": definition.category,
        "version": definition.version,
        "status": definition.status,
        "source": "workspace",
        "instruction": definition.instruction,
        "usage": definition.usage,
        "triggers": list(definition.triggers),
        "example_questions": list(definition.example_questions),
        "allowed_tools": list(definition.allowed_tools),
        "source_ids": list(definition.source_ids),
        "knowledge_document_ids": list(definition.knowledge_document_ids),
        "mcp_server_ids": list(definition.mcp_server_ids),
        "metric_names": list(definition.metric_names),
        "inputs": list(definition.inputs),
        "outputs": list(definition.outputs),
        "notes": definition.notes,
        "agent_ids": list(definition.agent_ids),
        "created_by": definition.created_by,
        "published_at": definition.published_at,
        "package_path": definition.package_path,
    }
