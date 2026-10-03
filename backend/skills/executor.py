"""Skill execution.

A skill does not run its own loop.  It *narrows and focuses* the governed
analysis loop: it decides which tools the run may reach, what the model is told
the skill's job is, and which evidence must exist before the answer is accepted.

That keeps a single decision loop in the product (an explicit V2 requirement)
while still making skills a real runtime capability rather than prompt text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..agent.contracts import RunContext
from .context import SkillContext
from .models import SkillDefinition
from .permissions import skill_requirements

# Tools that are always available to a skill: without them the model could not
# ask for clarification, read back its own results, or validate them.
_ALWAYS_ALLOWED = frozenset({"ask_user", "structured_output", "read_tool_result", "validate_result"})

# When a skill reaches for raw data, the governed metric tools are a safer
# subset and are added automatically.
_DATA_TOOLS = frozenset({"query_data", "profile_data", "run_analysis"})


@dataclass(frozen=True)
class SkillExecution:
    """Everything a run needs in order to apply one skill."""

    skill: SkillDefinition
    allowed_tools: frozenset[str]
    model_blocks: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()
    missing_resources: tuple[str, ...] = ()

    def to_public(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill.id,
            "name": self.skill.name,
            "allowed_tools": sorted(self.allowed_tools),
            "warnings": list(self.warnings),
            "missing_resources": list(self.missing_resources),
        }


def effective_tool_set(
    definition: SkillDefinition,
    *,
    runtime_tools: Iterable[str] | None = None,
) -> frozenset[str]:
    """Intersect a skill's declared tools with what the runtime actually has."""
    available = set(runtime_tools) if runtime_tools is not None else None
    declared = set(definition.allowed_tools)
    if declared:
        tools = declared | _ALWAYS_ALLOWED
    else:
        # A skill with no explicit tool list is a *guidance* skill: it shapes how
        # the model reasons, not which tools it may call.
        tools = set(_ALWAYS_ALLOWED) | _DATA_TOOLS
    if _DATA_TOOLS & tools:
        tools |= {"list_semantic_metrics", "query_metric"}
    if available is not None:
        tools &= set(available) | _ALWAYS_ALLOWED
    return frozenset(tools)


def _focus_prompt(definition: SkillDefinition) -> str:
    parts = [f"【{definition.name}】{definition.description}"]
    if definition.outputs:
        parts.append("应产出：" + "、".join(definition.outputs))
    return " ".join(part for part in parts if part)


class SkillExecutor:
    """Turn selected skill definitions into a runnable plan."""

    def __init__(self, context: SkillContext, *, runtime_tools: Iterable[str] | None = None):
        self.context = context
        self.runtime_tools = list(runtime_tools) if runtime_tools is not None else None

    def tools_for(self, definition: SkillDefinition) -> frozenset[str]:
        return effective_tool_set(definition, runtime_tools=self.runtime_tools)

    def prepare(
        self,
        definitions: Sequence[SkillDefinition],
        *,
        run_context: RunContext | None = None,
    ) -> list[SkillExecution]:
        """Build one :class:`SkillExecution` per skill, narrowing tools as we go.

        When several skills are selected their tool sets are unioned — a skill
        must never be able to *remove* another skill's capability.
        """
        executions: list[SkillExecution] = []
        allowed_by_run = (
            set(run_context.allowed_tool_ids) if run_context is not None else None
        )
        for definition in definitions:
            tools = set(self.tools_for(definition))
            if allowed_by_run is not None:
                tools &= allowed_by_run | _ALWAYS_ALLOWED
            gaps = skill_requirements(definition).missing(self.context.available)
            warnings: list[str] = []
            if gaps:
                warnings.append(f"当前运行缺少{'、'.join(gaps)}资源，相关能力可能不可用")
            executions.append(SkillExecution(
                skill=definition,
                allowed_tools=frozenset(tools),
                model_blocks=({**definition.to_model_block(), "allowed_tools": sorted(tools)},),
                warnings=tuple(warnings),
                missing_resources=gaps,
            ))
        return executions

    @staticmethod
    def combined_tools(executions: Sequence[SkillExecution]) -> frozenset[str]:
        combined: set[str] = set(_ALWAYS_ALLOWED)
        for execution in executions:
            combined |= set(execution.allowed_tools)
        return frozenset(combined)

    @staticmethod
    def model_blocks(executions: Sequence[SkillExecution]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for execution in executions:
            for block in execution.model_blocks:
                if block["id"] in seen:
                    continue
                seen.add(block["id"])
                blocks.append(block)
        return blocks


@dataclass
class SkillRunLedger:
    """Records which skills a run actually used, for run-detail replay."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, resolution: Any, *, source: str = "auto") -> None:
        for item in getattr(resolution, "selected", ()) or ():
            self.entries.append({
                "skill_id": getattr(item, "id", str(item)),
                "name": getattr(item, "name", ""),
                "source": source,
                "reasons": [
                    reason for candidate in getattr(resolution, "candidates", ())
                    if candidate.definition.id == getattr(item, "id", "")
                    for reason in candidate.reasons
                ],
            })

    def to_records(self) -> list[dict[str, Any]]:
        return list(self.entries)
