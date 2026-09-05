from __future__ import annotations

import io
import sqlite3
import sys
import time
import types


def test_session_sources_toggle_preview_and_warehouse(client):
    response = client.post(
        "/api/session/welcome/upload",
        data={
            "file": [
                (io.BytesIO(b"city,sales\nBeijing,12\nShanghai,18\n"), "sales.csv"),
                (io.BytesIO(b"city,cost\nBeijing,8\nShanghai,11\n"), "cost.csv"),
            ],
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["added"]) == 2
    assert payload["warehouse_autosave"]["autosaved"] is True
    source_ids = [item["source_id"] for item in payload["added"]]

    listed = client.get("/api/session/welcome/sources").get_json()["sources"]
    assert {item["id"] for item in listed} == set(source_ids)
    assert all(item["active"] for item in listed)

    toggle = client.post(f"/api/session/welcome/sources/{source_ids[1]}/toggle")
    assert toggle.status_code == 200
    assert toggle.get_json()["active"] is False
    preview = client.get("/api/session/welcome/preview").get_json()
    assert {item["source_id"] for item in preview["tables"]} == {source_ids[0]}
    table = client.get(
        "/api/session/welcome/preview-table",
        query_string={"source_id": source_ids[0], "table": "data"},
    ).get_json()
    assert table["total_rows"] == 2
    assert table["rows"][0]["city"] == "Beijing"

    saved = client.post("/api/session/welcome/data-warehouse/save", json={"name": "two sources"})
    assert saved.status_code == 200
    filename = saved.get_json()["filename"]
    assert filename.endswith(".json")
    assert any(item["filename"] == filename for item in client.get("/api/data-warehouses").get_json())

    assert client.delete("/api/session/welcome/datasource").status_code == 200
    assert client.get("/api/session/welcome/sources").get_json()["sources"] == []
    restored = client.post("/api/session/welcome/data-warehouse/load", json={"filename": filename}).get_json()
    assert restored["restored"] == 2
    restored_sources = restored["sources"]
    assert len(restored_sources) == 2
    assert sum(item["active"] for item in restored_sources) == 1

    removed = client.delete(f"/api/session/welcome/sources/{source_ids[0]}")
    assert len(removed.get_json()["sources"]) == 1
    assert client.delete(f"/api/data-warehouses/{filename}").status_code == 200


def test_large_excel_job_finalize_is_idempotent(client, monkeypatch):
    monkeypatch.setenv("MERIDIAN_EXCEL_JOB_THRESHOLD", "1")
    from openpyxl import Workbook

    stream = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["city", "sales"])
    sheet.append(["Beijing", 12])
    workbook.save(stream)
    stream.seek(0)
    response = client.post(
        "/api/session/welcome/upload",
        data={"file": (stream, "large.xlsx")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    job_id = response.get_json()["pending_jobs"][0]["id"]
    job = None
    for _ in range(100):
        job = client.get(f"/api/session/welcome/jobs/{job_id}").get_json()["job"]
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)
    assert job["status"] == "succeeded", job
    events = client.get("/api/session/welcome/jobs/events").get_json()
    assert events["events"]

    first = client.post(f"/api/session/welcome/upload-jobs/{job_id}/finalize")
    second = client.post(f"/api/session/welcome/upload-jobs/{job_id}/finalize")
    assert first.status_code == second.status_code == 200
    assert first.get_json()["added"][0]["source_id"] == second.get_json()["added"][0]["source_id"]
    assert len(client.get("/api/session/welcome/sources").get_json()["sources"]) == 1


def test_database_scope_and_saved_credentials_are_server_enforced(client, app):
    database_path = app.config["SETTINGS"].storage_dir / "remote.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sales(city TEXT, amount INTEGER)")
        connection.execute("CREATE TABLE private_notes(note TEXT)")
        connection.execute("INSERT INTO sales VALUES('Beijing', 12)")
        connection.execute("INSERT INTO private_notes VALUES('secret')")
    url = f"sqlite:///{database_path}"
    connected = client.post(
        "/api/session/welcome/connect-db",
        json={"connection_string": url, "name": "warehouse db"},
    )
    assert connected.status_code == 200, connected.get_json()
    source_id = connected.get_json()["source_id"]
    configured = client.get("/api/datasource-configs").get_json()
    assert configured["sql"]["has_connection_string"] is True
    assert "connection_string" not in configured["sql"]
    assert url not in str(configured)

    scope = client.post(
        f"/api/session/welcome/sources/{source_id}/analysis-tables",
        json={"tables": ["sales"]},
    )
    assert scope.status_code == 200
    with app.app_context():
        from backend.services.datasets import source_frames

        source = app.extensions["meridian_db"].get("sources", source_id)
        assert set(source_frames(source)) == {"sales"}
    bad = client.post(
        f"/api/session/welcome/sources/{source_id}/analysis-tables",
        json={"tables": ["does_not_exist"]},
    )
    assert bad.status_code == 400


def test_api_connector_exact_auth_and_secret_mask(client, monkeypatch):
    class Response:
        headers = {"Content-Type": "text/csv"}
        text = "city,sales\nBeijing,12\n"

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            raise AssertionError("CSV response should not be decoded as JSON")

    observed = {}

    def fake_request(method, url, **kwargs):
        observed.update({"method": method, "url": url, **kwargs})
        return Response()

    monkeypatch.setattr("backend.services.datasets.validate_outbound_url", lambda url: url)
    monkeypatch.setattr("backend.services.datasets.safe_http_request", fake_request)
    result = client.post(
        "/api/session/welcome/connect-api",
        json={"url": "https://data.example.test/sales.csv", "auth_type": "api_key", "auth_value": "top-secret"},
    )
    assert result.status_code == 200, result.get_json()
    assert observed["headers"]["X-API-Key"] == "top-secret"
    configs = client.get("/api/datasource-configs").get_json()
    assert configs["api"]["has_auth_value"] is True
    assert "top-secret" not in str(configs)
    assert client.delete("/api/datasource-configs/api").status_code == 200


def test_google_service_account_loads_every_nonempty_worksheet(client, monkeypatch):
    class Credentials:
        @staticmethod
        def from_service_account_info(value, scopes):
            assert value["client_email"] == "robot@example.test"
            assert len(scopes) == 2
            return object()

    class Worksheet:
        def __init__(self, title, rows):
            self.title, self._rows = title, rows

        def get_all_values(self):
            return self._rows

    class Document:
        title = "Sales book"

        @staticmethod
        def worksheets():
            return [
                Worksheet("销售", [["城市", "销售额"], ["北京", "12"]]),
                Worksheet("成本", [["标题"], ["城市", "成本"], ["北京", "8"]]),
                Worksheet("空表", [["标题"]]),
            ]

    class Client:
        def set_timeout(self, value):
            assert value == 20

        @staticmethod
        def open_by_url(value):
            assert value.startswith("https://docs.google.com/")
            return Document()

    gspread = types.ModuleType("gspread")
    gspread.authorize = lambda credentials: Client()
    service_account = types.ModuleType("google.oauth2.service_account")
    service_account.Credentials = Credentials
    oauth2 = types.ModuleType("google.oauth2")
    oauth2.service_account = service_account
    google = types.ModuleType("google")
    google.oauth2 = oauth2
    monkeypatch.setitem(sys.modules, "gspread", gspread)
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", service_account)

    result = client.post(
        "/api/session/welcome/connect-gsheets",
        json={
            "creds_json": '{"client_email":"robot@example.test"}',
            "spreadsheet": "https://docs.google.com/spreadsheets/d/example-sheet-id/edit",
        },
    )
    assert result.status_code == 200, result.get_json()
    assert len(result.get_json()["sources"][0]["tables"]) == 2
    config = client.get("/api/datasource-configs").get_json()["gsheets"]
    assert config["has_creds_json"] is True
    assert "robot@example.test" not in str(config)
