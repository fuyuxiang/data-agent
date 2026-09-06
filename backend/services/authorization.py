from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable

from ..core.database import Database


@dataclass(frozen=True)
class SourceAccessDecision:
    allowed: bool
    reason: str
    workspace_id: str
    source_id: str
    actor_id: str
    action: str


def _workspace_membership(database: Database, workspace_id: str, actor_id: str) -> dict | None:
    users_exist = bool(database.list("users", include_archived=True, limit=1))
    if not users_exist and actor_id == "local-default":
        return {"workspace_id": workspace_id, "user_id": actor_id, "role": "owner"}
    return next(
        (
            item
            for item in database.list("workspace_members", workspace_id=workspace_id, limit=5000)
            if str(item.get("user_id")) == actor_id and item.get("enabled", True)
        ),
        None,
    )


def decide_source_access(
    database: Database,
    source: dict | None,
    *,
    workspace_id: str,
    actor_id: str,
    action: str = "read",
) -> SourceAccessDecision:
    source_id = str((source or {}).get("id") or "")
    actor_id = str(actor_id or "")
    if not source or str(source.get("workspace_id") or "default") != workspace_id:
        return SourceAccessDecision(False, "not_found", workspace_id, source_id, actor_id, action)
    membership = _workspace_membership(database, workspace_id, actor_id)
    if not membership:
        return SourceAccessDecision(False, "not_workspace_member", workspace_id, source_id, actor_id, action)
    if action in {"create", "update", "delete", "refresh", "query", "analyze", "export"}:
        if str(membership.get("role") or "viewer") not in {"owner", "editor"}:
            return SourceAccessDecision(False, "workspace_read_only", workspace_id, source_id, actor_id, action)
    allowed_users = source.get("authorized_user_ids")
    if isinstance(allowed_users, list) and actor_id not in {str(value) for value in allowed_users}:
        return SourceAccessDecision(False, "source_acl", workspace_id, source_id, actor_id, action)
    return SourceAccessDecision(True, "allowed", workspace_id, source_id, actor_id, action)


def require_source_access(
    database: Database,
    source_id: str,
    *,
    workspace_id: str,
    actor_id: str,
    action: str = "read",
) -> dict:
    source = database.get("sources", str(source_id), workspace_id=workspace_id)
    decision = decide_source_access(
        database, source, workspace_id=workspace_id, actor_id=actor_id, action=action,
    )
    if not decision.allowed:
        database.audit(
            "authorization.denied", workspace_id=workspace_id, actor=actor_id,
            object_type="source", object_id=str(source_id),
            detail={"action": action, "reason": decision.reason},
        )
        if decision.reason == "not_found":
            raise FileNotFoundError(f"sources 记录不存在：{source_id}")
        raise PermissionError("无权访问选定数据源")
    return source


def require_sources_access(
    database: Database,
    source_ids: Iterable[str],
    *,
    workspace_id: str,
    actor_id: str,
    action: str = "read",
) -> list[dict]:
    return [
        require_source_access(
            database, str(source_id), workspace_id=workspace_id, actor_id=actor_id, action=action,
        )
        for source_id in source_ids
    ]


def filter_authorized_sources(
    database: Database,
    sources: Iterable[dict],
    *,
    workspace_id: str,
    actor_id: str,
    action: str = "read",
) -> list[dict]:
    return [
        source
        for source in sources
        if decide_source_access(
            database, source, workspace_id=workspace_id, actor_id=actor_id, action=action,
        ).allowed
    ]


def inherited_source_policy(source: dict) -> dict:
    """Copy security-relevant metadata to snapshots and derived datasets."""
    keys = (
        "authorized_user_ids", "classification", "sensitivity", "retention_policy",
        "row_policy", "column_policies", "purpose_constraints",
    )
    return {key: source[key] for key in keys if key in source}


def inherited_sources_policy(sources: Iterable[dict]) -> dict:
    """Return the most restrictive representable policy for derived data.

    An absent source ACL means every workspace member may use that source, so only
    explicit ACLs participate in the intersection.  A derived data set must never
    be visible to someone who could not read every restricted parent.
    """
    parents = list(sources)
    policies = [
        {str(value) for value in source.get("authorized_user_ids") or []}
        for source in parents if isinstance(source.get("authorized_user_ids"), list)
    ]
    result: dict = {}
    if policies:
        allowed = set.intersection(*policies)
        result["authorized_user_ids"] = sorted(allowed)

    levels = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
    for field in ("classification", "sensitivity"):
        values = [str(source.get(field) or "").lower() for source in parents if source.get(field)]
        if values:
            result[field] = max(values, key=lambda value: levels.get(value, 2))
    return result


def require_result_access(
    database: Database,
    result: dict | None,
    *,
    workspace_id: str,
    actor_id: str,
    action: str = "read",
) -> dict:
    if not result or str(result.get("workspace_id") or "default") != workspace_id:
        raise FileNotFoundError("查询结果不存在")
    require_sources_access(
        database, result.get("source_ids") or [], workspace_id=workspace_id,
        actor_id=actor_id, action=action,
    )
    return result


def require_session_access(
    database: Database,
    session_id: str,
    *,
    workspace_id: str,
    actor_id: str,
) -> dict:
    """Return a private conversation only to its owner.

    Explicitly workspace-visible sessions are the only shareable form. Legacy
    records without an owner remain visible to the workspace owner so they can
    be migrated without exposing them to every member.
    """
    record = database.get("sessions", str(session_id), workspace_id=workspace_id)
    if not record:
        raise FileNotFoundError(f"sessions 记录不存在：{session_id}")
    membership = _workspace_membership(database, workspace_id, actor_id)
    if not membership:
        raise FileNotFoundError(f"sessions 记录不存在：{session_id}")
    if record.get("visibility") == "workspace" or str(record.get("owner_id") or "") == actor_id:
        return record
    if not record.get("owner_id") and membership.get("role") == "owner":
        return record
    database.audit(
        "authorization.denied", workspace_id=workspace_id, actor=actor_id,
        object_type="session", object_id=str(session_id),
        detail={"action": "read", "reason": "session_owner"},
    )
    # Deliberately hide whether another user's private conversation exists.
    raise FileNotFoundError(f"sessions 记录不存在：{session_id}")


def filter_authorized_sessions(
    database: Database,
    sessions: Iterable[dict],
    *,
    workspace_id: str,
    actor_id: str,
) -> list[dict]:
    visible = []
    for record in sessions:
        try:
            require_session_access(
                database, str(record.get("id") or ""), workspace_id=workspace_id,
                actor_id=actor_id,
            )
        except (FileNotFoundError, PermissionError):
            continue
        visible.append(record)
    return visible


def require_job_access(
    database: Database,
    job: dict | None,
    *,
    workspace_id: str,
    actor_id: str,
) -> dict:
    if not job or str(job.get("workspace_id") or "default") != workspace_id:
        raise FileNotFoundError("后台任务不存在")
    run_id = str(job.get("run_id") or "")
    if run_id:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT actor_id,source_scope FROM agent_runs WHERE id=? AND workspace_id=?",
                (run_id, workspace_id),
            ).fetchone()
        if not row or str(row["actor_id"]) != actor_id:
            raise FileNotFoundError("后台任务不存在")
        require_sources_access(
            database, json.loads(row["source_scope"] or "[]"), workspace_id=workspace_id,
            actor_id=actor_id,
        )
        return job
    session_id = str(job.get("session_id") or "")
    if session_id and database.get("sessions", session_id, workspace_id=workspace_id):
        require_session_access(
            database, session_id, workspace_id=workspace_id, actor_id=actor_id,
        )
        return job
    membership = _workspace_membership(database, workspace_id, actor_id)
    if membership and membership.get("role") == "owner":
        return job
    raise FileNotFoundError("后台任务不存在")


def filter_authorized_jobs(
    database: Database,
    jobs: Iterable[dict],
    *,
    workspace_id: str,
    actor_id: str,
) -> list[dict]:
    visible = []
    for job in jobs:
        try:
            require_job_access(
                database, job, workspace_id=workspace_id, actor_id=actor_id,
            )
        except (FileNotFoundError, PermissionError):
            continue
        visible.append(job)
    return visible


def dashboard_source_ids(database: Database, dashboard: dict) -> list[str]:
    workspace_id = str(dashboard.get("workspace_id") or "default")
    source_ids: list[str] = []
    for widget in dashboard.get("widgets") or []:
        raw = widget.get("source_ids")
        if not isinstance(raw, list):
            raw = [widget["source_id"]] if widget.get("source_id") else []
        source_ids.extend(str(value) for value in raw if str(value))
        result_ids = [str(widget.get("result_id") or "")]
        chart_id = str(widget.get("chart_id") or "")
        chart = database.get("charts", chart_id, workspace_id=workspace_id) if chart_id else None
        if chart:
            result_ids.append(str(chart.get("result_id") or ""))
            if chart.get("source_id"):
                source_ids.append(str(chart["source_id"]))
        for result_id in result_ids:
            result = database.get("query_results", result_id, workspace_id=workspace_id) if result_id else None
            if result:
                source_ids.extend(str(value) for value in result.get("source_ids") or [])
    return list(dict.fromkeys(source_ids))


def require_dashboard_access(
    database: Database, dashboard: dict | None, *, workspace_id: str,
    actor_id: str, action: str = "read",
) -> dict:
    if not dashboard or str(dashboard.get("workspace_id") or "default") != workspace_id:
        raise FileNotFoundError("看板不存在")
    require_sources_access(
        database, dashboard_source_ids(database, dashboard), workspace_id=workspace_id,
        actor_id=actor_id, action=action,
    )
    return dashboard


def require_artifact_access(
    database: Database, artifact: dict | None, *, workspace_id: str,
    actor_id: str, action: str = "read",
) -> dict:
    if not artifact or str(artifact.get("workspace_id") or "default") != workspace_id:
        raise FileNotFoundError("成果不存在")
    source_ids = [str(value) for value in artifact.get("source_ids") or []]
    run_id = str(artifact.get("run_id") or "")
    if run_id:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT actor_id,source_scope FROM agent_runs WHERE id=? AND workspace_id=?",
                (run_id, workspace_id),
            ).fetchone()
        if not row or str(row["actor_id"]) != actor_id:
            raise FileNotFoundError("成果不存在")
        source_ids.extend(str(value) for value in json.loads(row["source_scope"] or "[]"))
    require_sources_access(
        database, list(dict.fromkeys(source_ids)), workspace_id=workspace_id,
        actor_id=actor_id, action=action,
    )
    return artifact
