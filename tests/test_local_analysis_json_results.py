from __future__ import annotations

import json

import pandas as pd
import pytest

from backend.services.data_plane.local_analysis import LocalAnalysisRunner
from backend.services.data_plane.reviewed_analysis import _read_json_input
from backend.services.query_results import write_result_frame


def test_real_local_runner_analyzes_exact_mixed_sqlite_result(tmp_path):
    input_root = tmp_path / "inputs"
    task = input_root / "task"
    task.mkdir(parents=True)
    frame = pd.DataFrame({
        "group": ["A", "A", "B", "B"], "amount": [1, None, 3, 4],
        "mixed": [1, "A", True, None], "code": ["00123", "NA", "", None],
        "enabled": [True, None, False, True],
    }, dtype=object)
    path = write_result_frame(frame, task / "input.parquet")
    assert path.suffix == ".json"
    decoded = _read_json_input(path)
    assert decoded["mixed"].tolist() == [1, "A", True, None]
    assert decoded["code"].tolist() == ["00123", "NA", "", None]
    assert decoded["enabled"].dtype.name == "boolean"
    result = LocalAnalysisRunner(input_root=input_root, output_root=tmp_path / "outputs").execute(
        {"input": path.name, "method": "grouped_summary", "parameters": {"group": "group"}},
        input_dir=task, run_id="mixed-json",
    )
    assert result["status"] == "SUCCEEDED"
    output = pd.read_parquet(tmp_path / "outputs" / "mixed-json" / "result.parquet")
    assert output.to_dict(orient="records") == [
        {"group": "A", "amount_count": 1, "amount_mean": 1.0},
        {"group": "B", "amount_count": 2, "amount_mean": 3.5},
    ]


@pytest.mark.parametrize("payload", [
    {"columns": ["value", "value"], "data": [{"value": 1}]},
    {"columns": ["value"], "data": [[1]]},
    {"columns": ["value"], "data": [{"value": 1, "unexpected": "ignored"}]},
    {"columns": ["value"], "data": [{"value": float("nan")}]},
])
def test_json_worker_rejects_invalid_result_shapes_and_values(tmp_path, payload):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        _read_json_input(path)


def test_json_worker_checks_cell_limits_before_constructing_frame(tmp_path, monkeypatch):
    from backend.services.data_plane import reviewed_analysis

    path = tmp_path / "too-many.json"
    path.write_text(json.dumps({"columns": ["value"], "data": [{"value": 1}, {"value": 2}]}), encoding="utf-8")
    monkeypatch.setattr(reviewed_analysis, "MAX_CELLS", 1)
    with pytest.raises(ValueError, match="oversized"):
        _read_json_input(path)
