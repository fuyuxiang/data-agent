"""Skill evaluation: does a skill actually work?

Two levels of check, both cheap enough to run from the skill editor:

* **static** — the definition is well formed, its tools exist in the runtime,
  and its declared resources are reachable by the acting user.
* **behavioural** — a sample question resolves to this skill, and the resulting
  tool set is non-empty and consistent with what the skill claims to do.

A skill that cannot pass the static check cannot be published.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..core.database import Database
from .context import SkillContext
from .executor import SkillExecutor
from .models import FORMAL_AGENT_TOOLS, SkillDefinition, SkillError
from .permissions import available_resources, skill_requirements
from .registry import SkillRegistry
from .resolver import SkillResolver


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def to_public(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class Evaluation:
    skill_id: str
    checks: tuple[Check, ...]
    samples: tuple[dict[str, Any], ...] = ()

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_public(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "passed": self.passed,
            "checks": [check.to_public() for check in self.checks],
            "samples": list(self.samples),
        }


def static_checks(
    database: Database,
    workspace_id: str,
    actor_id: str,
    definition: SkillDefinition,
    *,
    runtime_tools: Sequence[str] | None = None,
) -> list[Check]:
    checks: list[Check] = []

    if not definition.instruction and not definition.usage:
        checks.append(Check("使用说明", False, "技能缺少使用说明"))
    else:
        checks.append(Check("使用说明", True, f"{len(definition.instruction or definition.usage)} 字"))

    if not definition.description:
        checks.append(Check("描述", False, "技能缺少描述，用户无法判断何时使用"))
    else:
        checks.append(Check("描述", True))

    if not definition.triggers and not definition.example_questions:
        checks.append(Check("触发条件", False, "既没有触发场景也没有示例问题，自动选择会失效"))
    else:
        checks.append(Check(
            "触发条件", True,
            f"{len(definition.triggers)} 个触发场景 · {len(definition.example_questions)} 个示例问题",
        ))

    unsupported = sorted(set(definition.allowed_tools) - FORMAL_AGENT_TOOLS)
    if unsupported:
        checks.append(Check("工具声明", False, f"使用了未知工具：{'、'.join(unsupported)}"))
    elif runtime_tools is not None:
        unreachable = sorted(set(definition.allowed_tools) - set(runtime_tools))
        if unreachable:
            checks.append(Check("工具声明", False, f"当前运行环境没有：{'、'.join(unreachable)}"))
        else:
            checks.append(Check("工具声明", True, f"{len(definition.allowed_tools)} 个工具可用"))
    else:
        checks.append(Check("工具声明", True, f"{len(definition.allowed_tools)} 个工具"))

    available = available_resources(database, workspace_id, actor_id)
    gaps = skill_requirements(definition).missing(available)
    if gaps:
        checks.append(Check("资源依赖", False, f"缺少可用的{'、'.join(gaps)}资源"))
    else:
        checks.append(Check("资源依赖", True))

    return checks


def behavioural_samples(
    database: Database,
    workspace_id: str,
    actor_id: str,
    definition: SkillDefinition,
    *,
    registry: SkillRegistry | None = None,
) -> list[dict[str, Any]]:
    """Resolve each example question and report whether this skill wins."""
    candidates = (registry or SkillRegistry(database, workspace_id)).definitions(include_disabled=True)
    resolver = SkillResolver(candidates)
    executor = SkillExecutor(SkillContext(
        workspace_id=workspace_id, actor_id=actor_id, run_id="", session_id="",
    ))
    results: list[dict[str, Any]] = []
    for question in definition.example_questions[:5]:
        resolution = resolver.resolve(question)
        matched = definition.id in {item.id for item in resolution.selected}
        top = resolution.candidates[0].definition.id if resolution.candidates else ""
        results.append({
            "question": question,
            "selected": matched,
            "matched_by": "示例问题" if matched else ("被其他技能优先匹配" if top else "无候选"),
            "tools": sorted(executor.tools_for(definition)),
        })
    return results


def evaluate(
    database: Database,
    workspace_id: str,
    actor_id: str,
    definition: SkillDefinition,
    *,
    runtime_tools: Sequence[str] | None = None,
    registry: SkillRegistry | None = None,
    with_samples: bool = True,
) -> Evaluation:
    checks = static_checks(
        database, workspace_id, actor_id, definition, runtime_tools=runtime_tools,
    )
    if not with_samples:
        return Evaluation(definition.id, tuple(checks))
    if any(not check.passed for check in checks):
        return Evaluation(definition.id, tuple(checks))
    samples = behavioural_samples(database, workspace_id, actor_id, definition, registry=registry)
    return Evaluation(definition.id, tuple(checks), tuple(samples))


def assert_publishable(evaluation: Evaluation) -> None:
    if not evaluation.passed:
        failed = "；".join(f"{check.name}：{check.detail}" for check in evaluation.checks if not check.passed)
        raise SkillError(f"技能未通过发布校验：{failed}")
