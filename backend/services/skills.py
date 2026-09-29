from __future__ import annotations

DEFAULT_SKILLS = [
    {"id": "executive-summary", "name": "经营摘要", "description": "提炼变化、原因、风险和建议", "instruction": "按结论、证据、风险、行动建议四段输出。", "enabled": True, "source": "builtin"},
    {"id": "quality-audit", "name": "数据质量审计", "description": "检查缺失、重复、异常和类型问题", "instruction": "先量化质量问题，再给出不破坏原始数据的处理建议。", "enabled": True, "source": "builtin"},
    {"id": "trend-diagnosis", "name": "趋势诊断", "description": "识别趋势、季节性和突变", "instruction": "比较环比与同比，标注异常点和可能原因。", "enabled": True, "source": "builtin"},
]

# This is the single contract for tools callable by a formal analysis run.
FORMAL_AGENT_TOOLS = frozenset({
    "query_knowledge", "get_schema", "get_table_detail", "list_semantic_metrics",
    "query_metric", "query_data", "profile_data", "run_analysis", "select_chart",
    "generate_chart", "ask_user", "structured_output",
    "load_analysis_skill", "read_tool_result", "validate_result", "update_plan",
    "search_mcp_tools", "warehouse_catalog", "warehouse_explain",
    "warehouse_query", "warehouse_spark_submit",
})


def unsupported_formal_tools(skill: dict) -> list[str]:
    values = skill.get("allowed_tools") or []
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise SkillError("allowed_tools 必须是工具名数组")
    return sorted(set(values) - FORMAL_AGENT_TOOLS)


def require_formal_skill(skill: dict) -> None:
    unsupported = unsupported_formal_tools(skill)
    if unsupported:
        raise SkillError(f"Skill 使用了正式分析不支持的工具：{', '.join(unsupported)}")


class SkillError(ValueError):
    pass


def get_skill(name: str | None, workspace_id: str) -> dict | None:
    return next((dict(item) for item in DEFAULT_SKILLS if item["id"] == name), None)


def public_skill(skill: dict, *, include_prompt: bool = False) -> dict:
    result = dict(skill)
    if not include_prompt:
        result.pop("instruction", None)
    result["formal_compatible"] = not unsupported_formal_tools(skill)
    result["unsupported_tools"] = unsupported_formal_tools(skill)
    return result
