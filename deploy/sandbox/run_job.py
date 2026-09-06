"""Reviewed bounded analysis entrypoint used only inside the sandbox image."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


METHODS = {"describe", "correlation", "grouped_summary"}
MAX_ROWS = 100_000
MAX_COLUMNS = 500
MAX_CELLS = 2_000_000
MAX_DECODED_BYTES = 512 * 1024 * 1024
MAX_VALUE_BYTES = 4 * 1024 * 1024


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _validate_frame(frame: pd.DataFrame, label: str) -> None:
    if len(frame) > MAX_ROWS or len(frame.columns) > MAX_COLUMNS or frame.size > MAX_CELLS:
        raise ValueError(f"bounded {label} exceeds the row, column, or cell limit")
    if int(frame.memory_usage(index=True, deep=True).sum()) > MAX_DECODED_BYTES:
        raise ValueError(f"bounded {label} exceeds the decoded byte limit")
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        if frame[column].dropna().map(lambda value: len(str(value).encode("utf-8"))).max() > MAX_VALUE_BYTES:
            raise ValueError(f"bounded {label} contains an oversized value")


def main() -> int:
    spec_path = Path(sys.argv[1]).resolve()
    if spec_path.parent != Path("/output") or not spec_path.is_file():
        raise ValueError("JobSpec path is invalid")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    method = str(spec.get("method") or "")
    code = str(spec.get("code") or "")
    if bool(method) == bool(code):
        raise ValueError("provide exactly one reviewed method or Python code")
    if method and method not in METHODS:
        raise ValueError("unsupported reviewed method")
    if len(code.encode("utf-8")) > 64 * 1024:
        raise ValueError("Python code exceeds 64 KiB")
    relative = Path(str(spec.get("input") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("input path is invalid")
    source = (Path("/input") / relative).resolve()
    if Path("/input") not in source.parents:
        raise ValueError("input escaped root")
    if source.suffix == ".parquet":
        frame = pd.read_parquet(source)
    elif source.suffix == ".csv":
        frame = pd.read_csv(source)
    else:
        raise ValueError("unsupported bounded input")
    _validate_frame(frame, "input")
    metrics: dict[str, Any] = {}
    if code:
        namespace: dict[str, Any] = {"df": frame.copy(), "pd": pd, "result": None, "metrics": {}}
        exec(compile(code, "<generated-analysis>", "exec"), namespace, namespace)  # noqa: S102 -- inside OS sandbox
        result = namespace.get("result")
        metrics = namespace.get("metrics") or {}
        if not isinstance(result, pd.DataFrame):
            raise ValueError("generated analysis must assign a pandas DataFrame to result")
        if not isinstance(metrics, dict):
            raise ValueError("generated analysis metrics must be an object")
    elif method == "describe":
        result = frame.describe(include="all").reset_index()
    elif method == "correlation":
        result = frame.select_dtypes(include="number").corr().reset_index()
    else:
        group = str((spec.get("parameters") or {}).get("group") or "")
        if group not in frame.columns:
            raise ValueError("group column does not exist")
        result = frame.groupby(group, dropna=False).agg(["count", "mean"]).reset_index()
        result.columns = ["_".join(str(part) for part in item if part) if isinstance(item, tuple) else str(item) for item in result.columns]
    _validate_frame(result, "output")
    target = Path("/output/result.parquet")
    result.to_parquet(target, index=False)
    Path("/output/manifest.json").write_text(json.dumps({
        "files": [{"path": target.name}],
        "metrics": _json_safe({
            **metrics, "input_rows": len(frame), "output_rows": len(result),
            "method": method or "generated_python",
        }),
    }), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
