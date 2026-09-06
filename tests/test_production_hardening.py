from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import sqlite3
import tarfile
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from backend.services.agent_tools import AgentToolContext, tool_schemas
from backend.services.models import resolve_provider
from backend.services.mcp import _safe_command
from backend.services.sql_security import bounded_read_only_sql
from backend.services.security import SecretVault


def test_database_rejects_cross_workspace_record_takeover(app):
    database = app.extensions["meridian_db"]
    database.put(
        "mcp_servers", {"id": "shared-id", "workspace_id": "alpha", "name": "alpha"},
        workspace_id="alpha",
    )
    with pytest.raises(PermissionError):
        database.put(
            "mcp_servers", {"id": "shared-id", "workspace_id": "beta", "name": "beta"},
            workspace_id="beta",
        )
    assert database.get("mcp_servers", "shared-id")["name"] == "alpha"


def test_prometheus_metrics_use_bounded_route_labels(client):
    client.get("/api/health")
    response = client.get("/api/metrics")
    assert response.status_code == 200
    content = response.get_data(as_text=True)
    assert "meridian_http_requests_total" in content
    assert 'route="/api/health"' in content
    assert "meridian_http_request_duration_seconds_bucket" in content


def test_production_metrics_require_bearer_token_and_readiness_is_strict(monkeypatch, tmp_path):
    from backend import create_app

    token = "metrics-token-that-is-longer-than-thirty-two-characters"
    monkeypatch.setenv("MERIDIAN_ENV", "production")
    monkeypatch.setenv("MERIDIAN_METRICS_TOKEN", token)
    monkeypatch.setenv("MERIDIAN_STORAGE_DIR", str(tmp_path / "storage"))
    application = create_app({
        "TESTING": True,
        "DATABASE_PATH": tmp_path / "production-metrics.sqlite3",
        "STORAGE_DIR": tmp_path / "storage",
    })
    client = application.test_client()

    assert client.get("/api/metrics").status_code == 401
    assert client.get("/api/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(
        "/api/metrics", headers={"Authorization": f"Bearer {token}"},
    ).status_code == 200
    readiness = client.get("/api/ready")
    assert readiness.status_code == 503
    assert readiness.get_json()["owner_configured"] is False


def test_explicit_embedding_provider_fails_closed(monkeypatch):
    from backend.services import embeddings
    from backend.services.knowledge import _cosine

    monkeypatch.setattr(embeddings, "get_config", lambda _wid: {
        "mode": "cloud", "url": "https://embedding.example.com", "model": "embedding-model",
        "token": "secret", "token_configured": True,
    })
    monkeypatch.setattr(embeddings, "local_installed", lambda: True)
    monkeypatch.setattr(
        embeddings, "cloud_embed_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("cloud unavailable")),
    )
    with pytest.raises(ConnectionError, match="cloud unavailable"):
        embeddings.embed_batch(["业务口径"])
    assert _cosine([1.0, 0.0], [1.0]) == 0.0


def test_provider_resolution_and_bootstrap_are_workspace_scoped(app, client):
    database = app.extensions["meridian_db"]
    with app.app_context():
        database.put(
            "providers",
            {"id": "provider-alpha", "workspace_id": "alpha", "name": "Alpha", "enabled": True},
            workspace_id="alpha",
        )
        provider, model_client = resolve_provider("provider-alpha", "beta")
        assert provider is model_client is None

    response = client.get("/api/bootstrap", headers={"X-Workspace-Id": "beta"})
    assert response.status_code == 200
    assert "provider-alpha" not in {item["id"] for item in response.get_json()["providers"]}


def test_query_limit_is_rewritten_and_enforced(client, source):
    bounded = bounded_read_only_sql("SELECT * FROM data LIMIT 999", 5, "duckdb")
    assert "LIMIT 5" in bounded.upper()
    result = client.post(
        "/api/query",
        json={"source_ids": [source["id"]], "sql": "SELECT * FROM data LIMIT 999", "limit": 3},
    )
    assert result.status_code == 200
    assert result.get_json()["result"]["rows"] == 3


def test_retired_agent_mutations_cannot_be_reenabled_by_session_policy(app):
    database = app.extensions["meridian_db"]
    session = database.put(
        "sessions", {"id": "policy-session", "workspace_id": "default", "name": "policy"},
        workspace_id="default",
    )
    context = AgentToolContext(database, "default", session["id"], [])
    old_testing = app.config["TESTING"]
    app.config["TESTING"] = False
    try:
        with app.app_context():
            names = {item["function"]["name"] for item in tool_schemas(context)}
            assert "workspace_write_file" not in names
            assert "configure_hooks" not in names
            database.patch("sessions", session["id"], {"agent_allow_mutations": True})
            names = {item["function"]["name"] for item in tool_schemas(context)}
            assert "workspace_write_file" not in names
            assert "configure_hooks" not in names
    finally:
        app.config["TESTING"] = old_testing


def test_stdio_mcp_disabled_outside_explicit_test_mode(app, monkeypatch):
    monkeypatch.delenv("MERIDIAN_ENABLE_STDIO_MCP", raising=False)
    old_testing = app.config["TESTING"]
    app.config["TESTING"] = False
    try:
        with app.app_context(), pytest.raises(PermissionError):
            _safe_command("python3")
    finally:
        app.config["TESTING"] = old_testing


def test_daily_quota_blocks_model_turn_before_provider_call(app, client, monkeypatch):
    class Completions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("quota should be checked before provider call")

    completions = Completions()
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(
        "backend.services.advanced_agent.resolve_provider",
        lambda _provider_id=None, _workspace_id="default": ({"model": "fake"}, fake),
    )
    app.config["SETTINGS"] = replace(app.config["SETTINGS"], daily_token_limit=1)
    database = app.extensions["meridian_db"]
    database.put(
        "usage_events",
        {"id": "quota-used", "workspace_id": "default", "total_tokens": 1},
        workspace_id="default",
    )
    session = client.post("/api/sessions", json={"name": "quota"}).get_json()["item"]
    run = client.post(
        "/api/analyses", json={"session_id": session["id"], "objective": "分析"},
    ).get_json()["item"]
    confirmed = client.post(
        f"/api/analyses/{run['id']}/contract/confirm", json={"expected_version": 1},
    ).get_json()
    deadline = time.time() + 3
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{confirmed['job']['id']}").get_json()["item"]
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    current = client.get(f"/api/analyses/{run['id']}").get_json()["item"]
    assert current["execution_status"] == "failed"
    assert current["stop_reason"] == "daily_model_budget_exceeded"
    assert not completions.calls


def test_online_self_update_is_removed(client):
    response = client.post("/api/system/update")
    assert response.status_code == 404
    assert response.get_json()["error"] == "接口不存在"


def test_spreadsheet_exports_neutralize_formula_cells(app):
    from openpyxl import load_workbook

    from backend.services.exports import export_data

    with app.app_context():
        artifact = export_data(
            {"format": "xlsx", "rows": [{"label": "=HYPERLINK(\"https://example.test\")"}]},
            "default",
        )
        workbook = load_workbook(app.config["SETTINGS"].export_dir / artifact["filename"], data_only=False)
        cell = workbook.active["A2"]
        assert cell.data_type != "f"
        assert cell.value.startswith("'=")


def test_workspace_invitation_registers_bound_member(app):
    owner = app.test_client()
    invited = app.test_client()
    owner.post(
        "/api/auth/register",
        json={"email": "owner@company.test", "password": "correct-horse", "name": "Owner"},
    )
    workspace = owner.post("/api/workspaces", json={"name": "Enterprise"}).get_json()["item"]
    invitation = owner.post(
        f"/api/workspaces/{workspace['id']}/invitations",
        json={"email": "analyst@company.test", "role": "editor"},
    )
    assert invitation.status_code == 201
    registered = invited.post(
        "/api/auth/register",
        json={
            "email": "analyst@company.test", "password": "correct-horse", "name": "Analyst",
            "invitation_token": invitation.get_json()["invitation_token"],
        },
    )
    assert registered.status_code == 201
    assert registered.get_json()["active_workspace_id"] == workspace["id"]


def test_invalid_invitation_does_not_create_orphan_user(app):
    database = app.extensions["meridian_db"]
    token = "missing-workspace-invitation"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    database.put(
        "invitations",
        {
            "id": f"invite_{token_hash}", "workspace_id": "missing", "email": "orphan@company.test",
            "role": "viewer", "status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
        workspace_id="missing",
    )
    response = app.test_client().post(
        "/api/auth/register",
        json={
            "email": "orphan@company.test", "password": "correct-horse",
            "invitation_token": token,
        },
    )
    assert response.status_code == 404
    assert not any(user.get("email") == "orphan@company.test" for user in database.list("users"))


def test_password_reset_revokes_existing_sessions(app):
    first = app.test_client()
    existing_session = app.test_client()
    first.post(
        "/api/auth/register",
        json={"email": "owner@company.test", "password": "correct-horse"},
    )
    assert existing_session.post(
        "/api/auth/login",
        json={"email": "owner@company.test", "password": "correct-horse"},
    ).status_code == 200
    database = app.extensions["meridian_db"]
    database.put(
        "email_codes",
        {
            "id": "reset-owner", "email": "owner@company.test", "attempts": 0,
            "code": SecretVault(app.config["VAULT_KEY"]).seal({"value": "123456"}),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        },
    )
    reset = first.post(
        "/api/auth/reset-password",
        json={"email": "owner@company.test", "code": "123456", "password": "new-correct-horse"},
    )
    assert reset.status_code == 200
    assert existing_session.get("/api/bootstrap").status_code == 401


def test_api_cannot_mutate_foreign_workspace_record(app):
    client = app.test_client()
    client.post(
        "/api/auth/register",
        json={"email": "owner@company.test", "password": "correct-horse"},
    )
    database = app.extensions["meridian_db"]
    database.put(
        "charts", {"id": "foreign-chart", "workspace_id": "foreign", "name": "private"},
        workspace_id="foreign",
    )
    response = client.delete("/api/charts/foreign-chart", headers={"X-Workspace-Id": "default"})
    assert response.status_code == 404
    assert database.get("charts", "foreign-chart").get("archived_at") is None


def test_production_instance_lock_is_exclusive(tmp_path):
    from backend.core.instance_lock import acquire_instance_lock

    first = acquire_instance_lock(tmp_path / "instance.lock")
    try:
        with pytest.raises(RuntimeError, match="仅支持单实例"):
            acquire_instance_lock(tmp_path / "instance.lock")
    finally:
        first.close()


def test_model_provider_blocks_private_network(client):
    response = client.post(
        "/api/providers",
        json={"name": "metadata", "base_url": "http://127.0.0.1:9000/v1", "api_key": "secret"},
    )
    assert response.status_code == 400
    assert "禁止" in response.get_json()["error"]


def test_backup_contains_consistent_database_and_files(app, tmp_path):
    from scripts.backup import create_backup

    storage = app.config["SETTINGS"].storage_dir
    marker = storage / "uploads" / "evidence.txt"
    marker.write_text("verified", encoding="utf-8")
    output = tmp_path / "backup.tar.gz"
    result = create_backup(storage, output, app.extensions["meridian_db"].path)
    assert len(result["sha256"]) == 64
    extract = tmp_path / "restored"
    with tarfile.open(output, "r:gz") as archive:
        archive.extractall(extract, filter="data")
    assert (extract / "storage/uploads/evidence.txt").read_text(encoding="utf-8") == "verified"
    with sqlite3.connect(extract / "storage/meridian.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 1


def test_encrypted_backup_can_be_verified_and_restored(app, tmp_path):
    from scripts.backup import BACKUP_MAGIC, create_backup
    from scripts.restore import restore_backup

    storage = app.config["SETTINGS"].storage_dir
    marker = storage / "knowledge" / "private.txt"
    marker.write_text("confidential", encoding="utf-8")
    key = "backup-key-that-is-longer-than-thirty-two-characters"
    output = tmp_path / "backup.tar.gz.enc"
    result = create_backup(storage, output, app.extensions["meridian_db"].path, key)
    assert result["encrypted"] is True
    assert output.read_bytes().startswith(BACKUP_MAGIC)
    assert b"confidential" not in output.read_bytes()

    destination = tmp_path / "restore"
    restored = restore_backup(
        output, destination, encryption_key=key, expected_sha256=str(result["sha256"]),
    )
    assert restored["database_integrity"] == "ok"
    assert (destination / "storage/knowledge/private.txt").read_text(encoding="utf-8") == "confidential"
    with pytest.raises(ValueError, match="空目录"):
        restore_backup(output, destination, encryption_key=key)


def test_outbound_session_pins_dns_and_ignores_environment_proxy(monkeypatch):
    import requests

    from backend.services.security import _pinned_session, validate_outbound_url

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    assert validate_outbound_url("https://api.example.com/v1") == "https://api.example.com/v1"
    session = _pinned_session("https://api.example.com/v1")
    try:
        assert session.trust_env is False
        adapter = session.get_adapter("https://api.example.com/v1")
        prepared = requests.Request("GET", "https://api.example.com/v1").prepare()
        host, pool = adapter.build_connection_pool_key_attributes(prepared, True)
        assert host["host"] == "93.184.216.34"
        assert pool["assert_hostname"] == "api.example.com"
        assert pool["server_hostname"] == "api.example.com"
    finally:
        session.close()


def test_production_requires_independent_backup_key(monkeypatch, tmp_path):
    from backend import create_app

    monkeypatch.setenv("MERIDIAN_ENV", "production")
    monkeypatch.setenv("MERIDIAN_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("MERIDIAN_FRONTEND_DIR", str(tmp_path / "frontend"))
    monkeypatch.setenv("MERIDIAN_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("MERIDIAN_ENCRYPTION_KEY", "e" * 48)
    monkeypatch.setenv("MERIDIAN_TRUSTED_HOSTS", "localhost")
    monkeypatch.setenv("MERIDIAN_OUTBOUND_HOST_ALLOWLIST", "api.example.com")
    monkeypatch.delenv("MERIDIAN_BACKUP_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MERIDIAN_BACKUP_KEY"):
        create_app()


def test_production_first_owner_requires_bootstrap_token(monkeypatch, tmp_path):
    from backend import create_app

    token = "bootstrap-token-that-is-longer-than-thirty-two-characters"
    monkeypatch.setenv("MERIDIAN_ENV", "production")
    monkeypatch.setenv("MERIDIAN_BOOTSTRAP_TOKEN", token)
    application = create_app({
        "TESTING": True,
        "DATABASE_PATH": tmp_path / "production.sqlite3",
        "STORAGE_DIR": tmp_path / "storage",
    })
    client = application.test_client()
    payload = {
        "email": "production-owner@example.com", "password": "correct-horse",
        "name": "Production Owner",
    }
    denied = client.post("/api/auth/register", json=payload)
    assert denied.status_code == 403
    created = client.post(
        "/api/auth/register", json={**payload, "bootstrap_token": token},
    )
    assert created.status_code == 201


def test_invalid_compressed_upload_is_a_client_error(client):
    response = client.post(
        "/api/sources/upload",
        data={"file": (io.BytesIO(b"not-an-office-archive"), "broken.xlsx")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "无法安全解析" in response.get_json()["error"]


def test_sensitive_integrations_require_workspace_owner(app):
    owner = app.test_client()
    editor = app.test_client()
    owner.post(
        "/api/auth/register",
        json={"email": "owner@company.test", "password": "correct-horse"},
    )
    workspace = owner.post("/api/workspaces", json={"name": "Governed"}).get_json()["item"]
    registered = editor.post(
        "/api/auth/register",
        json={"email": "editor@company.test", "password": "correct-horse"},
    ).get_json()
    owner.post(
        f"/api/workspaces/{workspace['id']}/members",
        json={"email": "editor@company.test", "role": "editor"},
    )
    assert editor.post(f"/api/workspaces/{workspace['id']}/activate").status_code == 200
    assert registered["user"]["role"] == "member"
    assert editor.post(
        "/api/providers", json={"name": "blocked", "base_url": "https://api.example.test/v1"},
    ).status_code == 403
    assert editor.post(
        "/api/hooks", json={"event": "turn_end", "action": {"type": "noop"}},
    ).status_code == 403
    assert editor.put(
        "/api/feishu-bot", json={"app_id": "blocked", "app_secret": "blocked"},
    ).status_code == 403


def test_multipart_upload_uses_authenticated_active_workspace(app):
    client = app.test_client()
    client.post(
        "/api/auth/register",
        json={"email": "uploader@company.test", "password": "correct-horse"},
    )
    workspace = client.post("/api/workspaces", json={"name": "Upload target"}).get_json()["item"]
    assert client.post(f"/api/workspaces/{workspace['id']}/activate").status_code == 200
    uploaded = client.post(
        "/api/sources/upload",
        data={"file": (io.BytesIO(b"name,value\na,1\n"), "active.csv")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 201
    source = uploaded.get_json()["items"][0]
    assert source["workspace_id"] == workspace["id"]
    assert app.extensions["meridian_db"].get("sources", source["id"], workspace_id="default") is None


def test_formal_routes_cannot_cross_workspace_boundaries(app, tmp_path):
    owner = app.test_client()
    attacker = app.test_client()
    owner.post(
        "/api/auth/register",
        json={"email": "owner@company.test", "password": "correct-horse"},
    )
    secret_workspace = owner.post("/api/workspaces", json={"name": "Secret"}).get_json()["item"]
    attacker_registration = attacker.post(
        "/api/auth/register",
        json={"email": "attacker@company.test", "password": "correct-horse"},
    ).get_json()
    attacker_workspace_id = attacker_registration["active_workspace_id"]
    database = app.extensions["meridian_db"]
    database.put(
        "sessions",
        {"id": "foreign-session", "workspace_id": secret_workspace["id"], "name": "Private"},
        workspace_id=secret_workspace["id"],
    )
    database.add_message("foreign-session", "assistant", "TOP-SECRET-METRIC=42")
    database.put(
        "charts",
        {"id": "foreign-chart", "workspace_id": secret_workspace["id"], "name": "Private chart", "spec": {}},
        workspace_id=secret_workspace["id"],
    )
    database.put(
        "dashboards",
        {"id": "foreign-dashboard", "workspace_id": secret_workspace["id"], "name": "Private dashboard", "widgets": []},
        workspace_id=secret_workspace["id"],
    )

    assert attacker.get("/api/sessions/foreign-session").status_code == 404
    assert attacker.get("/api/chart/foreign-chart").status_code == 404
    assert attacker.get("/api/dashboards/foreign-dashboard").status_code == 404
    assert app.test_client().get("/dashboard/foreign-dashboard").status_code == 404

    assert attacker.post("/api/sessions", json={"name": "attacker"}).status_code == 201
    listed = attacker.get("/api/workspaces").get_json()["items"]
    assert {item["id"] for item in listed} == {attacker_workspace_id}
    assert attacker.patch(
        f"/api/workspaces/{secret_workspace['id']}",
        json={"name": "PWNED"},
    ).status_code == 403
    assert database.get("workspaces", secret_workspace["id"])["name"] == "Secret"

    outside = tmp_path / "foreign-files"
    outside.mkdir()
    (outside / "foreign.csv").write_text("secret\n42\n", encoding="utf-8")
    mounted = attacker.post(
        f"/api/workspaces/{attacker_workspace_id}/mount",
        json={"path": str(outside), "permission": "read_only"},
    )
    assert mounted.status_code == 403
    assert not any(
        source.get("name") == "foreign.csv"
        for source in database.list("sources", workspace_id=attacker_workspace_id)
    )


def test_saved_sessions_are_workspace_scoped(app):
    first = app.test_client()
    second = app.test_client()
    first.post(
        "/api/auth/register",
        json={"email": "first@company.test", "password": "correct-horse"},
    )
    second_workspace = second.post(
        "/api/auth/register",
        json={"email": "second@company.test", "password": "correct-horse"},
    ).get_json()["active_workspace_id"]
    database = app.extensions["meridian_db"]
    database.put(
        "saved_sessions",
        {
            "id": "private-session.json", "filename": "private-session.json",
            "workspace_id": "default", "name": "Private", "history": [{"role": "user", "content": "secret"}],
        },
        workspace_id="default",
    )
    target = second.post("/api/sessions", json={"name": "target"}).get_json()["item"]["id"]
    response = second.post(
        "/api/saved-sessions/private-session.json/load", json={"session_id": target},
    )
    assert response.status_code == 404
    assert second.patch(
        "/api/saved-sessions/private-session.json", json={"name": "PWNED"},
    ).status_code == 404
    assert second.delete("/api/saved-sessions/private-session.json").status_code == 404
    assert database.list("saved_sessions", workspace_id=second_workspace) == []
    assert database.get("saved_sessions", "private-session.json")["name"] == "Private"


def test_database_urls_enforce_transport_security(app, monkeypatch):
    from backend.services import datasets

    monkeypatch.setattr(datasets, "validate_outbound_host", lambda *_args, **_kwargs: ["203.0.113.10"])
    monkeypatch.setenv("MERIDIAN_DATABASE_HOST_ALLOWLIST", "db.example.test")
    app.config["SETTINGS"] = replace(app.config["SETTINGS"], environment="production")
    with app.app_context():
        postgres = make_url(datasets._build_database_url({
            "driver": "postgresql", "host": "db.example.test", "database": "analytics",
            "username": "reader", "password": "secret",
        }, "default"))
        assert postgres.query["sslmode"] == "verify-full"
        assert postgres.query["connect_timeout"] == "10"

        mysql = make_url(datasets._build_database_url({
            "driver": "mysql", "host": "db.example.test", "database": "analytics",
            "username": "reader", "password": "secret",
        }, "default"))
        assert mysql.query["ssl_verify_cert"] == "true"
        assert mysql.query["ssl_verify_identity"] == "true"

        sqlserver = make_url(datasets._build_database_url({
            "driver": "sqlserver", "host": "db.example.test", "database": "analytics",
            "username": "reader", "password": "secret",
        }, "default"))
        assert sqlserver.query["Encrypt"] == "yes"
        assert sqlserver.query["TrustServerCertificate"] == "no"
        assert sqlserver.query["driver"] == "ODBC Driver 18 for SQL Server"

        with pytest.raises(ValueError, match="TLS"):
            datasets._build_database_url({
                "driver": "postgresql", "host": "db.example.test", "database": "analytics",
                "ssl_mode": "disable",
            }, "default")
        with pytest.raises(ValueError, match="odbc_connect"):
            datasets._build_database_url({
                "url": "mssql+pyodbc://db.example.test/analytics?odbc_connect=unsafe",
            }, "default")
        monkeypatch.delenv("MERIDIAN_DATABASE_HOST_ALLOWLIST")
        with pytest.raises(ValueError, match="DATABASE_HOST_ALLOWLIST"):
            datasets._build_database_url({
                "driver": "postgresql", "host": "db.example.test", "database": "analytics",
            }, "default")


def test_job_manager_has_a_bounded_queue(app):
    from backend.services.jobs import JobManager, register_job_handler

    started = threading.Event()
    release = threading.Event()
    manager = JobManager(app, max_workers=1, max_pending=0)

    def blocking(_app, _spec, _progress, _cancel):
        started.set()
        release.wait(3)
        return {"ok": True}

    register_job_handler("bounded_queue_test", blocking)
    try:
        job = manager.submit_spec(
            workspace_id="default", session_id=None, job_type="bounded_queue_test",
            title="first", spec={"case": 1},
        )
        assert started.wait(1)
        with pytest.raises(ValueError, match="队列已满"):
            manager.submit_spec(
                workspace_id="default", session_id=None, job_type="bounded_queue_test",
                title="second", spec={"case": 2},
            )
        release.set()
        deadline = time.time() + 3
        while time.time() < deadline and app.extensions["meridian_db"].get("jobs", job["id"])["status"] != "completed":
            time.sleep(0.02)
        assert app.extensions["meridian_db"].get("jobs", job["id"])["status"] == "completed"
    finally:
        release.set()
        manager.shutdown()


def test_job_manager_releases_capacity_when_started_hook_fails(app, monkeypatch):
    from backend.services import jobs

    jobs.register_job_handler(
        "started_hook_failure_test", lambda _app, _spec, _progress, _cancel: {"ok": True},
    )
    manager = jobs.JobManager(app, max_workers=1, max_pending=0)

    def failing_started_hook(event, *_args, **_kwargs):
        if event == "job.started":
            raise RuntimeError("hook failed")
        return []

    monkeypatch.setattr(jobs, "dispatch_hooks", failing_started_hook)
    try:
        first = manager.submit_spec(
            workspace_id="default", session_id=None, job_type="started_hook_failure_test",
            title="first", spec={"case": 1},
        )
        deadline = time.time() + 3
        while time.time() < deadline:
            current = app.extensions["meridian_db"].get("jobs", first["id"])
            if current and current["status"] == "failed":
                break
            time.sleep(0.02)
        second = manager.submit_spec(
            workspace_id="default", session_id=None, job_type="started_hook_failure_test",
            title="second", spec={"case": 2},
        )
        assert second["status"] == "queued"
    finally:
        manager.shutdown()
