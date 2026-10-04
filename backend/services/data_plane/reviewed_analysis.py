"""Fixed, bounded analysis methods executed by a local worker process."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LinearRegression
from sklearn.metrics import r2_score
from sklearn.preprocessing import StandardScaler


METHODS = {
    "describe", "correlation", "grouped_summary", "decile", "ab_test",
    "linear_regression", "kmeans", "anomaly", "trend_forecast",
}
MAX_ROWS = 100_000
MAX_COLUMNS = 500
MAX_CELLS = 2_000_000
MAX_DECODED_BYTES = 512 * 1024 * 1024
MAX_VALUE_BYTES = 4 * 1024 * 1024


def _json_safe(value: Any) -> Any:
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return None
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


def _parquet_safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with stable column names and Arrow-compatible mixed values."""
    result = frame.copy()
    result.columns = [
        "_".join(str(part) for part in column if part is not None and str(part))
        if isinstance(column, tuple) else str(column)
        for column in result.columns
    ]
    if any(not column for column in result.columns):
        raise ValueError("output contains an empty column name")
    if result.columns.has_duplicates:
        raise ValueError("output contains duplicate column names after normalization")
    for column in result.select_dtypes(include=["object", "string"]).columns:
        inferred = pd.api.types.infer_dtype(result[column].dropna(), skipna=True)
        if inferred.startswith("mixed"):
            result[column] = result[column].astype("string")
    return result


def _required_column(frame: pd.DataFrame, value: Any, label: str) -> str:
    column = str(value or "")
    if column not in frame.columns:
        raise ValueError(f"{label} column does not exist")
    return column


def _numeric_columns(frame: pd.DataFrame, value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 50:
        raise ValueError(f"{label} requires 1–50 columns")
    columns = [_required_column(frame, item, label) for item in value]
    if len(set(columns)) != len(columns):
        raise ValueError(f"{label} contains duplicate columns")
    return columns


def _reviewed_method(frame: pd.DataFrame, method: str, params: dict) -> tuple[pd.DataFrame, dict]:
    if method == "describe":
        return frame.describe(include="all").reset_index(), {}
    if method == "correlation":
        numeric = frame.select_dtypes(include="number")
        if numeric.shape[1] < 2:
            raise ValueError("correlation requires two numeric columns")
        return numeric.corr().reset_index(), {}
    if method == "grouped_summary":
        group = _required_column(frame, params.get("group"), "group")
        values = [column for column in frame.select_dtypes(include="number").columns if column != group]
        if not values:
            raise ValueError("grouped_summary requires numeric columns")
        return frame.groupby(group, dropna=False)[values].agg(["count", "mean"]).reset_index(), {}
    if method == "decile":
        column = _required_column(frame, params.get("column"), "decile")
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        if len(values) < 10:
            raise ValueError("decile requires at least ten numeric rows")
        bins = pd.qcut(values, 10, labels=False, duplicates="drop")
        result = pd.DataFrame({"decile": bins + 1, "value": values}).groupby("decile")["value"].agg(
            ["count", "min", "mean", "max"],
        ).reset_index()
        return result, {"actual_groups": len(result)}
    if method == "ab_test":
        group = _required_column(frame, params.get("group"), "group")
        value = _required_column(frame, params.get("value"), "value")
        groups = []
        for label, part in frame.groupby(group, dropna=True):
            numeric = pd.to_numeric(part[value], errors="coerce").dropna()
            if len(numeric) < 2:
                raise ValueError("each A/B group requires at least two numeric rows")
            groups.append((label, numeric))
        if len(groups) != 2:
            raise ValueError("A/B test requires exactly two groups")
        statistic, pvalue = stats.ttest_ind(groups[0][1], groups[1][1], equal_var=False)
        if not np.isfinite(statistic) or not np.isfinite(pvalue):
            raise ValueError("A/B test requires nonconstant groups with measurable variance")
        result = pd.DataFrame([
            {"group": str(label), "count": len(values), "mean": float(values.mean()),
             "std": float(values.std()), "p_value": float(pvalue), "t_statistic": float(statistic)}
            for label, values in groups
        ])
        return result, {"test": "welch_t", "p_value": float(pvalue)}
    if method == "linear_regression":
        features = _numeric_columns(frame, params.get("features"), "features")
        target = _required_column(frame, params.get("target"), "target")
        if target in features:
            raise ValueError("target cannot be a feature")
        prepared = frame[features + [target]].apply(pd.to_numeric, errors="coerce").dropna()
        if len(prepared) <= len(features) + 2:
            raise ValueError("linear regression has too few complete rows")
        model = LinearRegression().fit(prepared[features], prepared[target])
        predicted = model.predict(prepared[features])
        result = pd.DataFrame({"feature": [*features, "intercept"], "coefficient": [
            *[float(value) for value in model.coef_], float(model.intercept_),
        ]})
        return result, {"r2_in_sample": float(r2_score(prepared[target], predicted)), "training_rows": len(prepared)}
    if method == "kmeans":
        features = _numeric_columns(frame, params.get("features"), "features")
        count = int(params.get("clusters") or 3)
        if not 2 <= count <= 20:
            raise ValueError("clusters must be between 2 and 20")
        prepared = frame[features].apply(pd.to_numeric, errors="coerce").dropna()
        if len(prepared) <= count:
            raise ValueError("kmeans has too few complete rows")
        scaled = StandardScaler().fit_transform(prepared)
        model = KMeans(n_clusters=count, random_state=42, n_init=10).fit(scaled)
        result = prepared.assign(cluster=model.labels_).groupby("cluster")[features].mean().reset_index()
        result.insert(1, "count", pd.Series(model.labels_).value_counts().sort_index().values)
        return result, {"inertia": float(model.inertia_), "training_rows": len(prepared)}
    if method == "anomaly":
        features = _numeric_columns(frame, params.get("features"), "features")
        contamination = float(params.get("contamination") or 0.05)
        if not 0 < contamination <= 0.5:
            raise ValueError("contamination must be greater than zero and at most 0.5")
        prepared = frame[features].apply(pd.to_numeric, errors="coerce").dropna()
        if len(prepared) < 20:
            raise ValueError("anomaly detection requires at least twenty complete rows")
        model = IsolationForest(contamination=contamination, random_state=42).fit(prepared)
        scores = model.decision_function(prepared)
        labels = model.predict(prepared)
        result = prepared.assign(anomaly_score=scores, is_anomaly=labels == -1).sort_values(
            "anomaly_score",
        ).head(100).reset_index(drop=True)
        return result, {"anomaly_count": int((labels == -1).sum()), "training_rows": len(prepared)}
    if method == "trend_forecast":
        date = _required_column(frame, params.get("date"), "date")
        value = _required_column(frame, params.get("value"), "value")
        horizon = int(params.get("horizon") or 6)
        if not 1 <= horizon <= 24:
            raise ValueError("horizon must be between 1 and 24")
        prepared = pd.DataFrame({
            "period": pd.to_datetime(frame[date], errors="coerce"),
            "value": pd.to_numeric(frame[value], errors="coerce"),
        }).dropna().sort_values("period")
        if len(prepared) < 6 or prepared["period"].duplicated().any():
            raise ValueError("trend forecast requires six unique dated observations")
        x = np.arange(len(prepared)).reshape(-1, 1)
        model = LinearRegression().fit(x, prepared["value"])
        frequency = pd.infer_freq(prepared["period"]) or "D"
        future = pd.date_range(prepared["period"].iloc[-1], periods=horizon + 1, freq=frequency)[1:]
        result = pd.DataFrame({
            "period": future.astype(str), "forecast": model.predict(
                np.arange(len(prepared), len(prepared) + horizon).reshape(-1, 1),
            ),
        })
        return result, {"method": "linear_trend", "frequency": frequency, "training_rows": len(prepared)}
    raise ValueError("unsupported reviewed method")


def main() -> int:
    if len(sys.argv) != 5:
        raise ValueError("expected input, output, method and parameters")
    source = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2]).resolve()
    method = sys.argv[3]
    params = json.loads(sys.argv[4])
    if method not in METHODS or not isinstance(params, dict):
        raise ValueError("unsupported reviewed method or parameters")
    if not source.is_file() or source.is_symlink() or target.name != "result.parquet":
        raise ValueError("invalid analysis paths")
    if source.suffix == ".parquet":
        frame = pd.read_parquet(source)
    elif source.suffix == ".csv":
        frame = pd.read_csv(source)
    else:
        raise ValueError("unsupported bounded input")
    _validate_frame(frame, "input")
    result, metrics = _reviewed_method(frame, method, params)
    result = _parquet_safe_frame(result)
    _validate_frame(result, "output")
    result.to_parquet(target, index=False)
    (target.parent / "manifest.json").write_text(json.dumps({
        "files": [{"path": target.name}],
        "metrics": _json_safe({
            **metrics, "input_rows": len(frame), "output_rows": len(result), "method": method,
        }),
    }), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
