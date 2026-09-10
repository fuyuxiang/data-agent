from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, current_app, request

from ..agent.store import RunStore
from ..core.database import utcnow
from ..services.authorization import filter_authorized_sources, require_source_access, require_sources_access
from ..services.results.manifests import ResultService
from ..services.semantic import visible_metrics
from ..services.scheduler import start_analysis_subscription, subscription_cron
from ..services.skills import get_skill
from .common import (
    api_errors, body, current_user_id, db, ok, require_workspace_access,
    require_workspace_record, workspace_id, workspace_membership,
)


bp = Blueprint("business", __name__)


def _membership(wid: str) -> dict:
    require_workspace_access(wid)
    return workspace_membership(wid) or {}


def _admin(wid: str) -> dict:
    membership = _membership(wid)
    if membership.get("role") not in {"owner", "editor"}:
        raise PermissionError("该操作仅限数据管理员")
    return membership


def _strings(value: Any, field: str, *, limit: int = 200) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [item.strip() for item in value.replace("\r", "").split("\n") if item.strip()]
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{field}必须是不超过 {limit} 项的数组")
    return list(dict.fromkeys(str(item).strip()[:500] for item in value if str(item).strip()))


def _space_visible(item: dict, membership: dict) -> bool:
    role = str(membership.get("role") or "viewer")
    members = {str(value) for value in item.get("member_ids") or []}
    if role not in {"owner", "editor"} and (
        item.get("status") != "published" or (members and current_user_id() not in members)
    ):
        return False
    try:
        for source_id in item.get("source_ids") or []:
            require_source_access(
                db(), str(source_id), workspace_id=str(item.get("workspace_id") or "default"),
                actor_id=current_user_id(), action="read",
            )
    except (FileNotFoundError, PermissionError):
        return False
    return True


def _space(space_id: str, wid: str, *, write: bool = False) -> dict:
    membership = _admin(wid) if write else _membership(wid)
    item = require_workspace_record("business_spaces", space_id, wid)
    if not _space_visible(item, membership):
        raise FileNotFoundError("业务数据空间不存在")
    return item


def _validate_space(payload: dict, wid: str, current: dict | None = None) -> dict:
    merged = {**(current or {}), **payload}
    name = str(merged.get("name") or "").strip()
    if not name:
        raise ValueError("业务数据空间名称不能为空")
    source_ids = _strings(merged.get("source_ids"), "数据源", limit=100)
    metric_ids = _strings(merged.get("metric_ids"), "指标", limit=500)
    for source_id in source_ids:
        require_source_access(
            db(), source_id, workspace_id=wid, actor_id=current_user_id(), action="read",
        )
    available_metrics = {item["id"]: item for item in visible_metrics(db(), wid, current_user_id())}
    if any(metric_id not in available_metrics for metric_id in metric_ids):
        raise ValueError("业务数据空间引用了不存在或无权访问的指标")
    for metric_id in metric_ids:
        if available_metrics[metric_id].get("source_id") not in source_ids:
            raise ValueError("业务数据空间中的指标必须属于已选择的数据源")
    skill_ids = _strings(merged.get("skill_ids"), "分析技能", limit=100)
    if any(not get_skill(skill_id, wid) for skill_id in skill_ids):
        raise ValueError("业务数据空间引用了不存在或未发布的分析技能")
    return {
        "name": name[:120],
        "description": str(merged.get("description") or "")[:2000],
        "business_domain": str(merged.get("business_domain") or "")[:100],
        "source_ids": source_ids,
        "metric_ids": metric_ids,
        "knowledge_tags": _strings(merged.get("knowledge_tags"), "知识标签", limit=100),
        "skill_ids": skill_ids,
        "member_ids": _strings(merged.get("member_ids"), "授权成员", limit=1000),
        "recommended_questions": _strings(merged.get("recommended_questions"), "推荐问题", limit=30),
        "owner_id": str(merged.get("owner_id") or current_user_id())[:128],
    }


def _space_readiness(item: dict) -> dict:
    checks = [
        {"id": "sources", "label": "已接入可用数据", "done": bool(item.get("source_ids"))},
        {"id": "metrics", "label": "已绑定认证指标", "done": bool(item.get("metric_ids"))},
        {"id": "questions", "label": "已配置业务问题", "done": bool(item.get("recommended_questions"))},
    ]
    complete = sum(1 for check in checks if check["done"])
    return {"score": complete / len(checks), "checks": checks, "publishable": complete == len(checks)}


def _public_space(item: dict) -> dict:
    return {**item, "readiness": _space_readiness(item)}


@bp.get("/api/business-spaces")
@api_errors
def list_business_spaces():
    wid = workspace_id()
    membership = _membership(wid)
    items = [
        _public_space(item) for item in db().list("business_spaces", workspace_id=wid, limit=5000)
        if _space_visible(item, membership)
    ]
    return ok(items=items)


@bp.post("/api/business-spaces")
@api_errors
def create_business_space():
    wid, payload = workspace_id(), body()
    _admin(wid)
    values = _validate_space(payload, wid)
    item = db().put("business_spaces", {
        "id": db().new_id("space"), "workspace_id": wid, **values,
        "status": "draft", "version": 1, "created_by": current_user_id(),
    }, workspace_id=wid)
    db().audit(
        "business_space.created", workspace_id=wid, actor=current_user_id(),
        object_type="business_space", object_id=item["id"],
    )
    return ok(item=_public_space(item)), 201


@bp.patch("/api/business-spaces/<space_id>")
@api_errors
def update_business_space(space_id: str):
    wid = workspace_id()
    current = _space(space_id, wid, write=True)
    values = _validate_space(body(), wid, current)
    values.update({"status": "draft", "version": int(current.get("version") or 0) + 1})
    item = db().patch("business_spaces", space_id, values, workspace_id=wid)
    db().audit(
        "business_space.updated", workspace_id=wid, actor=current_user_id(),
        object_type="business_space", object_id=space_id,
        detail={"version": values["version"], "publication_invalidated": current.get("status") == "published"},
    )
    return ok(item=_public_space(item or current))


@bp.post("/api/business-spaces/<space_id>/publish")
@api_errors
def publish_business_space(space_id: str):
    wid = workspace_id()
    item = _space(space_id, wid, write=True)
    readiness = _space_readiness(item)
    if not readiness["publishable"]:
        missing = "、".join(check["label"] for check in readiness["checks"] if not check["done"])
        raise ValueError(f"业务数据空间尚未就绪：{missing}")
    metrics = {metric["id"]: metric for metric in visible_metrics(db(), wid, current_user_id())}
    if any(metrics.get(metric_id, {}).get("status") != "approved" for metric_id in item.get("metric_ids") or []):
        raise ValueError("只能发布全部使用已审批指标的业务数据空间")
    item = db().patch("business_spaces", space_id, {
        "status": "published", "published_at": utcnow(), "published_by": current_user_id(),
    }, workspace_id=wid) or item
    db().audit(
        "business_space.published", workspace_id=wid, actor=current_user_id(),
        object_type="business_space", object_id=space_id, detail={"version": item.get("version")},
    )
    return ok(item=_public_space(item))


@bp.delete("/api/business-spaces/<space_id>")
@api_errors
def archive_business_space(space_id: str):
    wid = workspace_id()
    _space(space_id, wid, write=True)
    if not db().archive("business_spaces", space_id, workspace_id=wid):
        raise FileNotFoundError("业务数据空间不存在")
    return ok(archived=True)


def _owned(collection: str, record_id: str, wid: str) -> dict:
    item = require_workspace_record(collection, record_id, wid)
    if item.get("owner_id") != current_user_id():
        raise FileNotFoundError("记录不存在")
    return item


def _timezone(value: Any) -> str:
    name = str(value or "Asia/Shanghai").strip()[:60]
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("订阅时区无效") from exc
    return name


def _notification_connector(wid: str, channel: str, connector_id: Any) -> str | None:
    if channel not in {"in_app", "email", "feishu", "webhook"}:
        raise ValueError("不支持的通知渠道")
    selected = str(connector_id or "") or None
    if channel == "in_app":
        return None
    if not selected:
        raise ValueError("外部送达渠道必须选择已配置的通知连接")
    connector = require_workspace_record("connectors", selected, wid)
    compatible = {
        "email": {"email"}, "feishu": {"lark", "lark_app"},
        "webhook": {"webhook", "dingtalk", "slack", "lark"},
    }
    if connector.get("type") not in compatible[channel] or not connector.get("enabled", True):
        raise ValueError("通知连接类型与订阅渠道不匹配或已停用")
    return selected


@bp.get("/api/subscriptions")
@api_errors
def list_subscriptions():
    wid = workspace_id()
    _membership(wid)
    return ok(items=[
        item for item in db().list("analysis_subscriptions", workspace_id=wid, limit=5000)
        if item.get("owner_id") == current_user_id()
    ])


@bp.post("/api/subscriptions")
@api_errors
def create_subscription():
    wid, payload = workspace_id(), body()
    _membership(wid)
    space = _space(str(payload.get("business_space_id") or ""), wid)
    metric_id = str(payload.get("metric_id") or "")
    if metric_id and metric_id not in set(space.get("metric_ids") or []):
        raise ValueError("订阅指标不属于所选业务数据空间")
    frequency = str(payload.get("frequency") or "daily")
    if frequency not in {"daily", "weekly", "monthly"}:
        raise ValueError("订阅频率必须是 daily、weekly 或 monthly")
    channel = str(payload.get("channel") or "in_app")
    connector_id = _notification_connector(wid, channel, payload.get("connector_id"))
    delivery_time = str(payload.get("delivery_time") or "09:00")[:5]
    cron = subscription_cron(frequency, delivery_time)
    item = db().put("analysis_subscriptions", {
        "id": db().new_id("sub"), "workspace_id": wid, "owner_id": current_user_id(),
        "name": str(payload.get("name") or "我的数据订阅")[:120],
        "business_space_id": space["id"], "metric_id": metric_id or None,
        "question": str(payload.get("question") or "")[:2000],
        "condition": str(payload.get("condition") or "")[:1000],
        "frequency": frequency, "delivery_time": delivery_time,
        "timezone": _timezone(payload.get("timezone")), "cron": cron,
        "channel": channel, "connector_id": connector_id, "enabled": bool(payload.get("enabled", True)),
        "last_run_at": None, "next_run_at": None,
    }, workspace_id=wid)
    return ok(item=item), 201


@bp.patch("/api/subscriptions/<subscription_id>")
@api_errors
def update_subscription(subscription_id: str):
    wid = workspace_id()
    current = _owned("analysis_subscriptions", subscription_id, wid)
    allowed = {key: value for key, value in body().items() if key in {
        "name", "question", "condition", "frequency", "delivery_time", "timezone", "channel", "connector_id", "enabled",
    }}
    if "frequency" in allowed and allowed["frequency"] not in {"daily", "weekly", "monthly"}:
        raise ValueError("订阅频率无效")
    if "channel" in allowed and allowed["channel"] not in {"in_app", "email", "feishu", "webhook"}:
        raise ValueError("通知渠道无效")
    channel = str(allowed.get("channel") or current.get("channel") or "in_app")
    connector_id = allowed.get("connector_id", current.get("connector_id"))
    allowed["connector_id"] = _notification_connector(wid, channel, connector_id)
    if "timezone" in allowed:
        allowed["timezone"] = _timezone(allowed["timezone"])
    if {"frequency", "delivery_time"} & allowed.keys():
        allowed["cron"] = subscription_cron(
            str(allowed.get("frequency") or current.get("frequency") or "daily"),
            str(allowed.get("delivery_time") or current.get("delivery_time") or "09:00"),
        )
    item = db().patch("analysis_subscriptions", subscription_id, allowed, workspace_id=wid)
    return ok(item=item or current)


@bp.delete("/api/subscriptions/<subscription_id>")
@api_errors
def archive_subscription(subscription_id: str):
    wid = workspace_id()
    _owned("analysis_subscriptions", subscription_id, wid)
    db().archive("analysis_subscriptions", subscription_id, workspace_id=wid)
    return ok(archived=True)


@bp.post("/api/subscriptions/<subscription_id>/run")
@api_errors
def run_subscription_now(subscription_id: str):
    wid = workspace_id()
    current = _owned("analysis_subscriptions", subscription_id, wid)
    marker = f"manual:{utcnow()}"
    started = start_analysis_subscription(
        db(), current_app._get_current_object(), current, minute_key=marker,
    )
    item = db().patch("analysis_subscriptions", subscription_id, {
        "last_run_at": utcnow(), "last_run_id": started["run"]["id"], "last_error": None,
    }, workspace_id=wid)
    return ok(item=item or current, run=started["run"], job=started["job"]), 202


def _insight_visible(item: dict) -> bool:
    audience = {str(value) for value in item.get("audience_ids") or []}
    if audience and current_user_id() not in audience and item.get("owner_id") != current_user_id():
        return False
    space_id = str(item.get("business_space_id") or "")
    if not space_id:
        return True
    wid = str(item.get("workspace_id") or workspace_id())
    space = db().get("business_spaces", space_id, workspace_id=wid)
    membership = workspace_membership(wid)
    return bool(space and membership and _space_visible(space, membership))


@bp.get("/api/insights")
@api_errors
def list_insights():
    wid = workspace_id()
    _membership(wid)
    receipts = {
        item.get("insight_id"): item for item in db().list("insight_receipts", workspace_id=wid, limit=5000)
        if item.get("owner_id") == current_user_id()
    }
    items = []
    for item in db().list("business_insights", workspace_id=wid, limit=5000):
        if not _insight_visible(item):
            continue
        receipt = receipts.get(item["id"], {})
        if receipt.get("dismissed"):
            continue
        items.append({**item, "acknowledged": bool(receipt.get("acknowledged"))})
    return ok(items=items)


@bp.post("/api/insights")
@api_errors
def create_insight():
    wid, payload = workspace_id(), body()
    _admin(wid)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("洞察标题不能为空")
    space = _space(str(payload.get("business_space_id") or ""), wid, write=True)
    severity = str(payload.get("severity") or "info")
    if severity not in {"info", "opportunity", "warning", "critical"}:
        raise ValueError("洞察级别无效")
    item = db().put("business_insights", {
        "id": db().new_id("insight"), "workspace_id": wid, "owner_id": current_user_id(),
        "business_space_id": space["id"], "title": title[:160],
        "summary": str(payload.get("summary") or "")[:4000], "severity": severity,
        "metric_id": str(payload.get("metric_id") or "") or None,
        "run_id": str(payload.get("run_id") or "") or None,
        "audience_ids": _strings(payload.get("audience_ids"), "洞察接收人", limit=1000),
        "status": "active", "detected_at": utcnow(),
    }, workspace_id=wid)
    return ok(item=item), 201


@bp.post("/api/insights/<insight_id>/receipt")
@api_errors
def update_insight_receipt(insight_id: str):
    wid, payload = workspace_id(), body()
    _membership(wid)
    insight = require_workspace_record("business_insights", insight_id, wid)
    if not _insight_visible(insight):
        raise FileNotFoundError("洞察不存在")
    item = db().put("insight_receipts", {
        "id": f"{insight_id}:{current_user_id()}", "workspace_id": wid,
        "insight_id": insight_id, "owner_id": current_user_id(),
        "acknowledged": bool(payload.get("acknowledged")),
        "dismissed": bool(payload.get("dismissed")),
    }, workspace_id=wid)
    return ok(item=item)


def _run_for_report(run_id: str, wid: str) -> tuple[dict, dict]:
    run = RunStore(db()).get_run(run_id, workspace_id=wid)
    if not run or run.get("actor_id") != current_user_id():
        raise FileNotFoundError("分析结果不存在")
    require_sources_access(
        db(), run.get("source_scope") or [], workspace_id=wid,
        actor_id=current_user_id(), action="read",
    )
    publication = ResultService(db()).publication(run_id, workspace_id=wid)
    if not publication:
        raise ValueError("只有通过验证并发布的分析结果可以加入报告")
    return run, publication


@bp.get("/api/reports")
@api_errors
def list_reports():
    wid = workspace_id()
    membership = _membership(wid)
    items = [
        item for item in db().list("business_reports", workspace_id=wid, limit=5000)
        if _report_visible(item, wid, membership)
    ]
    return ok(items=items)


def _report_visible(item: dict, wid: str, membership: dict) -> bool:
    if item.get("owner_id") != current_user_id() and item.get("visibility") != "workspace":
        return False
    try:
        for run_id in item.get("run_ids") or []:
            run = RunStore(db()).get_run(str(run_id), workspace_id=wid)
            if not run:
                return False
            require_sources_access(
                db(), run.get("source_scope") or [], workspace_id=wid,
                actor_id=current_user_id(), action="read",
            )
            session = db().get("sessions", run["session_id"], workspace_id=wid) or {}
            space_id = str(session.get("business_space_id") or "")
            if space_id:
                space = db().get("business_spaces", space_id, workspace_id=wid)
                if not space or not _space_visible(space, membership):
                    return False
    except (FileNotFoundError, PermissionError):
        return False
    return True


@bp.post("/api/reports")
@api_errors
def create_report():
    wid, payload = workspace_id(), body()
    _membership(wid)
    run_ids = _strings(payload.get("run_ids") or ([payload.get("run_id")] if payload.get("run_id") else []), "分析结果", limit=50)
    if not run_ids:
        raise ValueError("报告至少需要一个已发布分析结果")
    for run_id in run_ids:
        _run_for_report(run_id, wid)
    title = str(payload.get("title") or "经营分析报告").strip()
    item = db().put("business_reports", {
        "id": db().new_id("report"), "workspace_id": wid, "owner_id": current_user_id(),
        "title": title[:160], "description": str(payload.get("description") or "")[:2000],
        "run_ids": run_ids, "sections": _strings(payload.get("sections") or ["核心结论", "指标表现", "原因分析", "行动建议"], "报告章节", limit=30),
        "status": "draft", "visibility": "private", "version": 1,
    }, workspace_id=wid)
    return ok(item=item), 201


@bp.patch("/api/reports/<report_id>")
@api_errors
def update_report(report_id: str):
    wid = workspace_id()
    current = _owned("business_reports", report_id, wid)
    payload = body()
    allowed = {key: payload[key] for key in {"title", "description", "sections"} if key in payload}
    if "sections" in allowed:
        allowed["sections"] = _strings(allowed["sections"], "报告章节", limit=30)
    allowed.update({"status": "draft", "version": int(current.get("version") or 0) + 1})
    return ok(item=db().patch("business_reports", report_id, allowed, workspace_id=wid) or current)


@bp.post("/api/reports/<report_id>/publish")
@api_errors
def publish_report(report_id: str):
    wid = workspace_id()
    current = _owned("business_reports", report_id, wid)
    for run_id in current.get("run_ids") or []:
        _run_for_report(run_id, wid)
    visibility = str(body().get("visibility") or "private")
    if visibility not in {"private", "workspace"}:
        raise ValueError("报告可见范围无效")
    if visibility == "workspace":
        _admin(wid)
    item = db().patch("business_reports", report_id, {
        "status": "published", "visibility": visibility,
        "published_at": utcnow(), "published_by": current_user_id(),
    }, workspace_id=wid) or current
    return ok(item=item)


@bp.delete("/api/reports/<report_id>")
@api_errors
def archive_report(report_id: str):
    wid = workspace_id()
    _owned("business_reports", report_id, wid)
    db().archive("business_reports", report_id, workspace_id=wid)
    return ok(archived=True)


@bp.post("/api/feedback")
@api_errors
def create_feedback():
    wid, payload = workspace_id(), body()
    _membership(wid)
    run_id = str(payload.get("run_id") or "")
    run = RunStore(db()).get_run(run_id, workspace_id=wid)
    if not run or run.get("actor_id") != current_user_id():
        raise FileNotFoundError("分析结果不存在")
    rating = str(payload.get("rating") or "")
    if rating not in {"correct", "incorrect", "partially_correct"}:
        raise ValueError("反馈类型无效")
    item = db().put("analysis_feedback", {
        "id": f"{run_id}:{current_user_id()}", "workspace_id": wid,
        "run_id": run_id, "owner_id": current_user_id(), "rating": rating,
        "category": str(payload.get("category") or "")[:100],
        "comment": str(payload.get("comment") or "")[:2000], "status": "open",
    }, workspace_id=wid)
    return ok(item=item), 201


@bp.get("/api/business/operations")
@api_errors
def business_operations():
    wid = workspace_id()
    _admin(wid)
    feedback = db().list("analysis_feedback", workspace_id=wid, limit=5000)
    runs = RunStore(db()).list_runs(wid, limit=500)
    return ok(
        feedback=feedback,
        metrics={
            "runs": len(runs), "published": sum(1 for item in runs if item.get("outcome") == "published"),
            "failed": sum(1 for item in runs if item.get("execution_status") == "failed"),
            "feedback": len(feedback), "incorrect": sum(1 for item in feedback if item.get("rating") == "incorrect"),
        },
    )


@bp.get("/api/business/home")
@api_errors
def business_home():
    wid = workspace_id()
    membership = _membership(wid)
    spaces = [
        _public_space(item) for item in db().list("business_spaces", workspace_id=wid, limit=5000)
        if _space_visible(item, membership)
    ]
    active_space_id = str(request.args.get("business_space_id") or "")
    active = next((item for item in spaces if item["id"] == active_space_id), None)
    active = active or next((item for item in spaces if item.get("status") == "published"), spaces[0] if spaces else None)
    metrics = visible_metrics(db(), wid, current_user_id())
    if active:
        allowed = set(active.get("metric_ids") or [])
        metrics = [item for item in metrics if item["id"] in allowed]
    runs = [
        item for item in RunStore(db()).list_runs(wid, limit=100)
        if item.get("actor_id") == current_user_id()
    ]
    result_service = ResultService(db())
    analyses = []
    for run in runs[:20]:
        publication = result_service.publication(run["id"], workspace_id=wid)
        contract = RunStore(db()).latest_contract(run["id"])
        analyses.append({
            "id": run["id"], "session_id": run["session_id"],
            "objective": ((contract or {}).get("payload") or {}).get("objective", ""),
            "status": run["execution_status"], "quality_status": run["quality_status"],
            "published": bool(publication), "created_at": run["created_at"], "updated_at": run["updated_at"],
        })
    insights = [item for item in db().list("business_insights", workspace_id=wid, limit=100) if _insight_visible(item)]
    subscriptions = [
        item for item in db().list("analysis_subscriptions", workspace_id=wid, limit=5000)
        if item.get("owner_id") == current_user_id()
    ]
    reports = [
        item for item in db().list("business_reports", workspace_id=wid, limit=5000)
        if _report_visible(item, wid, membership)
    ]
    authorized_sources = filter_authorized_sources(
        db(), db().list("sources", workspace_id=wid, limit=5000),
        workspace_id=wid, actor_id=current_user_id(), action="read",
    )
    readiness = {
        "data": bool(authorized_sources),
        "metrics": any(item.get("status") == "approved" for item in metrics),
        "space": bool(active and active.get("status") == "published"),
    }
    return ok(
        role=membership.get("role", "viewer"), spaces=spaces, active_space=active,
        metrics=[item for item in metrics if item.get("status") == "approved"],
        recent_analyses=analyses, insights=insights[:10], subscriptions=subscriptions,
        reports=reports[:20], readiness=readiness,
    )
