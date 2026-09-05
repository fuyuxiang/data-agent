from __future__ import annotations

import json

import pandas as pd
import pytest

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


def test_reference_chart_options_preserve_bands_measures_order_and_midlines():
    frame = pd.DataFrame({
        "label": ["A", "B", "C", "D"],
        "actual": [35, 58, 71, 82],
        "target": [45, 60, 75, 90],
        "low": [40, 40, 40, 40],
        "medium": [65, 65, 65, 65],
        "high": [100, 100, 100, 100],
        "measure": ["absolute", "relative", "relative", "total"],
        "order": [3, 1, 4, 2],
        "x": [30, 10, 40, 20],
        "y": [3, 1, 4, 2],
        "size": [4, 9, 16, 25],
    })
    bullet = make_spec(
        frame, chart_type="bullet", x="label", y=["actual", "target"],
        options={"low": "low", "medium": "medium", "high": "high"},
    )["option"]
    assert [item["name"] for item in bullet["series"][:3]] == ["high", "medium", "low"]

    waterfall = make_spec(
        frame, chart_type="waterfall", x="label", y=["actual"], options={"type": "measure"},
    )["option"]
    assert [item["name"] for item in waterfall["series"]] == ["基准", "增加", "减少", "合计"]
    assert waterfall["series"][-1]["data"][-1] == 164

    connected = make_spec(
        frame, chart_type="connected_scatter", x="x", y=["y", "size"], options={"order": "order"},
    )["option"]
    assert [item["value"][:2] for item in connected["series"][0]["data"]] == [
        [10.0, 1.0], [20.0, 2.0], [30.0, 3.0], [40.0, 4.0],
    ]
    assert connected["series"][0]["size_field"] == "size"

    bubble = make_spec(
        frame, chart_type="bubble", x="x", y=["y", "size"],
        options={"color": "label", "x_mid": "actual", "y_mid": 2.5},
    )["option"]
    assert bubble["series"][0]["color_field"] == "label"
    assert bubble["series"][0]["data"][0]["itemStyle"]["color"]
    assert bubble["series"][0]["markLine"]["data"] == [
        {"xAxis": 35.0, "name": "x_mid"}, {"yAxis": 2.5, "name": "y_mid"},
    ]

    parallel = make_spec(
        frame[["x", "y", "size", "label"]], chart_type="parallel",
        x="x", y=["y", "size"], options={"color": "label"},
    )["option"]
    assert [item["name"] for item in parallel["series"]] == ["A", "B", "C", "D"]


def test_dot_density_map_uses_city_coordinates_and_category_series():
    pytest.importorskip("pyecharts")
    frame = pd.DataFrame({
        "city": ["北京", "武汉", "宜昌"],
        "value": [120, 80, 40],
        "category": ["一线", "二线", "二线"],
    })
    spec = make_spec(
        frame, chart_type="dot_map", x="city", y=["value"], options={"category": "category"},
    )
    assert [item["name"] for item in spec["option"]["series"]] == ["一线", "二线"]
    points = [point for series in spec["option"]["series"] for point in series["data"]]
    assert points[0]["value"][:2] == [116.407526, 39.90403]
    assert spec["warnings"] == []


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
