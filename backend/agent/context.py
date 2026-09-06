from __future__ import annotations

import json
from typing import Any


class ContextBuilder:
    """Build bounded context without splitting assistant/tool protocol groups."""

    def __init__(self, *, context_window: int, max_output_tokens: int):
        self.character_budget = max(4000, (context_window - max_output_tokens) * 2)

    def build(
        self,
        *,
        system: str,
        contract: dict[str, Any],
        plan: dict[str, Any] | None,
        history: list[dict[str, Any]],
        evidence_summary: list[dict[str, Any]],
        skills: list[dict[str, Any]],
        remaining_budget: dict[str, Any],
    ) -> list[dict[str, Any]]:
        governed = {
            "contract": contract,
            "plan": (plan or {}).get("payload") if plan else None,
            "evidence": evidence_summary[-20:],
            "skills": skills[:8],
            "remaining_budget": remaining_budget,
        }
        first = {"role": "system", "content": system + "\n\n受控运行上下文：\n" + json.dumps(governed, ensure_ascii=False, default=str)}
        groups = self._groups(history)
        selected: list[list[dict[str, Any]]] = []
        used = len(json.dumps(first, ensure_ascii=False))
        for group in reversed(groups):
            size = len(json.dumps(group, ensure_ascii=False, default=str))
            if selected and used + size > self.character_budget:
                break
            selected.append(group)
            used += size
        selected.reverse()
        return [first, *(message for group in selected for message in group)]

    @staticmethod
    def _groups(history: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        groups: list[list[dict[str, Any]]] = []
        index = 0
        while index < len(history):
            message = history[index]
            group = [message]
            calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            if calls:
                expected = {str(item.get("id") or "") for item in calls}
                cursor = index + 1
                while cursor < len(history) and history[cursor].get("role") == "tool":
                    group.append(history[cursor])
                    expected.discard(str(history[cursor].get("tool_call_id") or ""))
                    cursor += 1
                if expected:
                    index = cursor
                    continue
                index = cursor
            else:
                index += 1
            groups.append(group)
        return groups
