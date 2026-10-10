from __future__ import annotations

import pandas as pd

from backend.services.datasets import frame_records
from backend.services.query_results import read_result_frame


def test_legacy_csv_preserves_late_mixed_fields_and_canonical_native_values(app):
    rows = [{"integer": index, "fraction": index / 10.0, "enabled": index % 2 == 0,
             "code": f"{index:05d}", "mixed": index if index % 2 else "text"}
            for index in range(620)]
    rows[510].update(integer="A", fraction="A", enabled="unknown")
    rows[511].update(integer="00123", fraction="00123", enabled="00123")
    rows[512].update(integer=1.5, fraction=1e-5, enabled=None)
    rows[513].update(integer=None, fraction=None, mixed="00123")
    rows[514]["mixed"] = "NA"
    rows[515]["integer"] = "9" * 5000
    frame = pd.DataFrame(rows, dtype=object)
    path = app.config["SETTINGS"].export_dir / "historical-mixed.csv"
    frame.to_csv(path, index=False)
    result = {"path": str(path), "rows": len(frame), "columns": list(frame.columns),
              "data": frame_records(frame, 300)}
    with app.app_context():
        actual = frame_records(read_result_frame(result, offset=500, limit=50), 50)
        assert actual == frame_records(frame.iloc[500:550], 50)
        assert frame_records(read_result_frame(result), 620) == frame_records(frame, 620)
        assert read_result_frame(result, offset=620, limit=50).empty


def test_legacy_csv_restores_numeric_types_without_guessing_string_codes(app):
    frame = pd.DataFrame({"amount": [0.00001, None, -2.5], "count": [1, None, 3],
                          "enabled": [True, None, False], "code": ["00123", "NA", "1"]}, dtype=object)
    path = app.config["SETTINGS"].export_dir / "historical-types.csv"
    frame.to_csv(path, index=False)
    result = {"path": str(path), "rows": len(frame), "columns": list(frame.columns), "data": frame_records(frame)}
    with app.app_context():
        decoded = read_result_frame(result)
        assert frame_records(decoded) == result["data"]
        assert list(decoded.select_dtypes("number").columns) == ["amount", "count"]
        assert decoded["enabled"].dtype.name == "boolean"
        assert decoded["code"].tolist() == ["00123", "NA", "1"]


def test_legacy_csv_retains_exact_mixed_preview_even_for_numeric_text(app):
    frame = pd.DataFrame({"value": [1, "123", "00123", True, "True", None]}, dtype=object)
    path = app.config["SETTINGS"].export_dir / "historical-exact-preview.csv"
    frame.to_csv(path, index=False)
    result = {"path": str(path), "rows": len(frame), "columns": list(frame.columns), "data": frame_records(frame)}
    with app.app_context():
        assert frame_records(read_result_frame(result)) == result["data"]
