"""Execution context handed to a skill at run time.

The context is the bridge between a skill definition and the governed analysis
runtime.  It carries *which* resources are in scope, *which* tools the skill is
allowed to reach, and the conversation artefacts (metrics, knowledge, MCP) it may
reference — all already permission-checked by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.database import Database
from .permissions import AvailableResources, available_resources


@dataclass(frozen=True)
class SkillContext:
    workspace_id: str
    actor_id: str
    run_id: str
    session_id: str
    source_ids: tuple[str, ...] = ()
    available: AvailableResources = field(default_factory=AvailableResources)
    metric_hints: tuple[str, ...] = ()
    knowledge_hints: tuple[str, ...] = ()
    mcp_hints: tuple[str, ...] = ()


def build_skill_context(
    database: Database,
    run: dict[str, Any],
    *,
    metric_hints: tuple[str, ...] = (),
) -> SkillContext:
    """Build a permission-checked context for one analysis run."""
    workspace_id = str(run["workspace_id"])
    actor_id = str(run.get("actor_id") or "")
    available = available_resources(
        database, workspace_id, actor_id, source_ids=list(run.get("source_scope") or []),
    )
    return SkillContext(
        workspace_id=workspace_id,
        actor_id=actor_id,
        run_id=str(run["id"]),
        session_id=str(run.get("session_id") or ""),
        source_ids=tuple(str(value) for value in (run.get("source_scope") or [])),
        available=available,
        metric_hints=metric_hints,
        knowledge_hints=tuple(sorted(available.knowledge_document_ids)),
        mcp_hints=tuple(sorted(available.mcp_server_ids)),
    )
