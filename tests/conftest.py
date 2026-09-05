from __future__ import annotations

import io

import pytest

from backend import create_app


@pytest.fixture()
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "DATABASE_PATH": tmp_path / "test.sqlite3",
        "STORAGE_DIR": tmp_path / "storage",
        "SECRET_KEY": "test-secret",
    })


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def source(client):
    payload = (
        "region,month,sales,cost,converted\n"
        "North,2026-01-01,120,80,1\n"
        "South,2026-01-01,90,75,0\n"
        "North,2026-02-01,150,95,1\n"
        "South,2026-02-01,110,88,1\n"
        "North,2026-03-01,170,100,1\n"
        "South,2026-03-01,105,90,0\n"
    ).encode()
    response = client.post(
        "/api/sources/upload",
        data={"file": (io.BytesIO(payload), "sales.csv"), "workspace_id": "default"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    return response.get_json()["items"][0]

