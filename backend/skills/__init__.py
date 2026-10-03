"""Skill runtime for 数擎 Data Agent.

A Skill is a professional capability that an Agent can discover and execute.
It is deliberately *not* a prompt template, a tool list, an MCP server or a
workflow: it bundles a role description, the tools and resources it may use,
the situations it triggers on, and the shape of what it produces.
"""

from .context import SkillContext, build_skill_context
from .executor import SkillExecution, SkillExecutor
from .loader import load_builtin_skills, read_package
from .models import SkillDefinition, SkillError
from .permissions import filter_visible, skill_requirements
from .registry import SkillRegistry, registry_for
from .resolver import Resolution, SkillResolver

__all__ = [
    "Resolution",
    "SkillContext",
    "SkillDefinition",
    "SkillError",
    "SkillExecution",
    "SkillExecutor",
    "SkillRegistry",
    "SkillResolver",
    "build_skill_context",
    "filter_visible",
    "load_builtin_skills",
    "read_package",
    "registry_for",
    "skill_requirements",
]
