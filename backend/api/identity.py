from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone

from flask import Blueprint, current_app, session

from .common import api_errors, body, db, ok


bp = Blueprint("identity", __name__)


def _password_hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return salt.hex(), digest.hex()


def _public(user: dict) -> dict:
    return {key: value for key, value in user.items() if key not in {"password_hash", "password_salt"}}


@bp.post("/api/auth/register")
@api_errors
def register():
    payload = body()
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    require_code = bool(
        os.getenv("RAILWAY_PROJECT_ID") or os.getenv("VERCEL") == "1"
        or os.getenv("MERIDIAN_REQUIRE_EMAIL_CODE") == "1"
    )
    if require_code:
        from ..services.security import SecretVault

        code = str(payload.get("code") or "").strip()
        verification = next(
            (item for item in db().list("email_codes", limit=5000) if item.get("email") == email),
            None,
        )
        expected = (
            SecretVault(current_app.config["SECRET_KEY"]).open(verification.get("code", ""), {}).get("value")
            if verification else None
        )
        expired = not verification or datetime.fromisoformat(verification["expires_at"]) < datetime.now(timezone.utc)
        if not code or expired or not hmac.compare_digest(code, str(expected or "")):
            raise ValueError("验证码无效或已过期")
        db().delete("email_codes", verification["id"])
    if "@" not in email or len(password) < 8:
        raise ValueError("请输入有效邮箱，密码至少 8 位")
    if any(user.get("email") == email for user in db().list("users", include_archived=True)):
        raise ValueError("该邮箱已经注册")
    salt, digest = _password_hash(password)
    user = db().put(
        "users",
        {
            "id": db().new_id("usr"), "email": email,
            "name": str(payload.get("name") or email.split("@")[0])[:80],
            "password_salt": salt, "password_hash": digest,
            "role": "owner" if not db().list("users", include_archived=True) else "member",
            "enabled": True,
        },
    )
    role = user["role"]
    active_workspace_id = "default"
    if role == "owner":
        for workspace in db().list("workspaces"):
            db().put(
                "workspace_members",
                {
                    "id": f"{workspace['id']}:{user['id']}", "workspace_id": workspace["id"],
                    "user_id": user["id"], "role": "owner", "enabled": True,
                },
                workspace_id=workspace["id"],
            )
    else:
        personal = db().put(
            "workspaces",
            {
                "id": db().new_id("ws"), "name": f"{user['name']} 的工作空间",
                "description": "个人分析空间", "permission": "write", "owner_id": user["id"],
            },
        )
        db().put(
            "workspace_members",
            {
                "id": f"{personal['id']}:{user['id']}", "workspace_id": personal["id"],
                "user_id": user["id"], "role": "owner", "enabled": True,
            },
            workspace_id=personal["id"],
        )
        active_workspace_id = personal["id"]
    session.clear()
    session["user_id"] = user["id"]
    session["active_workspace_id"] = active_workspace_id
    return ok(user=_public(user), active_workspace_id=active_workspace_id), 201


@bp.post("/api/auth/login")
@api_errors
def login():
    payload = body()
    email = str(payload.get("email") or "").strip().lower()
    user = next((item for item in db().list("users") if item.get("email") == email), None)
    if not user or not user.get("enabled", True):
        raise ValueError("邮箱或密码错误")
    _, digest = _password_hash(str(payload.get("password") or ""), bytes.fromhex(user["password_salt"]))
    if not hmac.compare_digest(digest, user["password_hash"]):
        raise ValueError("邮箱或密码错误")
    session.clear()
    session["user_id"] = user["id"]
    memberships = [
        item for item in db().list("workspace_members")
        if item.get("user_id") == user["id"] and item.get("enabled", True)
    ]
    active_workspace_id = memberships[0]["workspace_id"] if memberships else "default"
    session["active_workspace_id"] = active_workspace_id
    return ok(user=_public(user), active_workspace_id=active_workspace_id)


@bp.post("/api/auth/logout")
def logout():
    session.clear()
    return ok(logged_out=True)


@bp.get("/api/auth/me")
def me():
    user_id = session.get("user_id")
    user = db().get("users", user_id) if user_id else None
    if not user:
        return ok(user=None, local_mode=True, authenticated=False)
    wid = str(session.get("active_workspace_id") or "default")
    usage = sum(int(item.get("total_tokens") or 0) for item in db().list("usage_events", workspace_id=wid, limit=5000))
    daily_limit = int(os.getenv("MERIDIAN_DAILY_TOKEN_LIMIT", "1000000"))
    return ok(
        user=_public(user), local_mode=False, authenticated=True,
        quota={
            "used": usage, "limit": daily_limit, "remaining": max(0, daily_limit - usage),
            "allowed": usage < daily_limit,
        },
    )
