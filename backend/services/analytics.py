from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor, IsolationForest, RandomForestClassifier, RandomForestRegressor
from sklearn.feature_selection import f_classif, f_regression, mutual_info_classif, mutual_info_regression
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import accuracy_score, mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor

from ..analysis_modules import registry as analysis_registry
from .datasets import frame_records


ANALYSIS_METHODS = [
    {"id": "profile", "name": "数据质量画像", "group": "exploration"},
    {"id": "correlation", "name": "相关关系分析", "group": "exploration"},
    {"id": "decile", "name": "十分位分层", "group": "segmentation"},
    {"id": "cluster", "name": "K-Means 客群聚类", "group": "segmentation"},
    {"id": "ab_test", "name": "A/B 显著性检验", "group": "inference"},
    {"id": "linear_regression", "name": "线性回归", "group": "modeling"},
    {"id": "logistic_regression", "name": "逻辑回归", "group": "modeling"},
    {"id": "random_forest", "name": "随机森林", "group": "modeling"},
    {"id": "decision_tree", "name": "决策树", "group": "modeling"},
    {"id": "gradient_boosting", "name": "梯度提升模型", "group": "modeling"},
    {"id": "mlp", "name": "多层感知机", "group": "modeling"},
    {"id": "univariate_screening", "name": "单变量筛选", "group": "modeling"},
    {"id": "forecast", "name": "时间序列预测", "group": "forecasting"},
    {"id": "arima", "name": "ARIMA 预测", "group": "forecasting"},
    {"id": "sarima", "name": "季节 ARIMA 预测", "group": "forecasting"},
    {"id": "var", "name": "多变量自回归", "group": "forecasting"},
    {"id": "prophet_like", "name": "趋势与节律预测", "group": "forecasting"},
    {"id": "neural_forecast", "name": "神经网络预测", "group": "forecasting"},
    {"id": "anomaly", "name": "孤立森林异常检测", "group": "quality"},
]

_MODULE_GROUPS = {
    "AB_Test_Analysis": "inference", "Data_Decile_Analysis": "segmentation",
    "Decision_Tree": "modeling", "K_Means": "segmentation",
    "Logistic_Regression": "modeling", "Regression": "modeling",
    "Sklearn_Model": "modeling", "Torch_MLP": "modeling",
    "Univariate_Screening": "modeling", "Time_Series_ARIMA": "forecasting",
    "Time_Series_SARIMA": "forecasting", "Time_Series_VAR": "forecasting",
    "Time_Series_Prophet": "forecasting", "Time_Series_GRU": "forecasting",
}
for _module in analysis_registry.get_all().values():
    ANALYSIS_METHODS.append({
        "id": _module["id"], "name": _module["name"],
        "description": _module.get("desc", ""),
        "required": _module.get("required", []),
        "optional": _module.get("optional", []),
        "output_tables": _module.get("output_tables", []),
        "group": _MODULE_GROUPS.get(_module["id"], "modeling"),
    })


def _safe(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.ndarray):
        return [_safe(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_order(value: Any, default: list[int], maximum: int = 10) -> tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        value = default
    try:
        return tuple(max(0, min(int(item), maximum)) for item in value)
    except (TypeError, ValueError):
        return tuple(default)


def profile(frame: pd.DataFrame) -> dict:
    numeric = frame.select_dtypes(include=np.number)
    columns = []
    for column in frame.columns:
        series = frame[column]
        item = {
            "name": str(column),
            "dtype": str(series.dtype),
            "missing": int(series.isna().sum()),
            "missing_rate": round(float(series.isna().mean()), 6),
            "unique": int(series.nunique(dropna=True)),
            "sample": [str(value)[:120] for value in series.dropna().head(3).tolist()],
        }
        if pd.api.types.is_numeric_dtype(series):
            clean = pd.to_numeric(series, errors="coerce").dropna()
            if len(clean):
                q1, q3 = clean.quantile([0.25, 0.75])
                iqr = q3 - q1
                outliers = clean[(clean < q1 - 1.5 * iqr) | (clean > q3 + 1.5 * iqr)]
                item.update({
                    "min": _safe(clean.min()), "max": _safe(clean.max()),
                    "mean": _safe(clean.mean()), "median": _safe(clean.median()),
                    "std": _safe(clean.std()), "outliers": int(len(outliers)),
                })
        columns.append(item)
    missing_cells = int(frame.isna().sum().sum())
    total_cells = max(1, int(frame.shape[0] * frame.shape[1]))
    duplicate_rows = int(frame.duplicated().sum())
    completeness = max(0.0, 1 - missing_cells / total_cells)
    uniqueness = max(0.0, 1 - duplicate_rows / max(1, len(frame)))
    score = round((completeness * 0.7 + uniqueness * 0.3) * 100, 1)
    return {
        "rows": int(len(frame)),
        "column_count": int(len(frame.columns)),
        "numeric_columns": [str(column) for column in numeric.columns],
        "missing_cells": missing_cells,
        "duplicate_rows": duplicate_rows,
        "quality_score": score,
        "columns": columns,
    }


def clean_frame(frame: pd.DataFrame, operations: list[dict]) -> tuple[pd.DataFrame, list[dict]]:
    output = frame.copy()
    log: list[dict] = []
    for operation in operations:
        name = operation.get("type")
        columns = operation.get("columns") or list(output.columns)
        columns = [column for column in columns if column in output.columns]
        before = len(output)
        if name == "drop_duplicates":
            output = output.drop_duplicates(subset=columns or None)
        elif name == "drop_missing":
            output = output.dropna(subset=columns or None)
        elif name == "fill_missing":
            strategy = operation.get("strategy", "median")
            for column in columns:
                series = output[column]
                if strategy == "mean" and pd.api.types.is_numeric_dtype(series):
                    fill = series.mean()
                elif strategy == "median" and pd.api.types.is_numeric_dtype(series):
                    fill = series.median()
                elif strategy == "mode":
                    modes = series.mode(dropna=True)
                    fill = modes.iloc[0] if len(modes) else operation.get("value", "")
                else:
                    fill = operation.get("value", 0 if pd.api.types.is_numeric_dtype(series) else "未知")
                output[column] = series.fillna(fill)
        elif name == "trim_text":
            for column in columns:
                if output[column].dtype == object:
                    output[column] = output[column].map(lambda value: value.strip() if isinstance(value, str) else value)
        elif name == "winsorize":
            lower = float(operation.get("lower", 0.01))
            upper = float(operation.get("upper", 0.99))
            for column in columns:
                if pd.api.types.is_numeric_dtype(output[column]):
                    bounds = output[column].quantile([lower, upper])
                    output[column] = output[column].clip(bounds.iloc[0], bounds.iloc[1])
        elif name == "cast":
            target = operation.get("target", "string")
            for column in columns:
                if target == "number":
                    output[column] = pd.to_numeric(output[column], errors="coerce")
                elif target == "datetime":
                    output[column] = pd.to_datetime(output[column], errors="coerce")
                else:
                    output[column] = output[column].astype("string")
        elif name == "rename":
            output = output.rename(columns=operation.get("mapping", {}))
        else:
            raise ValueError(f"未知清洗操作：{name}")
        log.append({"operation": name, "rows_before": before, "rows_after": len(output)})
    return output, log


def _numeric(frame: pd.DataFrame, requested: list[str] | None = None) -> pd.DataFrame:
    columns = requested or list(frame.select_dtypes(include=np.number).columns)
    valid = [column for column in columns if column in frame.columns]
    result = frame[valid].apply(pd.to_numeric, errors="coerce").dropna()
    if result.empty:
        raise ValueError("没有足够的数值数据执行该分析")
    return result


def _module_params(method: str, params: dict) -> tuple[str, str | None, int, dict]:
    target = str(
        params.get("target_column") or params.get("target") or params.get("value_column") or "",
    ).strip()
    groupby = params.get("groupby_column")
    if groupby is None:
        groupby = params.get("group") or params.get("date_column")
    if method == "Time_Series_VAR" and params.get("value_columns"):
        values = [str(value) for value in params["value_columns"]]
        groupby = ",".join(values)
    count = params.get("n_deciles")
    if count is None:
        if method == "K_Means":
            count = params.get("clusters", 3)
        elif method.startswith("Time_Series_"):
            count = params.get("horizon", 0)
        elif method == "Decision_Tree":
            count = params.get("max_depth", 0)
        elif method == "Logistic_Regression":
            count = params.get("max_iter", 0)
        elif method == "Regression":
            count = params.get("degree", 0)
        else:
            count = 0
    maximum = 0
    if method == "K_Means":
        maximum = 12
    elif method.startswith("Time_Series_"):
        maximum = 120
    elif method == "Decision_Tree":
        maximum = 50
    elif method == "Logistic_Regression":
        maximum = 5000
    elif method == "Regression":
        maximum = 10
    elif count:
        maximum = 100
    count = _bounded_int(count, 0, 0, maximum) if maximum else 0
    return target, str(groupby) if groupby is not None else None, count, params.get("analysis_options") or {}


def _run_registered_analysis(
    frame: pd.DataFrame, method: str, params: dict, progress_callback=None,
) -> tuple[dict, dict[str, pd.DataFrame]]:
    entry = analysis_registry.get(method)
    run = entry.get("run")
    if not run:
        raise ValueError(f"分析模块 {method} 加载失败")
    target, groupby, count, analysis_options = _module_params(method, params)
    kwargs = {
        "df": frame, "target_column": target, "groupby_column": groupby, "n_deciles": count,
    }
    if method == "AB_Test_Analysis":
        kwargs["analysis_options"] = analysis_options
    if progress_callback is not None and (method.startswith("Time_Series_") or method == "Torch_MLP"):
        kwargs["progress_callback"] = progress_callback
    returned = run(**kwargs)
    markdown = ""
    frames: dict[str, pd.DataFrame]
    if isinstance(returned, dict):
        frames = {str(name): value for name, value in returned.items() if isinstance(value, pd.DataFrame)}
    elif isinstance(returned, tuple):
        values = list(returned)
        if values and isinstance(values[-1], str):
            markdown = values.pop()
        names = list(entry.get("output_tables") or [])
        frames = {
            names[index] if index < len(names) else f"analysis_output_{index + 1}": value
            for index, value in enumerate(values) if isinstance(value, pd.DataFrame)
        }
    else:
        raise ValueError(f"分析模块 {method} 返回了无效结果")
    tables = {
        name: {
            "rows": int(len(value)), "columns": [str(column) for column in value.columns],
            "data": frame_records(value, 2000),
        }
        for name, value in frames.items()
    }
    result = {
        "analysis_id": method, "name": entry.get("name", method),
        "summary": markdown, "output_tables": list(frames), "tables": tables,
    }
    return {"method": method, "result": result}, frames


def run_analysis_with_frames(
    frame: pd.DataFrame, method: str, params: dict | None = None, progress_callback=None,
) -> tuple[dict, dict[str, pd.DataFrame]]:
    if method in _MODULE_GROUPS:
        return _run_registered_analysis(frame, method, params or {}, progress_callback)
    return run_analysis(frame, method, params), {}


def run_analysis(frame: pd.DataFrame, method: str, params: dict | None = None) -> dict:
    params = params or {}
    if method in _MODULE_GROUPS:
        return _run_registered_analysis(frame, method, params)[0]
    if method == "profile":
        return {"method": method, "result": profile(frame)}
    if method == "correlation":
        numeric = _numeric(frame, params.get("columns"))
        return {"method": method, "result": {"matrix": _safe(numeric.corr(method=params.get("coefficient", "pearson")).round(6).to_dict())}}
    if method == "decile":
        column = params.get("column")
        if column not in frame.columns:
            raise ValueError("请选择用于分层的字段")
        data = frame[[column]].dropna().copy()
        bins = min(int(params.get("bins", 10)), int(data[column].nunique()))
        data["segment"] = pd.qcut(data[column], bins, labels=False, duplicates="drop") + 1
        summary = data.groupby("segment")[column].agg(["count", "min", "max", "mean"]).reset_index()
        return {"method": method, "result": {"column": column, "segments": frame_records(summary, 20)}}
    if method == "cluster":
        numeric = _numeric(frame, params.get("features"))
        k = max(2, min(int(params.get("clusters", 3)), 12, len(numeric)))
        scaled = StandardScaler().fit_transform(numeric)
        model = KMeans(n_clusters=k, random_state=42, n_init=10).fit(scaled)
        labeled = numeric.copy()
        labeled["cluster"] = model.labels_.astype(int)
        centers = pd.DataFrame(StandardScaler().fit(numeric).inverse_transform(model.cluster_centers_), columns=numeric.columns)
        return {"method": method, "result": {"clusters": k, "centers": frame_records(centers, k), "assignments": frame_records(labeled, 300), "inertia": _safe(model.inertia_)}}
    if method == "ab_test":
        group, metric = params.get("group"), params.get("metric")
        if group not in frame.columns or metric not in frame.columns:
            raise ValueError("请选择分组字段和指标字段")
        groups = [(str(name), pd.to_numeric(part[metric], errors="coerce").dropna()) for name, part in frame.groupby(group)]
        if len(groups) != 2:
            raise ValueError("A/B 检验需要且仅需要两个分组")
        statistic, pvalue = stats.ttest_ind(groups[0][1], groups[1][1], equal_var=False)
        return {"method": method, "result": {"groups": [{"name": name, "count": len(values), "mean": _safe(values.mean())} for name, values in groups], "statistic": _safe(statistic), "p_value": _safe(pvalue), "significant": bool(pvalue < float(params.get("alpha", 0.05)))}}
    if method == "univariate_screening":
        target = params.get("target")
        if target not in frame.columns:
            raise ValueError("请选择目标字段")
        features = params.get("features") or [column for column in frame.select_dtypes(include=np.number).columns if column != target]
        working = frame[features + [target]].dropna()
        x = working[features].apply(pd.to_numeric, errors="coerce").fillna(0)
        y = working[target]
        classification = not pd.api.types.is_numeric_dtype(y) or y.nunique() <= 10
        if classification:
            y = pd.factorize(y)[0]
            scores, p_values = f_classif(x, y)
            mutual = mutual_info_classif(x, y, random_state=42)
        else:
            y = pd.to_numeric(y, errors="coerce")
            scores, p_values = f_regression(x, y)
            mutual = mutual_info_regression(x, y, random_state=42)
        rows = sorted(
            ({"feature": str(name), "score": _safe(scores[index]), "p_value": _safe(p_values[index]), "mutual_information": _safe(mutual[index])} for index, name in enumerate(x.columns)),
            key=lambda item: item["mutual_information"] or 0,
            reverse=True,
        )
        return {"method": method, "result": {"classification": classification, "features": rows}}
    if method in {"linear_regression", "logistic_regression", "random_forest", "decision_tree", "gradient_boosting", "mlp"}:
        target = params.get("target")
        features = params.get("features") or [column for column in frame.select_dtypes(include=np.number).columns if column != target]
        if target not in frame.columns or not features:
            raise ValueError("请选择目标字段和特征字段")
        working = frame[features + [target]].dropna()
        x = pd.get_dummies(working[features], drop_first=True)
        y = working[target]
        if len(working) < 10:
            raise ValueError("建模至少需要 10 条完整记录")
        classification = method == "logistic_regression" or (method in {"random_forest", "decision_tree", "gradient_boosting", "mlp"} and (not pd.api.types.is_numeric_dtype(y) or y.nunique() <= 10))
        stratify = y if classification and y.value_counts().min() >= 2 else None
        x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.25, random_state=42, stratify=stratify)
        if method == "linear_regression":
            model = LinearRegression()
        elif method == "logistic_regression":
            model = LogisticRegression(max_iter=2000)
        elif method == "random_forest" and classification:
            model = RandomForestClassifier(n_estimators=160, random_state=42)
        elif method == "random_forest":
            model = RandomForestRegressor(n_estimators=160, random_state=42)
        elif method == "decision_tree" and classification:
            model = DecisionTreeClassifier(max_depth=_bounded_int(params.get("max_depth"), 5, 1, 50), random_state=42)
        elif method == "decision_tree":
            model = DecisionTreeRegressor(max_depth=_bounded_int(params.get("max_depth"), 5, 1, 50), random_state=42)
        elif method == "gradient_boosting" and classification:
            model = GradientBoostingClassifier(random_state=42)
        elif method == "gradient_boosting":
            model = GradientBoostingRegressor(random_state=42)
        elif method == "mlp" and classification:
            model = MLPClassifier(hidden_layer_sizes=(64, 32), max_iter=_bounded_int(params.get("max_iter"), 500, 10, 2000), random_state=42)
        else:
            model = MLPRegressor(hidden_layer_sizes=(64, 32), max_iter=_bounded_int(params.get("max_iter"), 500, 10, 2000), random_state=42)
        model.fit(x_train, y_train)
        prediction = model.predict(x_test)
        metrics = {"accuracy": _safe(accuracy_score(y_test, prediction))} if classification else {"r2": _safe(r2_score(y_test, prediction)), "mae": _safe(mean_absolute_error(y_test, prediction))}
        importance = getattr(model, "feature_importances_", getattr(model, "coef_", np.zeros(len(x.columns))))
        if np.asarray(importance).ndim > 1:
            importance = np.mean(np.abs(importance), axis=0)
        ranked = sorted(zip(x.columns, np.asarray(importance).reshape(-1)), key=lambda item: abs(item[1]), reverse=True)
        return {"method": method, "result": {"metrics": metrics, "features": [{"name": str(name), "importance": _safe(value)} for name, value in ranked], "test_rows": len(x_test)}}
    if method == "forecast":
        date_column, value_column = params.get("date_column"), params.get("value_column")
        if date_column not in frame.columns or value_column not in frame.columns:
            raise ValueError("请选择时间字段和数值字段")
        horizon = max(1, min(int(params.get("horizon", 12)), 120))
        data = frame[[date_column, value_column]].copy()
        data[date_column] = pd.to_datetime(data[date_column], errors="coerce")
        data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
        data = data.dropna().sort_values(date_column)
        if len(data) < 3:
            raise ValueError("预测至少需要 3 个有效时间点")
        x = np.arange(len(data)).reshape(-1, 1)
        model = LinearRegression().fit(x, data[value_column])
        future_x = np.arange(len(data), len(data) + horizon).reshape(-1, 1)
        inferred = pd.infer_freq(data[date_column]) or params.get("frequency", "D")
        dates = pd.date_range(data[date_column].iloc[-1], periods=horizon + 1, freq=inferred)[1:]
        forecast = pd.DataFrame({date_column: dates, "forecast": model.predict(future_x)})
        return {"method": method, "result": {"history": frame_records(data.tail(200), 200), "forecast": frame_records(forecast, horizon), "trend_per_period": _safe(model.coef_[0])}}
    if method in {"arima", "sarima", "prophet_like", "neural_forecast"}:
        date_column, value_column = params.get("date_column"), params.get("value_column")
        if date_column not in frame.columns or value_column not in frame.columns:
            raise ValueError("请选择时间字段和数值字段")
        horizon = max(1, min(int(params.get("horizon", 12)), 120))
        data = frame[[date_column, value_column]].copy()
        data[date_column] = pd.to_datetime(data[date_column], errors="coerce")
        data[value_column] = pd.to_numeric(data[value_column], errors="coerce")
        data = data.dropna().sort_values(date_column).drop_duplicates(date_column)
        if len(data) < 8:
            raise ValueError("该预测方法至少需要 8 个有效时间点")
        frequency = pd.infer_freq(data[date_column]) or params.get("frequency", "D")
        future_dates = pd.date_range(data[date_column].iloc[-1], periods=horizon + 1, freq=frequency)[1:]
        values = data[value_column].to_numpy(dtype=float)
        if method in {"arima", "sarima"}:
            try:
                from statsmodels.tsa.statespace.sarimax import SARIMAX
            except ImportError as exc:
                raise ValueError("请安装 statsmodels 后使用 ARIMA 系列模型") from exc
            order = _bounded_order(params.get("order"), [1, 1, 1], 10)
            if method == "sarima":
                raw_seasonal = params.get("seasonal_order", [1, 0, 1, params.get("season_length", 12)])
                if not isinstance(raw_seasonal, (list, tuple)) or len(raw_seasonal) != 4:
                    raw_seasonal = [1, 0, 1, 12]
                seasonal_order = (
                    _bounded_int(raw_seasonal[0], 1, 0, 5),
                    _bounded_int(raw_seasonal[1], 0, 0, 2),
                    _bounded_int(raw_seasonal[2], 1, 0, 5),
                    _bounded_int(raw_seasonal[3], 12, 2, 365),
                )
            else:
                seasonal_order = (0, 0, 0, 0)
            fitted = SARIMAX(values, order=order, seasonal_order=seasonal_order, enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
            predicted = fitted.get_forecast(horizon)
            forecasts = predicted.predicted_mean
            alpha = max(0.001, min(float(params.get("alpha", 0.05)), 0.5))
            confidence = predicted.conf_int(alpha=alpha)
            result_frame = pd.DataFrame({date_column: future_dates, "forecast": forecasts, "lower": confidence[:, 0], "upper": confidence[:, 1]})
            diagnostics = {"aic": _safe(fitted.aic), "bic": _safe(fitted.bic)}
        elif method == "prophet_like":
            x = np.arange(len(values), dtype=float)
            future_x = np.arange(len(values), len(values) + horizon, dtype=float)
            period = _bounded_int(params.get("season_length"), min(12, max(2, len(values) // 3)), 2, 365)
            order = max(1, min(int(params.get("fourier_order", 3)), 8))
            def design(points):
                features = [np.ones_like(points), points]
                for harmonic in range(1, order + 1):
                    features.extend([np.sin(2 * np.pi * harmonic * points / period), np.cos(2 * np.pi * harmonic * points / period)])
                return np.column_stack(features)
            coefficients = np.linalg.lstsq(design(x), values, rcond=None)[0]
            forecasts = design(future_x) @ coefficients
            residual = values - design(x) @ coefficients
            band = 1.96 * np.std(residual)
            result_frame = pd.DataFrame({date_column: future_dates, "forecast": forecasts, "lower": forecasts - band, "upper": forecasts + band})
            diagnostics = {"season_length": period, "fourier_order": order, "residual_std": _safe(np.std(residual))}
        else:
            lookback = max(2, min(int(params.get("lookback", 6)), len(values) // 2))
            x_train = np.asarray([values[index - lookback:index] for index in range(lookback, len(values))])
            y_train = values[lookback:]
            model = MLPRegressor(hidden_layer_sizes=(48, 24), max_iter=_bounded_int(params.get("max_iter"), 800, 10, 2000), random_state=42)
            model.fit(x_train, y_train)
            window = list(values[-lookback:])
            forecasts = []
            for _ in range(horizon):
                prediction = float(model.predict(np.asarray(window[-lookback:]).reshape(1, -1))[0])
                forecasts.append(prediction)
                window.append(prediction)
            residual = y_train - model.predict(x_train)
            band = 1.96 * np.std(residual)
            forecasts = np.asarray(forecasts)
            result_frame = pd.DataFrame({date_column: future_dates, "forecast": forecasts, "lower": forecasts - band, "upper": forecasts + band})
            diagnostics = {"lookback": lookback, "training_mae": _safe(np.mean(np.abs(residual)))}
        return {"method": method, "result": {"history": frame_records(data.tail(300), 300), "forecast": frame_records(result_frame, horizon), "diagnostics": diagnostics}}
    if method == "var":
        date_column = params.get("date_column")
        value_columns = params.get("value_columns") or list(frame.select_dtypes(include=np.number).columns)[:4]
        if date_column not in frame.columns or len(value_columns) < 2:
            raise ValueError("VAR 需要时间字段和至少两个数值字段")
        data = frame[[date_column] + value_columns].copy()
        data[date_column] = pd.to_datetime(data[date_column], errors="coerce")
        data[value_columns] = data[value_columns].apply(pd.to_numeric, errors="coerce")
        data = data.dropna().sort_values(date_column)
        if len(data) < 12:
            raise ValueError("VAR 至少需要 12 个完整时间点")
        try:
            from statsmodels.tsa.api import VAR
        except ImportError as exc:
            raise ValueError("请安装 statsmodels 后使用 VAR 模型") from exc
        maxlags = max(1, min(int(params.get("lags", 2)), len(data) // 4))
        fitted = VAR(data[value_columns]).fit(maxlags=maxlags)
        horizon = max(1, min(int(params.get("horizon", 12)), 120))
        forecast_values = fitted.forecast(data[value_columns].values[-fitted.k_ar:], steps=horizon)
        frequency = pd.infer_freq(data[date_column]) or params.get("frequency", "D")
        future_dates = pd.date_range(data[date_column].iloc[-1], periods=horizon + 1, freq=frequency)[1:]
        forecast = pd.DataFrame(forecast_values, columns=value_columns)
        forecast.insert(0, date_column, future_dates)
        return {"method": method, "result": {"history": frame_records(data.tail(300), 300), "forecast": frame_records(forecast, horizon), "diagnostics": {"lags": fitted.k_ar, "aic": _safe(fitted.aic)}}}
    if method == "anomaly":
        numeric = _numeric(frame, params.get("features"))
        contamination = max(0.001, min(float(params.get("contamination", 0.05)), 0.5))
        model = IsolationForest(contamination=contamination, random_state=42)
        labels = model.fit_predict(StandardScaler().fit_transform(numeric))
        scores = -model.score_samples(StandardScaler().fit_transform(numeric))
        result = numeric.copy()
        result["anomaly"] = labels == -1
        result["anomaly_score"] = scores
        return {"method": method, "result": {"anomaly_count": int((labels == -1).sum()), "rows": frame_records(result.sort_values("anomaly_score", ascending=False), 300)}}
    raise ValueError(f"未知分析方法：{method}")
