"""Application factory for Meridian Analytics Workbench."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from .core.database import Database
from .core.settings import Settings


def create_app(test_config: dict | None = None) -> Flask:
    root = Path(__file__).resolve().parent.parent
    settings = Settings.from_environment(root)
    app = Flask(__name__, static_folder=None)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        MAX_CONTENT_LENGTH=settings.max_upload_bytes,
        JSON_SORT_KEYS=False,
        SETTINGS=settings,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("MERIDIAN_COOKIE_SECURE", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)
        settings = Settings.for_tests(root, test_config)
        app.config["SETTINGS"] = settings

    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    app.extensions["meridian_db"] = database

    CORS(app, resources={r"/api/*": {"origins": settings.allowed_origins}})

    from .api import register_blueprints

    register_blueprints(app)
    if not app.config.get("TESTING"):
        from .services.feishu_bot import start_long_connection

        for connector in database.list("connectors", limit=5000):
            if connector.get("type") == "lark_app" and connector.get("purpose") == "feishu_bot":
                start_long_connection(app, connector)
    if not app.config.get("TESTING") and os.getenv("MERIDIAN_DISABLE_SCHEDULER", "0") != "1":
        from .services.scheduler import start_scheduler

        start_scheduler(app)

    @app.before_request
    def reject_cross_origin_writes():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        origin = (request.headers.get("Origin") or "").strip()
        if not origin:
            return None
        try:
            parsed = urlsplit(origin)
            same_origin = parsed.netloc.lower() == request.host.lower()
            allowed_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        except ValueError:
            same_origin = allowed_local = False
        if not same_origin and not allowed_local:
            return jsonify({"ok": False, "error": "跨站写入已拒绝"}), 403
        return None

    @app.before_request
    def require_authenticated_workspace():
        if not request.path.startswith("/api/") or request.path in {"/api/health"}:
            return None
        if request.path.startswith("/api/auth/") or request.path in {"/api/integrations/events", "/api/feishu-bot/events"}:
            return None
        users = database.list("users", include_archived=True, limit=1)
        if not users:
            return None
        # Flask's signed session is authoritative; the unusual local mode is disabled once a user exists.
        from flask import session

        user_id = session.get("user_id")
        user = database.get("users", str(user_id)) if user_id else None
        if not user or not user.get("enabled", True):
            return jsonify({"ok": False, "error": "请先登录"}), 401
        if request.path == "/api/workspaces" and request.method in {"GET", "POST"}:
            return None
        payload = request.get_json(silent=True) if request.is_json else {}
        wid = str(
            request.args.get("workspace_id") or request.headers.get("X-Workspace-Id")
            or (payload.get("workspace_id") if isinstance(payload, dict) else None)
            or request.form.get("workspace_id") or session.get("active_workspace_id") or "default"
        )[:128]
        membership = next(
            (
                item for item in database.list("workspace_members", workspace_id=wid)
                if item.get("user_id") == user["id"] and item.get("enabled", True)
            ),
            None,
        )
        if not membership:
            return jsonify({"ok": False, "error": "无权访问该工作空间"}), 403
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and membership.get("role") == "viewer":
            return jsonify({"ok": False, "error": "当前成员只有只读权限"}), 403
        return None

    @app.get("/")
    def index():
        return send_from_directory(settings.frontend_dir, "index.html")

    @app.get("/assets/<path:filename>")
    def frontend_asset(filename: str):
        return send_from_directory(settings.frontend_dir / "assets", filename)

    @app.get("/src/<path:filename>")
    def frontend_source(filename: str):
        return send_from_directory(settings.frontend_dir / "src", filename)

    @app.get("/vendor/<path:filename>")
    def frontend_vendor(filename: str):
        return send_from_directory(settings.frontend_dir / "vendor", filename)

    @app.get("/static/drawio/<path:filename>")
    def drawio_asset(filename: str):
        return send_from_directory(settings.frontend_dir / "drawio", filename)

    @app.get("/api/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "meridian-analytics-workbench",
            "version": "1.0.0",
            "database": database.ping(),
        })

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify({"ok": False, "error": "上传文件超过大小限制"}), 413

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "接口不存在"}), 404
        return error

    @app.errorhandler(Exception)
    def unexpected_error(error):
        if isinstance(error, HTTPException):
            return error
        if app.config.get("TESTING"):
            raise error
        logging.getLogger(__name__).exception("Unhandled request error")
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "服务暂时无法完成该操作，请查看服务日志"}), 500
        return "Internal Server Error", 500

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.path.startswith("/static/drawio/"):
            # The vendored draw.io runtime requires inline/evaluated plugin code. Keep that
            # exception isolated to its same-origin iframe instead of weakening the workbench CSP.
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
                "font-src 'self' data:; worker-src 'self' blob:; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'self'",
            )
        else:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
                "frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
            )
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


__all__ = ["create_app"]
