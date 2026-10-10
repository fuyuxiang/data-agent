from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from backend.services.data_plane.local_analysis import LocalAnalysisRunner
from backend.services.data_plane.reviewed_analysis import _finite_input_frame
from backend.services.datasets import frame_records
from backend.services.query_results import _finite_frame, read_result_frame, write_result_frame


def test_object_result_storage_cleans_numeric_infinity_and_preserves_business_shapes(tmp_path):
    frame = pd.DataFrame({
        "numeric": [1.0, float("inf"), np.float32("-inf"), None],
        "mixed": [np.float64("inf"), "inf", [1, "inf"], {"value": "-inf"}],
    }, dtype=object)
    for cleaner in (_finite_frame, _finite_input_frame):
        cleaned = cleaner(frame)
        assert frame_records(cleaned) == [
            {"numeric": 1.0, "mixed": None}, {"numeric": None, "mixed": "inf"},
            {"numeric": None, "mixed": [1, "inf"]}, {"numeric": None, "mixed": {"value": "-inf"}},
        ]
        assert math.isinf(frame.loc[1, "numeric"])
    path = write_result_frame(frame, tmp_path / "mixed.parquet")
    assert path.suffix == ".json"
    assert frame_records(read_result_frame({"path": str(path)})) == frame_records(_finite_frame(frame))
    numeric_path = write_result_frame(frame[["numeric"]], tmp_path / "numeric.parquet")
    assert not pd.read_parquet(numeric_path)["numeric"].isin([math.inf, -math.inf]).any()


@pytest.mark.parametrize("format_name", ["parquet", "csv", "json"])
def test_actual_local_worker_ignores_nonfinite_samples_in_each_input_format(tmp_path, format_name):
    input_root = tmp_path / "inputs"
    task = input_root / "task"
    task.mkdir(parents=True)
    frame = pd.DataFrame({"group": ["inf", "inf", "A", "A", "A"],
                          "amount": [1.0, math.inf, 3.0, -math.inf, 5.0]})
    path = task / f"input.{format_name}"
    if format_name == "parquet":
        frame.to_parquet(path, index=False)
    elif format_name == "csv":
        frame.to_csv(path, index=False)
    else:
        # JSON exponent overflow is valid syntax and decodes to a native
        # nonfinite float; business strings with the same spelling stay text.
        payload = json.dumps({"columns": list(frame.columns), "data": frame.to_dict("records")})
        path.write_text(payload.replace("-Infinity", "-1e400").replace("Infinity", "1e400"), encoding="utf-8")
    result = LocalAnalysisRunner(input_root=input_root, output_root=tmp_path / "outputs").execute(
        {"input": path.name, "method": "grouped_summary", "parameters": {"group": "group"}},
        input_dir=task, run_id=f"nonfinite-{format_name}",
    )
    assert result["status"] == "SUCCEEDED"
    output = pd.read_parquet(tmp_path / "outputs" / f"nonfinite-{format_name}" / "result.parquet")
    assert frame_records(output) == [
        {"group": "A", "amount_count": 2, "amount_mean": 4.0},
        {"group": "inf", "amount_count": 1, "amount_mean": 1.0},
    ]
