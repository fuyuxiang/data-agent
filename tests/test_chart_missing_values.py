"""Validate the real server chart specs consumed by the frontend."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from backend.services.charts import make_spec


@pytest.mark.parametrize("values,expected,minimum", [
    ([-5, 0, None], [-5, 0], -5),
    ([-7, -2, None], [-7, -2], -7),
    ([0, 0, None], [0, 0], 0),
    ([None, "", "invalid"], [], 0),
])
def test_heatmap_server_option_preserves_missing_zero_and_negative(values, expected, minimum):
    frame = pd.DataFrame({"region": ["A", "B", "C"], "channel": ["X"] * 3, "amount": values})
    spec = make_spec(frame, chart_type="heatmap", x="region", y="amount")
    option = spec["option"]
    assert [point[2] for point in option["series"][0]["data"]] == expected
    assert option["visualMap"]["min"] == minimum
    assert option["visualMap"]["max"] > option["visualMap"]["min"]
    json.dumps(spec, allow_nan=False)

    # A normal spec contains option, so exercise the frontend's actual branch.
    runner = """
      globalThis.Vue = {};
      globalThis.document = { documentElement: {} };
      globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
      globalThis.matchMedia = () => ({ matches: false });
      const { readFileSync } = await import('node:fs');
      const { buildOption } = await import('./frontend/src/components/chart.js');
      console.log(JSON.stringify(buildOption(JSON.parse(readFileSync(0, 'utf8')))));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", runner], input=json.dumps(spec),
        text=True, capture_output=True, check=True, cwd=Path(__file__).resolve().parents[1],
    )
    rendered = json.loads(result.stdout)
    assert rendered["series"][0]["data"] == option["series"][0]["data"]
    assert rendered["visualMap"]["min"] == minimum


@pytest.mark.parametrize("kind", ["pie", "donut", "rose", "gauge", "funnel", "treemap", "sunburst"])
def test_composition_missing_values_do_not_become_zero(kind):
    spec = make_spec(pd.DataFrame({"region": ["A", "B", "C"], "amount": [None, 0, -3]}),
                     chart_type=kind, x="region", y="amount")
    points = spec["option"]["series"][0]["data"]
    assert points[0]["value"] is None
    if kind != "gauge":
        assert [point["value"] for point in points[1:]] == [0, -3]
    json.dumps(spec, allow_nan=False)


def test_calendar_heatmap_and_empty_distributions_do_not_invent_zero_samples():
    spec = make_spec(pd.DataFrame({"date": ["2026-01-01", "2026-01-02", "2026-01-03"], "amount": [-4, 0, None]}),
                     chart_type="calendar", x="date", y="amount")
    assert spec["option"]["series"][0]["data"] == [["2026-01-01", -4], ["2026-01-02", 0]]
    assert spec["option"]["visualMap"]["min"] == -4
    frame = pd.DataFrame({"amount": [float("nan"), float("nan")]})
    box = make_spec(frame, chart_type="boxplot", x="amount", y="amount")
    assert box["option"]["series"][0]["data"] == [[None] * 5]
    histogram = make_spec(frame, chart_type="histogram", x="amount", y="amount")
    assert histogram["option"]["series"][0]["data"] == []
    json.dumps(box, allow_nan=False)


def test_frontend_recovers_missing_cells_in_legacy_saved_heatmap_options():
    spec = make_spec(pd.DataFrame({"region": ["A", "B"], "channel": ["X", "X"], "amount": [-5, None]}),
                     chart_type="heatmap", x="region", y="amount")
    spec["option"]["series"][0]["data"] = [[0, 0, -5], [1, 0, 0]]
    spec["option"]["visualMap"].update(min=0, max=0)
    runner = """
      globalThis.Vue = {}; globalThis.document = { documentElement: {} };
      globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
      globalThis.matchMedia = () => ({ matches: false });
      const { readFileSync } = await import('node:fs');
      const { buildOption } = await import('./frontend/src/components/chart.js');
      console.log(JSON.stringify(buildOption(JSON.parse(readFileSync(0, 'utf8')))));
    """
    result = subprocess.run(["node", "--input-type=module", "-e", runner], input=json.dumps(spec),
                            text=True, capture_output=True, check=True, cwd=Path(__file__).resolve().parents[1])
    option = json.loads(result.stdout)
    assert option["series"][0]["data"] == [[0, 0, -5]]
    assert option["visualMap"]["min"] == -5
