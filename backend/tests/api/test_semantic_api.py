import pytest
from fastapi.testclient import TestClient

from app.core.db import get_meta_session
from app.main import app
from app.semantic.orm import DatasetRow
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def client(meta_session):
    app.dependency_overrides[get_meta_session] = lambda: meta_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_datasets(client, meta_session):
    build_orders_dataset(meta_session)

    response = client.get("/api/semantic/datasets")

    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert "orders" in names


def test_dataset_detail_includes_metrics_and_enum_values(client, meta_session):
    build_orders_dataset(meta_session)

    body = client.get("/api/semantic/datasets/orders").json()

    assert body["forbidden_scenario"].startswith("不可用于")
    metric_names = [item["name"] for item in body["metrics"]]
    assert "gross_margin_rate" in metric_names
    region = next(item for item in body["fields"] if item["name"] == "region_code")
    assert {value["business_value"] for value in region["enum_values"]} == {"华东", "华南", "华北"}


def test_unknown_dataset_returns_404(client):
    response = client.get("/api/semantic/datasets/ghost")
    assert response.status_code == 404


def test_lint_report_is_clean_for_valid_dataset(client, meta_session):
    build_orders_dataset(meta_session)

    body = client.get("/api/semantic/datasets/orders/lint").json()

    assert body["publishable"] is True
    assert body["issues"] == []


def test_publish_blocked_when_lint_fails(client, meta_session):
    dataset = build_orders_dataset(meta_session, published=False)
    # Break one field so lint produces an ERROR.
    dataset.fields[0].business_name = ""
    meta_session.flush()

    response = client.post("/api/semantic/datasets/orders/publish")

    assert response.status_code == 409
    stored = meta_session.get(DatasetRow, dataset.id)
    assert stored.is_published is False


def test_publish_succeeds_when_lint_passes(client, meta_session):
    dataset = build_orders_dataset(meta_session, published=False)

    response = client.post("/api/semantic/datasets/orders/publish")

    assert response.status_code == 200
    assert response.json()["published"] is True
    stored = meta_session.get(DatasetRow, dataset.id)
    assert stored.is_published is True
