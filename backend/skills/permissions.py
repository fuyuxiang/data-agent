"""Permission filtering for skills.

A skill may declare that it needs particular data sources, knowledge documents
or MCP servers.  Before a skill is offered to a user — or bound to an Agent —
those requirements are checked against what the actor may actually reach.
A skill whose requirements the actor cannot satisfy is hidden rather than
offered and failed later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..core.database import Database
from .models import SkillDefinition


@dataclass(frozen=True)
class SkillRequirements:
    """What a skill needs in order to be usable."""

    source_ids: frozenset[str] = frozenset()
    knowledge_document_ids: frozenset[str] = frozenset()
    mcp_server_ids: frozenset[str] = frozenset()

    def missing(self, available: "AvailableResources") -> tuple[str, ...]:
        gaps: list[str] = []
        if self.source_ids - available.source_ids:
            gaps.append("数据")
        if self.knowledge_document_ids - available.knowledge_document_ids:
            gaps.append("知识")
        if self.mcp_server_ids - available.mcp_server_ids:
            gaps.append("MCP")
        return tuple(gaps)


@dataclass(frozen=True)
class AvailableResources:
    """What one actor can reach in one workspace.

    ``metric_names`` is deliberately *not* a requirement: a skill that prefers
    governed metrics still works without them by falling back to exploratory
    analysis.  It is carried here so the executor can pass the hint along.
    """

    source_ids: frozenset[str] = frozenset()
    knowledge_document_ids: frozenset[str] = frozenset()
    mcp_server_ids: frozenset[str] = frozenset()
    metric_names: frozenset[str] = frozenset()


def skill_requirements(definition: SkillDefinition) -> SkillRequirements:
    return SkillRequirements(
        source_ids=frozenset(definition.source_ids),
        knowledge_document_ids=frozenset(definition.knowledge_document_ids),
        mcp_server_ids=frozenset(definition.mcp_server_ids),
    )


def available_resources(
    database: Database,
    workspace_id: str,
    actor_id: str,
    *,
    source_ids: Iterable[str] | None = None,
) -> AvailableResources:
    """Resolve the actor's reachable sources, knowledge, MCP servers and metrics.

    ``source_ids`` narrows the answer to a specific run or conversation scope;
    ``None`` means "everything the actor may reach in this workspace".
    """
    from ..services.authorization import filter_authorized_sources, require_sources_access
    from ..services.semantic import visible_metrics

    if source_ids is None:
        reachable_sources = frozenset(
            str(item["id"])
            for item in filter_authorized_sources(
                database,
                database.list("sources", workspace_id=workspace_id, limit=5000),
                workspace_id=workspace_id, actor_id=actor_id, action="analyze",
            )
        )
    else:
        # Raises PermissionError when any id is out of scope, which is the point:
        # a run may only ever use sources the actor can still read.
        require_sources_access(
            database, [str(value) for value in source_ids],
            workspace_id=workspace_id, actor_id=actor_id, action="analyze",
        )
        reachable_sources = frozenset(str(value) for value in source_ids)

    knowledge = frozenset(
        str(item["id"])
        for item in database.list("knowledge_documents", workspace_id=workspace_id, limit=5000)
        if item.get("enabled", True) and item.get("visibility") != "analysis_attachment"
    )
    mcp = frozenset(
        str(item["id"])
        for item in database.list("mcp_servers", workspace_id=workspace_id, limit=5000)
        if item.get("enabled", True) and item.get("status") == "connected"
    )
    metrics = frozenset(
        str(item.get("name") or "") for item in visible_metrics(database, workspace_id, actor_id)
    )
    return AvailableResources(reachable_sources, knowledge, mcp, metrics)


def filter_visible(
    definitions: Iterable[SkillDefinition],
    available: AvailableResources,
    *,
    include_disabled: bool = False,
) -> list[SkillDefinition]:
    """Keep only skills the actor can actually run."""
    visible: list[SkillDefinition] = []
    for definition in definitions:
        if definition.status == "disabled" and not include_disabled:
            continue
        if skill_requirements(definition).missing(available):
            continue
        visible.append(definition)
    return visible


def unavailable_reason(definition: SkillDefinition, available: AvailableResources) -> str:
    gaps = skill_requirements(definition).missing(available)
    if not gaps:
        return ""
    return f"缺少可用的{'、'.join(gaps)}资源"
