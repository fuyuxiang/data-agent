"""Resolve the live Agent independently of its editable draft.

Only publication changes the live version. The fallback handles databases
created before ``published_version`` existed without publishing a draft.
"""

from __future__ import annotations

from copy import deepcopy
import json

from ..core.database import Database, utcnow
from .authorization import filter_authorized_sources


def dynamic_source_scope(item: dict) -> bool:
    """Only the built-in entry Agent can follow the actor's authorized sources."""
    return bool(
        item.get("builtin") and item.get("id") == "agent-superskill"
        and (item.get("source_scope_mode") == "authorized"
             or ("source_scope_mode" not in item and not item.get("source_ids")))
    )


def published_agent(database: Database, item: dict | None) -> dict | None:
    if not item or item.get("archived_at"):
        return None
    wid = str(item.get("workspace_id") or "default")
    version = item.get("published_version")
    record = None
    if version:
        record = database.get("agent_versions", f"{item['id']}:{version}", workspace_id=wid)
        if not record:
            return None
    elif "published_version" in item:
        return None
    elif item.get("status") == "published":
        version = int(item.get("version") or 1)
        record = database.get("agent_versions", f"{item['id']}:{version}", workspace_id=wid)
    else:
        # Old builds changed a published Agent to draft on every save. Recover
        # the last explicitly published snapshot, never the edited definition.
        with database.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM records WHERE collection='agent_versions' AND workspace_id=? "
                "AND archived_at IS NULL AND json_extract(payload, '$.agent_id')=? "
                "AND json_type(payload, '$.snapshot')='object' "
                "ORDER BY CAST(json_extract(payload, '$.version') AS INTEGER) DESC LIMIT 1",
                (wid, item["id"]),
            ).fetchone()
        record = json.loads(row["payload"]) if row else None
        if not record:
            return None
        version = int(record["version"])
    snapshot = deepcopy(record["snapshot"] if record else item)
    # Identity and ownership are not editable configuration fields.
    snapshot.update({key: deepcopy(item.get(key)) for key in ("id", "workspace_id", "created_by", "builtin")})
    snapshot.update({
        "version": int(version), "published_version": int(version), "status": "published",
        "published_at": (record or {}).get("published_at") or item.get("published_at"),
        "has_unpublished_changes": False,
    })
    snapshot["source_scope_mode"] = "authorized" if dynamic_source_scope(snapshot) else "bound"
    return snapshot


def published_agents(database: Database, workspace_id: str) -> list[dict]:
    return [live for item in database.list("agent_definitions", workspace_id=workspace_id, limit=5000)
            if (live := published_agent(database, item)) is not None]


def agent_references(database: Database, workspace_id: str | None, field: str, resource_id: str | set[str]) -> list[dict]:
    """Find active drafts or live versions, excluding historical publications."""
    resource_ids = {resource_id} if isinstance(resource_id, str) else set(resource_id)
    if not resource_ids:
        return []
    result = []
    # Dependency guards must not use the UI catalog's 5000-row listing limit.
    with database.connect() as connection:
        where = " AND workspace_id=?" if workspace_id is not None else ""
        rows = connection.execute(
            "SELECT payload FROM records WHERE collection='agent_definitions' "
            f"AND archived_at IS NULL{where}", (workspace_id,) if workspace_id is not None else (),
        ).fetchall()
    for row in rows:
        item = json.loads(row["payload"])
        for configuration in (item, published_agent(database, item)):
            if not configuration:
                continue
            value = configuration.get(field)
            referenced = bool(resource_ids.intersection(value)) if isinstance(value, list) else value in resource_ids
            if field == "skill_ids":
                referenced = referenced or configuration.get("skill_id") in resource_ids
            if field == "source_ids" and dynamic_source_scope(configuration):
                referenced = False
            if field == "source_ids" and not referenced:
                # A dynamic source catalog still has explicit dependencies when
                # the Agent binds metrics defined on a particular source.
                wid = str(item.get("workspace_id") or "default")
                for metric_id in configuration.get("metric_ids") or []:
                    metric = database.get("semantic_metrics", str(metric_id), workspace_id=wid)
                    model = database.get("semantic_models", str((metric or {}).get("model_id") or ""), workspace_id=wid)
                    if (model or {}).get("source_id") in resource_ids:
                        referenced = True
                        break
            if referenced:
                result.append(item)
                break
    return result


def agent_source_ids(database: Database, item: dict, actor_id: str) -> list[str]:
    if dynamic_source_scope(item):
        wid = str(item.get("workspace_id") or "default")
        return [str(source["id"]) for source in filter_authorized_sources(
            database, database.list("sources", workspace_id=wid, limit=5000),
            workspace_id=wid, actor_id=actor_id, action="analyze",
        ) if source.get("status") == "ready"]
    return list(dict.fromkeys(str(value) for value in item.get("source_ids") or []))


def preserve_publication(database: Database, item: dict) -> dict:
    """Materialize a legacy live definition before overwriting its draft."""
    live = published_agent(database, item)
    if not live:
        return {"published_version": None, "published_at": None}
    wid = str(item.get("workspace_id") or "default")
    record_id = f"{item['id']}:{live['version']}"
    if not database.get("agent_versions", record_id, workspace_id=wid):
        database.put("agent_versions", {
            "id": record_id, "workspace_id": wid, "agent_id": item["id"],
            "version": live["version"], "snapshot": deepcopy(live),
            "published_by": item.get("created_by"), "published_at": live.get("published_at") or utcnow(),
        }, workspace_id=wid)
    return {"published_version": live["version"], "published_at": live.get("published_at")}
