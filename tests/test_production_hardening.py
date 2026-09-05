from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import sqlite3
import tarfile
from types import SimpleNamespace

import pytest

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


def test_agent_mutations_and_mcp_require_session_policy(app):
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
            assert "workspace_write_file" in names
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
        "backend.services.agent_runtime.resolve_provider",
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
    stream = client.post(
        f"/api/sessions/{session['id']}/messages", json={"message": "分析"},
    ).data.decode("utf-8")
    assert "今日模型 Token 额度已用尽" in stream
    assert not completions.calls


def test_online_self_update_is_removed(client):
    response = client.post("/api/system/update")
    assert response.status_code == 410
    assert "发布流水线" in response.get_json()["error"]


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
