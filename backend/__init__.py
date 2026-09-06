"""Application factory for Meridian Analytics Workbench."""

from __future__ import annotations

import atexit
import logging
import os
import hmac
import re
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, request, send_from_directory, session
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
        VAULT_KEY=settings.encryption_key,
        MAX_CONTENT_LENGTH=settings.max_upload_bytes,
        JSON_SORT_KEYS=False,
        SETTINGS=settings,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv(
            "MERIDIAN_COOKIE_SECURE", "1" if settings.environment == "production" else "0",
        ) == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(
            hours=max(1, int(os.getenv("MERIDIAN_SESSION_HOURS", "12"))),
        ),
        TRUSTED_HOSTS=settings.trusted_hosts or None,
    )
    if test_config:
        app.config.update(test_config)
        settings = Settings.for_tests(root, test_config)
        app.config["SETTINGS"] = settings

    if not app.config.get("TESTING") and settings.environment == "production":
        configured_secret = os.getenv("MERIDIAN_SECRET_KEY", "").strip()
        if len(configured_secret) < 32 or configured_secret in {"change-this-in-production", "replace-before-production"}:
            raise RuntimeError("生产环境必须配置至少 32 字符的随机 MERIDIAN_SECRET_KEY")
        configured_encryption = os.getenv("MERIDIAN_ENCRYPTION_KEY", "").strip()
        if len(configured_encryption) < 32:
            raise RuntimeError("生产环境必须配置至少 32 字符且独立持久化的 MERIDIAN_ENCRYPTION_KEY")
        if hmac.compare_digest(configured_secret, configured_encryption):
            raise RuntimeError("MERIDIAN_SECRET_KEY 与 MERIDIAN_ENCRYPTION_KEY 必须使用两个独立密钥")
        configured_backup = os.getenv("MERIDIAN_BACKUP_KEY", "").strip()
        if len(configured_backup) < 32:
            raise RuntimeError("生产环境必须配置至少 32 字符且独立保管的 MERIDIAN_BACKUP_KEY")
        if any(
            hmac.compare_digest(configured_backup, value)
            for value in (configured_secret, configured_encryption)
        ):
            raise RuntimeError("MERIDIAN_BACKUP_KEY 必须独立于应用会话和凭据加密密钥")
        bootstrap_token = os.getenv("MERIDIAN_BOOTSTRAP_TOKEN", "").strip()
        if len(bootstrap_token) < 32:
            raise RuntimeError("生产环境必须配置至少 32 字符的 MERIDIAN_BOOTSTRAP_TOKEN")
        metrics_token = os.getenv("MERIDIAN_METRICS_TOKEN", "").strip()
        if len(metrics_token) < 32:
            raise RuntimeError("生产环境必须配置至少 32 字符的 MERIDIAN_METRICS_TOKEN")
        if not app.config["SESSION_COOKIE_SECURE"]:
            raise RuntimeError("生产环境必须启用 MERIDIAN_COOKIE_SECURE=1")
        if not settings.trusted_hosts:
            raise RuntimeError("生产环境必须配置 MERIDIAN_TRUSTED_HOSTS")
        if not os.getenv("MERIDIAN_OUTBOUND_HOST_ALLOWLIST", "").strip():
            raise RuntimeError("生产环境必须配置 MERIDIAN_OUTBOUND_HOST_ALLOWLIST")
        required_frontend_files = (
            settings.frontend_dir / "index.html",
            settings.frontend_dir / "src" / "renders.js",
            settings.frontend_dir / "vendor" / "vue.global.prod.js",
            settings.frontend_dir / "vendor" / "echarts-china.min.js",
        )
        if not all(path.is_file() for path in required_frontend_files):
            raise RuntimeError(
                "生产前端产物不完整：请先执行 npm run build，并将 "
                "MERIDIAN_FRONTEND_DIR 指向 frontend/dist",
            )
        from .core.observability import configure_logging

        configure_logging(os.getenv("MERIDIAN_LOG_LEVEL", "INFO"))

    settings.ensure_directories()
    if not app.config.get("TESTING") and settings.environment == "production":
        from .core.instance_lock import acquire_instance_lock

        app.extensions["meridian_instance_lock"] = acquire_instance_lock(
            settings.storage_dir / ".instance.lock",
        )
    database = Database(settings.database_path)
    database.initialize()
    app.extensions["meridian_db"] = database
    from .core.metrics import RequestMetrics

    app.extensions["meridian_metrics"] = RequestMetrics()

    CORS(
        app,
        resources={r"/api/*": {"origins": settings.allowed_origins}},
        supports_credentials=True,
    )

    from .api import register_blueprints

    register_blueprints(app)
    if not app.config.get("TESTING"):
        from .services.jobs import get_job_manager

        job_manager = get_job_manager(app)
        atexit.register(job_manager.shutdown)
        from .services.feishu_bot import start_long_connection

        for connector in database.list("connectors", limit=5000):
            if connector.get("type") == "lark_app" and connector.get("purpose") == "feishu_bot":
                start_long_connection(app, connector)
    if not app.config.get("TESTING") and os.getenv("MERIDIAN_DISABLE_SCHEDULER", "0") != "1":
        from .services.scheduler import start_scheduler

        scheduler = start_scheduler(app)
        atexit.register(scheduler.stop)

    @app.before_request
    def establish_request_context():
        supplied = str(request.headers.get("X-Request-ID") or "")
        g.request_id = supplied if re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", supplied) else uuid.uuid4().hex
        g.request_started = time.perf_counter()
        g.csp_nonce = secrets.token_urlsafe(18)

    @app.before_request
    def reject_oversized_json():
        if (
            request.is_json and request.content_length is not None
            and request.content_length > settings.max_json_bytes
        ):
            return jsonify({"ok": False, "error": "JSON 请求体超过大小限制"}), 413
        return None

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
            allowed_origin = origin in settings.allowed_origins
        except ValueError:
            same_origin = allowed_origin = False
        if not same_origin and not allowed_origin:
            return jsonify({"ok": False, "error": "跨站写入已拒绝"}), 403
        return None

    @app.before_request
    def verify_csrf_token():
        if app.config.get("TESTING") or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if request.path in {"/api/auth/register", "/api/auth/login", "/api/auth/send-code", "/api/auth/reset-password"} or request.path in {"/api/integrations/events", "/api/feishu-bot/events"}:
            return None
        if not session.get("user_id"):
            return None
        expected = str(session.get("csrf_token") or "")
        supplied = str(request.headers.get("X-CSRF-Token") or "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return jsonify({"ok": False, "error": "CSRF 校验失败，请刷新页面后重试"}), 403
        return None

    @app.before_request
    def protect_metrics():
        if request.path != "/api/metrics":
            return None
        token = os.getenv("MERIDIAN_METRICS_TOKEN", "").strip()
        supplied = str(request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if token and supplied and hmac.compare_digest(token, supplied):
            return None
        if settings.environment != "production" and request.remote_addr in {"127.0.0.1", "::1", None}:
            return None
        return jsonify({"ok": False, "error": "指标端点鉴权失败"}), 401

    @app.before_request
    def require_authenticated_workspace():
        if not request.path.startswith("/api/") or request.path in {"/api/health", "/api/ready", "/api/metrics"}:
            return None
        if request.path.startswith("/api/auth/") or request.path in {"/api/integrations/events", "/api/feishu-bot/events"}:
            return None
        users = database.list("users", include_archived=True, limit=1)
        if not users:
            if settings.environment != "production":
                return None
            return jsonify({"ok": False, "error": "生产实例尚未创建系统所有者"}), 401
        # Flask's signed session is authoritative; the unusual local mode is disabled once a user exists.
        user_id = session.get("user_id")
        user = database.get("users", str(user_id)) if user_id else None
        session_version = int(session.get("session_version") or 0)
        if (
            not user or not user.get("enabled", True)
            or session_version != int(user.get("session_version") or 0)
        ):
            session.clear()
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
        owner_only_prefixes = (
            "/api/providers", "/api/models", "/api/mcp", "/api/connectors",
            "/api/compute", "/api/system/", "/api/hooks", "/api/feishu-bot",
            "/api/warehouse/engines", "/api/lifecycle",
        )
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.path.startswith(owner_only_prefixes)
            and membership.get("role") != "owner"
        ):
            return jsonify({"ok": False, "error": "该敏感配置操作仅限工作空间所有者"}), 403
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

    @app.get("/api/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "meridian-analytics-workbench",
            "version": "1.0.0",
            "database": database.ping(),
        })

    @app.get("/api/ready")
    def ready():
        database_status = database.ping()
        storage_ready = settings.storage_dir.is_dir() and os.access(settings.storage_dir, os.W_OK)
        try:
            from .services.data_plane.factory import sandbox_client

            sandbox = sandbox_client().capability()
        except Exception as exc:
            sandbox = {"available": False, "host_fallback": False, "error": str(exc)}
        owner_ready = bool(database.list("users", include_archived=True, limit=1))
        provider_ready = bool(os.getenv("OPENAI_API_KEY", "").strip())
        if not provider_ready:
            from .services.security import SecretVault

            vault = SecretVault(app.config["VAULT_KEY"])
            for item in database.list("providers", limit=5000):
                if not item.get("enabled", True):
                    continue
                try:
                    provider_ready = bool(
                        (vault.open(item.get("credential", ""), {}) or {}).get("api_key"),
                    )
                except (TypeError, ValueError):
                    provider_ready = False
                if provider_ready:
                    break
        base_ready = storage_ready and database_status == "ready"
        production_dependencies = owner_ready and provider_ready and bool(sandbox.get("available"))
        status = 200 if base_ready and (settings.environment != "production" or production_dependencies) else 503
        return jsonify({
            "ok": status == 200, "database": database_status, "storage_writable": storage_ready,
            "owner_configured": owner_ready, "model_provider_configured": provider_ready,
            "scheduler": "disabled" if os.getenv("MERIDIAN_DISABLE_SCHEDULER", "0") == "1" else "enabled",
            "sandbox": sandbox,
        }), status

    @app.get("/api/metrics")
    def metrics():
        with database.connect() as connection:
            run_rows = connection.execute(
                "SELECT execution_status,quality_status FROM agent_runs ORDER BY updated_at DESC LIMIT 5000",
            ).fetchall()
        payload = app.extensions["meridian_metrics"].render(
            jobs=database.list("jobs", limit=5000),
            agent_runs=[dict(row) for row in run_rows],
        )
        return Response(payload, content_type="text/plain; version=0.0.4; charset=utf-8")

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
            return jsonify({
                "ok": False, "error": "服务暂时无法完成该操作，请查看服务日志",
                "request_id": getattr(g, "request_id", ""),
            }), 500
        return "Internal Server Error", 500

    @app.after_request
    def security_headers(response):
        request_id = getattr(g, "request_id", uuid.uuid4().hex)
        response.headers.setdefault("X-Request-ID", request_id)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        if settings.environment == "production" and app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains",
            )
        nonce = str(getattr(g, "csp_nonce", ""))
        script_policy = f"'self' 'nonce-{nonce}'"
        if settings.environment != "production" and not app.config.get("TESTING"):
            script_policy += " 'unsafe-eval'"
        response.headers.setdefault(
            "Content-Security-Policy",
            f"default-src 'self'; script-src {script_policy}; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
            "frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
        )
        if response.mimetype == "text/html" or request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.path.startswith("/api/"):
            route = request.url_rule.rule if request.url_rule else "unmatched"
            app.extensions["meridian_metrics"].observe(
                request.method, route, response.status_code,
                max(0.0, time.perf_counter() - getattr(g, "request_started", time.perf_counter())),
            )
            logging.getLogger("meridian.request").info(
                "request_completed",
                extra={"request_detail": {
                    "request_id": request_id, "method": request.method, "path": request.path,
                    "status": response.status_code,
                    "duration_ms": round(
                        (time.perf_counter() - getattr(g, "request_started", time.perf_counter())) * 1000, 2,
                    ),
                    "user_id": str(session.get("user_id") or "anonymous"),
                }},
            )
        return response

    return app


__all__ = ["create_app"]
