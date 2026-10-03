"""Workspace-scoped skill registry.

The registry merges two sources into one lookup:

* builtin packages loaded from disk, and
* administrator-authored workspace records (``records["skills"]``).

A workspace record with the same id as a builtin package **shadows** it, so an
administrator can adapt a builtin skill without losing the ability to restore
it.  The registry is read-only: writes go through the API layer, which is the
only place that applies authorization and audit.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.database import Database
from .loader import load_builtin_skills
from .models import SkillDefinition, SkillError, skill_from_record

COLLECTION = "skills"
VERSION_COLLECTION = "skill_versions"


class SkillRegistry:
    """Read model over the builtin packages plus the workspace records."""

    def __init__(self, database: Database, workspace_id: str):
        self.database = database
        self.workspace_id = workspace_id

    def builtin(self, *, refresh: bool = False) -> dict[str, SkillDefinition]:
        return load_builtin_skills(refresh=refresh)

    def stored(self) -> dict[str, dict[str, Any]]:
        """Workspace skills keyed by the logical id an administrator typed.

        The physical record id is an opaque ``skl_…``; every API, resolver and
        Agent reference uses the slug, so the slug is the lookup key here.
        """
        return {
            str(item.get("slug") or item["id"]): item
            for item in self.database.list(COLLECTION, workspace_id=self.workspace_id, limit=5000)
        }

    def record_for(self, skill_id: str) -> dict[str, Any] | None:
        return self.stored().get(str(skill_id or ""))

    def definitions(self, *, include_disabled: bool = True) -> list[SkillDefinition]:
        """All skills visible in this workspace, builtins first."""
        merged: dict[str, SkillDefinition] = dict(self.builtin())
        for skill_id, record in self.stored().items():
            try:
                merged[skill_id] = skill_from_record(record)
            except SkillError:
                continue
        values = list(merged.values())
        if not include_disabled:
            values = [item for item in values if item.status != "disabled"]
        return sorted(values, key=lambda item: (item.source != "builtin", item.category, item.name))

    def get(self, skill_id: str) -> SkillDefinition | None:
        skill_id = str(skill_id or "")
        if not skill_id:
            return None
        record = self.stored().get(skill_id)
        if record is not None:
            try:
                return skill_from_record(record)
            except SkillError:
                return None
        return self.builtin().get(skill_id)

    def get_many(self, skill_ids: Iterable[str]) -> list[SkillDefinition]:
        found: list[SkillDefinition] = []
        for skill_id in dict.fromkeys(str(value) for value in skill_ids if value):
            definition = self.get(skill_id)
            if definition is not None:
                found.append(definition)
        return found

    def published(self) -> list[SkillDefinition]:
        return [item for item in self.definitions() if item.status == "published"]

    def ids(self) -> list[str]:
        return [item.id for item in self.definitions()]

    def agents_using(self, skill_id: str) -> list[str]:
        return sorted({
            str(agent["id"])
            for agent in self.database.list("agent_definitions", workspace_id=self.workspace_id, limit=5000)
            if skill_id in (agent.get("skill_ids") or []) or skill_id == str(agent.get("skill_id") or "")
        })


def registry_for(database: Database, workspace_id: str) -> SkillRegistry:
    return SkillRegistry(database, workspace_id)
