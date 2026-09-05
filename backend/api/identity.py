from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timezone

from flask import Blueprint, current_app, session

from .common import api_errors, body, db, ok
from ..services.usage import quota_status


bp = Blueprint("identity", __name__)
PASSWORD_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 12


def _password_hash(
    password: str, salt: bytes | None = None, iterations: int = PASSWORD_ITERATIONS,
) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return salt.hex(), digest.hex()


def _public(user: dict) -> dict:
    return {
        key: value for key, value in user.items()
        if key not in {"password_hash", "password_salt", "password_iterations"}
    }


def _start_session(user: dict, active_workspace_id: str) -> str:
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["session_version"] = int(user.get("session_version") or 0)
    session["active_workspace_id"] = active_workspace_id
    session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _registration_open() -> bool:
    return (
        not db().list("users", include_archived=True, limit=1)
        or current_app.config.get("TESTING")
        or os.getenv("MERIDIAN_ALLOW_SELF_REGISTRATION", "0") == "1"
    )


def _invitation(token: str, email: str) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    invite = db().get("invitations", f"invite_{token_hash}")
    if not invite or invite.get("status") != "pending" or invite.get("email") != email:
        return None
    if datetime.fromisoformat(invite["expires_at"]) <= datetime.now(timezone.utc):
        return None
    return invite


def _verify_email_code(email: str, code: str, *, consume: bool = True) -> None:
    from ..services.security import SecretVault

    verification = next(
        (item for item in db().list("email_codes", limit=5000) if item.get("email") == email),
        None,
    )
    expected = (
        SecretVault(current_app.config["VAULT_KEY"]).open(verification.get("code", ""), {}).get("value")
        if verification else None
    )
    expired = not verification or datetime.fromisoformat(verification["expires_at"]) < datetime.now(timezone.utc)
    attempts = int((verification or {}).get("attempts") or 0)
    valid = bool(code and not expired and attempts < 5 and hmac.compare_digest(code, str(expected or "")))
    if not valid:
        if verification:
            attempts += 1
            if expired or attempts >= 5:
                db().delete("email_codes", verification["id"])
            else:
                db().patch("email_codes", verification["id"], {"attempts": attempts})
        raise ValueError("验证码无效或已过期")
    if consume:
        db().delete("email_codes", verification["id"])


def _login_attempt_id(email: str) -> str:
    from flask import request

    address = request.remote_addr or "unknown"
    return "login:" + hashlib.sha256(f"{address}|{email}".encode()).hexdigest()


def _check_login_rate(email: str) -> tuple[str, dict]:
    attempt_id = _login_attempt_id(email)
    attempt = db().get("auth_attempts", attempt_id) or {"id": attempt_id, "failures": 0, "window_started": time.time()}
    if time.time() - float(attempt.get("window_started") or 0) > 900:
        attempt = {"id": attempt_id, "failures": 0, "window_started": time.time()}
    if int(attempt.get("failures") or 0) >= 5:
        raise PermissionError("登录尝试过多，请 15 分钟后重试")
    return attempt_id, attempt


@bp.post("/api/auth/register")
@api_errors
def register():
    payload = body()
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    invitation = _invitation(str(payload.get("invitation_token") or ""), email)
    invited_workspace = None
    if invitation:
        invited_workspace = db().get("workspaces", invitation["workspace_id"])
        if not invited_workspace:
            raise FileNotFoundError("邀请的工作空间不存在")
    if not _registration_open() and not invitation:
        raise PermissionError("当前实例未开放自助注册，请联系系统所有者添加成员")
    require_code = bool(
        os.getenv("RAILWAY_PROJECT_ID") or os.getenv("VERCEL") == "1"
        or os.getenv("MERIDIAN_REQUIRE_EMAIL_CODE") == "1"
    )
    if require_code:
        code = str(payload.get("code") or "").strip()
        _verify_email_code(email, code)
    if "@" not in email or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"请输入有效邮箱，密码至少 {MIN_PASSWORD_LENGTH} 位")
    if any(user.get("email") == email for user in db().list("users", include_archived=True)):
        raise ValueError("该邮箱已经注册")
    salt, digest = _password_hash(password)
    user = db().create_user(
        {
            "id": db().new_id("usr"), "email": email,
            "name": str(payload.get("name") or email.split("@")[0])[:80],
            "password_salt": salt, "password_hash": digest,
            "password_iterations": PASSWORD_ITERATIONS,
            "enabled": True, "session_version": 0,
        },
        allow_additional=bool(
            invitation or current_app.config.get("TESTING")
            or os.getenv("MERIDIAN_ALLOW_SELF_REGISTRATION", "0") == "1"
        ),
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
    elif invitation:
        db().put(
            "workspace_members",
            {
                "id": f"{invitation['workspace_id']}:{user['id']}",
                "workspace_id": invitation["workspace_id"], "user_id": user["id"],
                "role": invitation.get("role", "viewer"), "enabled": True,
            },
            workspace_id=invitation["workspace_id"],
        )
        active_workspace_id = invitation["workspace_id"]
        db().patch("invitations", invitation["id"], {"status": "accepted", "accepted_by": user["id"]})
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
    csrf_token = _start_session(user, active_workspace_id)
    return ok(user=_public(user), active_workspace_id=active_workspace_id, csrf_token=csrf_token), 201


@bp.post("/api/auth/login")
@api_errors
def login():
    payload = body()
    email = str(payload.get("email") or "").strip().lower()
    attempt_id, attempt = _check_login_rate(email)
    user = next((item for item in db().list("users") if item.get("email") == email), None)
    if not user or not user.get("enabled", True):
        attempt["failures"] = int(attempt.get("failures") or 0) + 1
        db().put("auth_attempts", attempt, workspace_id="default")
        raise ValueError("邮箱或密码错误")
    iterations = int(user.get("password_iterations") or 310_000)
    _, digest = _password_hash(
        str(payload.get("password") or ""), bytes.fromhex(user["password_salt"]), iterations,
    )
    if not hmac.compare_digest(digest, user["password_hash"]):
        attempt["failures"] = int(attempt.get("failures") or 0) + 1
        db().put("auth_attempts", attempt, workspace_id="default")
        raise ValueError("邮箱或密码错误")
    if iterations < PASSWORD_ITERATIONS:
        salt, digest = _password_hash(str(payload.get("password") or ""))
        user = db().patch("users", user["id"], {
            "password_salt": salt, "password_hash": digest,
            "password_iterations": PASSWORD_ITERATIONS,
        }) or user
    db().delete("auth_attempts", attempt_id)
    memberships = [
        item for item in db().list("workspace_members")
        if item.get("user_id") == user["id"] and item.get("enabled", True)
    ]
    active_workspace_id = memberships[0]["workspace_id"] if memberships else "default"
    csrf_token = _start_session(user, active_workspace_id)
    return ok(user=_public(user), active_workspace_id=active_workspace_id, csrf_token=csrf_token)


@bp.post("/api/auth/logout")
def logout():
    session.clear()
    return ok(logged_out=True)


@bp.post("/api/auth/reset-password")
@api_errors
def reset_password():
    payload = body()
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    user = next((item for item in db().list("users") if item.get("email") == email), None)
    if not user or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("邮箱、验证码或新密码无效")
    _verify_email_code(email, str(payload.get("code") or "").strip())
    salt, digest = _password_hash(password)
    db().patch(
        "users", user["id"],
        {
            "password_salt": salt,
            "password_hash": digest,
            "password_iterations": PASSWORD_ITERATIONS,
            "session_version": int(user.get("session_version") or 0) + 1,
        },
    )
    db().audit("auth.password_reset", actor=user["id"], object_type="user", object_id=user["id"])
    if session.get("user_id") == user["id"]:
        session.clear()
    return ok(reset=True)


@bp.get("/api/auth/me")
def me():
    user_id = session.get("user_id")
    user = db().get("users", user_id) if user_id else None
    if user and int(session.get("session_version") or 0) != int(user.get("session_version") or 0):
        session.clear()
        user = None
    if not user:
        local_mode = not db().list("users", include_archived=True, limit=1) and current_app.config["SETTINGS"].environment != "production"
        return ok(
            user=None, local_mode=local_mode, authenticated=False,
            registration_open=_registration_open(), csrf_token="",
        )
    wid = str(session.get("active_workspace_id") or "default")
    return ok(
        user=_public(user), local_mode=False, authenticated=True,
        registration_open=_registration_open(), csrf_token=str(session.get("csrf_token") or ""),
        quota=quota_status(db(), wid),
    )
