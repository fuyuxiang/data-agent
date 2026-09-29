"""A bounded, editable analysis brief built from the user's selected scope."""

from __future__ import annotations

import json
import re
from typing import Any

from flask import current_app

from .models import resolve_provider
from .usage import ensure_quota, record_usage


def _parsed_object(content: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content or "").strip())
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def suggest_contract(
    *, question: str, source_names: list[str], attachment_names: list[str],
    provider_id: str | None, workspace_id: str,
) -> dict[str, Any] | None:
    """Suggest the four visible fields; the user still edits and confirms them.

    Only names of already authorized sources and this run's attachments are sent
    to the model. Model output cannot alter the enforced source scope.
    """
    provider, client = resolve_provider(provider_id, workspace_id)
    if not provider or not client:
        return None
    database = current_app.extensions["meridian_db"]
    quota = ensure_quota(database, workspace_id)
    context = {
        "question": question[:4000],
        "selected_sources": source_names[:100],
        "attached_files": attachment_names[:20],
    }
    response = client.chat.completions.create(
        model=provider["model"],
        messages=[
            {"role": "system", "content": (
                "你负责为企业数据分析生成供用户确认的简短任务草稿。"
                "只输出 JSON 对象，字段为 objective、coverage、dimensions、deliverables、unresolved。"
                "dimensions、deliverables、unresolved 是字符串数组。"
                "不得虚构已选来源之外的数据、字段、指标、时间、权限或分析结论。"
                "缺少的关键口径写入 unresolved，不要补造。不要输出内部思考过程。"
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        temperature=0,
        max_tokens=max(1, min(700, int(quota["remaining"]))),
    )
    usage = getattr(response, "usage", None)
    if usage:
        record_usage(database, workspace_id, {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "model": provider["model"],
        }, operation="contract_suggestion")
    value = _parsed_object(response.choices[0].message.content or "")
    if not value:
        return None
    result: dict[str, Any] = {}
    for key in ("objective", "coverage"):
        text = str(value.get(key) or "").strip()
        if text:
            result[key] = text[:4000]
    for key in ("dimensions", "deliverables", "unresolved"):
        items = value.get(key)
        if isinstance(items, list):
            result[key] = [str(item).strip()[:200] for item in items[:12] if str(item).strip()]
    return result if result.get("objective") and result.get("coverage") else None
