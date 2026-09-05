from __future__ import annotations

import json

import pandas as pd

from backend.services.charts import catalog, make_spec


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": ["华东", "华南", "华北", "西南", "华东", "华南"],
            "channel": ["线上", "线上", "线下", "线下", "线下", "线上"],
            "date": pd.date_range("2026-01-01", periods=6, freq="D"),
            "value": [12.0, 18.0, 9.0, 23.0, 15.0, 27.0],
            "target": [10.0, 16.0, 11.0, 20.0, 16.0, 25.0],
            "lower": [9.0, 15.0, 7.0, 20.0, 12.0, 24.0],
            "upper": [15.0, 21.0, 12.0, 26.0, 18.0, 30.0],
        }
    )


def test_every_catalog_chart_produces_portable_echarts_json():
    frame = _frame()
    chart_ids = [item["id"] for item in catalog()]
    assert len(chart_ids) == 47
    assert len(set(chart_ids)) == 47
    for chart_type in chart_ids:
        kwargs = {
            "chart_type": chart_type,
            "x": "region",
            "y": ["value", "target", "lower", "upper"],
        }
        if chart_type == "calendar":
            kwargs.update(x="date", y=["value"])
        elif chart_type in {"scatter", "bubble", "connected_scatter"}:
            kwargs.update(x="value", y=["target", "upper"])
        elif chart_type in {"heatmap", "network", "chord", "sankey"}:
            selected = frame[["region", "channel", "value"]]
            spec = make_spec(selected, **kwargs)
            assert spec["option"]["series"]
            json.dumps(spec, allow_nan=False)
            continue
        elif chart_type == "dot_map":
            kwargs.update(x="region", y=["value", "target", "upper"])
        spec = make_spec(frame, **kwargs)
        assert spec["type"] == chart_type
        assert spec["option"].get("series")
        encoded = json.dumps(spec, ensure_ascii=False, allow_nan=False)
        assert '"type": "custom"' not in encoded
        assert "function(" not in encoded


def test_chart_semantics_are_not_generic_aliases():
    frame = _frame()
    histogram = make_spec(frame, chart_type="histogram", y=["value"])["option"]
    waterfall = make_spec(frame, chart_type="waterfall", x="region", y=["value"])["option"]
    sankey = make_spec(
        frame[["region", "channel", "value"]], chart_type="sankey",
    )["option"]
    error_bar = make_spec(
        frame, chart_type="error_bar", x="region", y=["value", "lower", "upper"],
    )["option"]
    assert histogram["series"][0]["name"] == "频数"
    assert waterfall["series"][0]["itemStyle"]["color"] == "transparent"
    assert sankey["series"][0]["type"] == "sankey"
    assert [item["name"] for item in error_bar["series"]] == ["下界", "上界", "value"]


def test_dashboard_export_contains_offline_echarts_not_raw_json(client, source):
    query = client.post(
        "/api/query",
        json={
            "source_ids": [source["id"]],
            "sql": "SELECT region, SUM(sales) AS sales FROM data GROUP BY region",
        },
    ).get_json()["result"]
    chart = client.post(
        "/api/charts/spec", json={"result_id": query["id"], "type": "bar", "title": "销售"},
    ).get_json()["item"]
    dashboard = client.post(
        "/api/dashboards",
        json={
            "name": "离线看板",
            "widgets": [{"id": "sales", "title": "销售", "result_id": query["id"], "chart": chart["spec"]}],
        },
    ).get_json()["item"]
    artifact = client.post(f"/api/dashboards/{dashboard['id']}/export").get_json()["artifact"]
    page = client.get(artifact["download_url"])
    assert page.status_code == 200
    assert b"echarts.init" in page.data
    assert b'<script id="dashboard-data" type="application/json">' in page.data
    assert b"<pre>" not in page.data
