from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint

from ..services.datasets import public_source
from ..services.saas import (
    DEFAULT_PLANS,
    DEFAULT_SUBSCRIPTION_ID,
    DEFAULT_TENANT_ID,
    SOLUTION_CATALOG,
    onboarding_status,
    plans,
    product_status,
    seed_demo_workspace,
    workspace_entitlements,
)
from .common import (
    api_errors,
    body,
    current_user_id,
    db,
    ok,
    require_system_owner,
    require_workspace_access,
    workspace_id,
)


bp = Blueprint("product", __name__)


@bp.get("/api/product")
@api_errors
def product():
    wid = workspace_id()
    require_workspace_access(wid)
    return ok(product=product_status(db(), wid, current_user_id()))


@bp.get("/api/product/plans")
@api_errors
def product_plans():
    return ok(items=plans(db()), defaults=DEFAULT_PLANS)


@bp.get("/api/product/solutions")
@api_errors
def product_solutions():
    return ok(items=SOLUTION_CATALOG)


@bp.get("/api/product/entitlements")
@api_errors
def product_entitlements():
    wid = workspace_id()
    require_workspace_access(wid)
    return ok(entitlements=workspace_entitlements(db(), wid))


@bp.patch("/api/product/subscription")
@api_errors
def update_subscription():
    require_system_owner()
    payload = body()
    wid = workspace_id()
    entitlements = workspace_entitlements(db(), wid)
    tenant_id = str(entitlements.get("tenant_id") or DEFAULT_TENANT_ID)
    tenant_name = str(payload.get("tenant_name") or "").strip()
    if tenant_name:
        db().patch("tenants", tenant_id, {"name": tenant_name[:120]})
    plan_id = str(payload.get("plan_id") or entitlements.get("plan", {}).get("id") or "enterprise")
    if plan_id not in {plan["id"] for plan in DEFAULT_PLANS}:
        raise ValueError("套餐不存在")
    status = str(payload.get("status") or entitlements.get("subscription", {}).get("status") or "trialing")
    if status not in {"trialing", "active", "inactive", "canceled"}:
        raise ValueError("订阅状态只能是 trialing、active、inactive 或 canceled")
    subscription_id = str(entitlements.get("subscription", {}).get("id") or DEFAULT_SUBSCRIPTION_ID)
    subscription = db().get("subscriptions", subscription_id) or {
        "id": subscription_id,
        "tenant_id": tenant_id,
        "current_period_start": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if "period_days" in payload:
        days = max(1, min(3660, int(payload.get("period_days") or 30)))
        subscription["current_period_end"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    elif payload.get("current_period_end"):
        subscription["current_period_end"] = str(payload["current_period_end"])[:80]
    subscription.update(
        {
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "status": status,
            "trial": bool(payload.get("trial", status == "trialing")),
            "entitlement_source": "system-owner",
            "updated_by": current_user_id(),
        },
    )
    db().put("subscriptions", subscription)
    return ok(product=product_status(db(), wid, current_user_id()))


@bp.get("/api/onboarding")
@api_errors
def onboarding():
    wid = workspace_id()
    require_workspace_access(wid)
    return ok(onboarding=onboarding_status(db(), wid))


@bp.post("/api/onboarding/demo")
@api_errors
def seed_demo():
    wid = workspace_id()
    require_workspace_access(wid, write=True)
    result = seed_demo_workspace(db(), wid, current_user_id())
    return ok(
        created=result["created"],
        source=public_source(result["source"]),
        onboarding=result["onboarding"],
        entitlements=result["entitlements"],
    ), 201
