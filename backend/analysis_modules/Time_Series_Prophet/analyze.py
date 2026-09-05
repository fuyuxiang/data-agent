#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Time_Series_Prophet
===================
轻量级 Prophet 风格时间序列预测模块（纯 numpy + pandas，无需安装 prophet 包）。

实现原理（与 Facebook Prophet 一致的加法分解框架）：
  y(t) = trend(t) + seasonality(t) + noise

  趋势（Trend）：
    - 检测变点（changepoints）：等间距采样候选点，岭回归（L2 正则）拟合分段线性趋势
    - 使用 numpy 从零实现，无依赖

  季节性（Seasonality）：
    - 傅里叶级数逼近年度 / 周度 / 日内季节性
    - 阶数 N 越大拟合越精细（默认年度 N=8，周度 N=3，日内 N=4）

  预测：
    - 用历史拟合的趋势斜率外推
    - 季节性在未来时间点展开傅里叶项

  不确定性：
    - 用残差标准差 × z 分位数生成近似置信区间

输出三张结果表：
    analysis_result    — 预测 + 历史拟合（ds / y_actual / y_pred / lower_ci / upper_ci / segment）
    analysis_breakdown — 趋势 & 季节性分解（ds / trend / yearly / weekly / daily / residual）
    analysis_metrics   — 模型评估（metric / value）
"""

import numpy as np
import pandas as pd
from typing import Callable, Optional, Tuple, List

# ── 模块元数据 ──────────────────────────────────────────────────────────────
ANALYSIS_ID   = "Time_Series_Prophet"
ANALYSIS_NAME = "Prophet 风格时间序列预测"
ANALYSIS_DESC = (
    "使用 Prophet 加法分解框架（趋势 + 季节性 + 残差）预测时间序列，"
    "纯 numpy+pandas 实现，无需安装 prophet/fbprophet 包。"
    "自动识别年度 / 周度 / 日内季节性，分段线性趋势建模。"
    "通过 groupby_column 指定时间列名；"
    "通过 n_deciles 指定预测步数（默认 30）。"
)
REQUIRED_PARAMS = ["target_column"]
OPTIONAL_PARAMS = [
    "groupby_column (时间列名，默认自动探测)",
    "n_deciles (预测步数，默认 30)",
]
OUTPUT_TABLES = ["analysis_result", "analysis_breakdown", "analysis_metrics"]

_DEFAULT_STEPS    = 30
_CHANGEPOINT_RANGE = 0.8    # 仅在历史数据前 80% 放置变点

# 大数据降采样配置
_RESAMPLE_THRESHOLD = 2000  # 超过此点数触发降采样
_RESAMPLE_FREQ      = "D"   # 降采样目标频率（日）
_RESAMPLE_AGG       = "mean"


# ═══════════════════════════════════════════════════════════════════════════
#  1. 工具函数
# ═══════════════════════════════════════════════════════════════════════════
def _auto_n_changepoints(n: int) -> int:
    if n < 60:
        return max(1, n // 10)
    if n < 365:
        return min(10, n // 20)
    if n < 1000:
        return min(15, n // 30)
    return min(25, n // 50)

def _detect_time_col(df: pd.DataFrame, hint: str = "") -> str:
    if hint and hint in df.columns:
        return hint
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            return col
    keywords = ["date", "time", "month", "year", "week", "day", "period", "ds", "日期", "时间"]
    for col in df.columns:
        if any(k in col.lower() for k in keywords):
            try:
                pd.to_datetime(df[col])
                return col
            except Exception:
                pass
    return ""


def _prepare_series(df: pd.DataFrame, time_col: str, value_col: str) -> Tuple[pd.Series, float]:
    """
    返回 (series_with_datetime_index, day_scale)
    day_scale = 数据相邻点的中位天数间距（用于季节性判断 & 预测时间步长）
    """
    work = df[[time_col, value_col]].copy() if time_col else df[[value_col]].copy()
    if time_col:
        work[time_col] = pd.to_datetime(work[time_col])
        work = work.sort_values(time_col).drop_duplicates(time_col)
        work = work.set_index(time_col)
    series = work[value_col].astype(float).dropna()

    if time_col and len(series) >= 2:
        deltas = pd.Series(series.index).diff().dt.total_seconds().dropna() / 86400
        day_scale = float(deltas.median())
        # 防御:中位数为 0 或负(异常数据)时退回 1 天
        if not np.isfinite(day_scale) or day_scale <= 0:
            day_scale = 1.0
    else:
        day_scale = 1.0
    return series, day_scale


def _resample_to_regular(series: pd.Series, freq: str, agg: str) -> pd.Series:
    """
    降采样到规则频率,保证 freq 属性合法。
    与 SARIMA 模块同款实现:不能简单 dropna,否则有缺口的数据会丢失 freq。
    """
    resampled = getattr(series.resample(freq), agg)()
    resampled = resampled.interpolate(method="time")
    resampled = resampled.ffill().bfill()
    resampled = resampled.asfreq(freq)
    return resampled

def _select_best_resample(series: pd.Series, freqs: List[str], steps: int) -> Tuple[pd.Series, str]:
    """
    在候选频率中选回测RMSE最低者；失败则返回原序列。
    """
    best_score = np.inf
    best_series = series
    best_freq = "original"

    for f in freqs:
        try:
            s = _resample_to_regular(series, f, _RESAMPLE_AGG)
            if len(s) < 80:
                continue

            # day_scale 按频率估算
            if f.upper().endswith("H"):
                h = int(f[:-1]) if f[:-1].isdigit() else 1
                ds = h / 24.0
            elif f.upper().endswith("D"):
                d = int(f[:-1]) if f[:-1].isdigit() else 1
                ds = float(d)
            else:
                ds = 1.0

            bt = _rolling_backtest(s, ds, n_folds=2, horizon=min(steps, max(5, len(s)//10)))
            score = bt["val_rmse"] if bt.get("ok", False) and np.isfinite(bt.get("val_rmse", np.nan)) else np.inf
            if score < best_score:
                best_score, best_series, best_freq = score, s, f
        except Exception:
            continue

    return best_series, best_freq

def _to_t(index) -> Tuple[np.ndarray, float]:
    """
    将 DatetimeIndex 转为归一化时间 t（0..1 对应历史范围）。
    Returns (normalized_t, t_max_days)
    """
    if pd.api.types.is_datetime64_any_dtype(index):
        # 用 total_seconds 而非 asi8(后者在新 pandas 有 FutureWarning)
        t_raw = (index - index[0]).total_seconds().values.astype(float) / 86400
    else:
        t_raw = np.arange(len(index), dtype=float)
    t_max = t_raw[-1] if t_raw[-1] > 0 else 1.0
    return t_raw / t_max, t_max

def _auto_trend_lambda(y: np.ndarray) -> float:
    scale = np.std(y)
    if not np.isfinite(scale) or scale <= 0:
        scale = 1.0
    return 0.2 * scale

def _auto_fourier_orders(t_max_days: float, day_scale: float):
    use_daily = day_scale < 0.5
    use_weekly = day_scale <= 4
    use_yearly = t_max_days >= 365

    daily_order = 4 if use_daily else 0
    weekly_order = 3 if use_weekly else 0

    if t_max_days < 365:
        yearly_order = 0
    elif t_max_days < 730:
        yearly_order = 4
    elif t_max_days < 1095:
        yearly_order = 6
    else:
        yearly_order = 8

    return yearly_order, weekly_order, daily_order

def _ridge_solve(X: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    reg = lam * np.eye(X.shape[1])
    try:
        return np.linalg.solve(X.T @ X + reg, X.T @ y)
    except np.linalg.LinAlgError:
        return np.linalg.lstsq(X.T @ X + reg, X.T @ y, rcond=None)[0]

def _forecast_sigma(residuals: np.ndarray, steps: int) -> np.ndarray:
    residuals = np.asarray(residuals, dtype=float)
    global_sigma = np.nanstd(residuals)
    if not np.isfinite(global_sigma) or global_sigma <= 0:
        global_sigma = 1.0

    window = min(60, max(14, len(residuals) // 5))
    recent_sigma = np.nanstd(residuals[-window:])
    if not np.isfinite(recent_sigma) or recent_sigma <= 0:
        recent_sigma = global_sigma

    horizon_growth = np.sqrt(np.arange(1, steps + 1) / steps)
    sigma = 0.5 * global_sigma + 0.5 * recent_sigma
    return sigma * (1.0 + 0.25 * horizon_growth)
# ═══════════════════════════════════════════════════════════════════════════
#  2. 傅里叶季节性
# ═══════════════════════════════════════════════════════════════════════════

def _fourier_features(t_days: np.ndarray, period_days: float, order: int) -> np.ndarray:
    """
    生成傅里叶季节性特征矩阵,shape (n, 2*order)。
    t_days: 原始天数(非归一化)
    """
    cols = []
    for k in range(1, order + 1):
        cols.append(np.sin(2 * np.pi * k * t_days / period_days))
        cols.append(np.cos(2 * np.pi * k * t_days / period_days))
    return np.column_stack(cols)


# ═══════════════════════════════════════════════════════════════════════════
#  3. 分段线性趋势
# ═══════════════════════════════════════════════════════════════════════════

def _changepoint_matrix(t: np.ndarray, cp_t: np.ndarray) -> np.ndarray:
    """变点指示矩阵 A,shape (n, n_cp);A[i,j] = 1 if t[i] >= cp_t[j] else 0。"""
    return (t[:, None] >= cp_t[None, :]).astype(float)


def _fit_trend(
    t: np.ndarray,
    y: np.ndarray,
    n_changepoints: int,
    cp_range: float,
    lam: float,
) -> Tuple[np.ndarray, np.ndarray, float, float]:
    """
    # 使用 numpy 从零实现的岭回归（L2 正则）
    Returns
    -------
    cp_t    : 变点的 t 位置
    deltas  : 各变点处的斜率变化量
    k0      : 初始斜率
    m0      : 截距
    """
    n = len(t)
    cp_indices = np.linspace(0, int(cp_range * n) - 1, n_changepoints, dtype=int)
    cp_t = t[cp_indices]

    A = _changepoint_matrix(t, cp_t)
    A_scaled = A * (t[:, None] - cp_t[None, :])
    X = np.column_stack([np.ones(n), t, A_scaled])

    reg = lam * np.eye(X.shape[1])
    reg[0, 0] = 0.0   # 截距不正则化
    reg[1, 1] = 0.0   # 基础斜率不正则化
    try:
        w = np.linalg.solve(X.T @ X + reg, X.T @ y)
    except np.linalg.LinAlgError:
        w = np.linalg.lstsq(X.T @ X + reg, X.T @ y, rcond=None)[0]

    return cp_t, w[2:], float(w[1]), float(w[0])


def _predict_trend(
    t: np.ndarray,
    cp_t: np.ndarray,
    deltas: np.ndarray,
    k0: float,
    m0: float,
) -> np.ndarray:
    A = _changepoint_matrix(t, cp_t)
    A_scaled = A * (t[:, None] - cp_t[None, :])
    return m0 + k0 * t + A_scaled @ deltas


# ═══════════════════════════════════════════════════════════════════════════
#  4. 完整拟合流程
# ═══════════════════════════════════════════════════════════════════════════
def _fit_prophet(
    series: pd.Series,
    day_scale: float,
    yearly_order: int,
    weekly_order: int,
    daily_order: int,
) -> dict:
    """
    Returns 拟合参数字典,包含 trend 参数 + 季节性系数。
    根据数据粒度自动启用:年度 / 周度 / 日内 季节性。
    """
    y = series.values.astype(float)
    n = len(y)
    t_norm, t_max_days = _to_t(series.index)
    t_days = t_norm * t_max_days

    # 趋势拟合
    n_cp = _auto_n_changepoints(n)
    lam = _auto_trend_lambda(y)
    cp_t, deltas, k0, m0 = _fit_trend(t_norm, y, n_cp, _CHANGEPOINT_RANGE, lam)
    trend_hat = _predict_trend(t_norm, cp_t, deltas, k0, m0)

    # 季节性 detrend
    y_detrend = y - trend_hat

    # 根据数据粒度决定启用哪些季节性
    use_daily  = day_scale < 0.5 and daily_order > 0     # 小时级:启用日内(24h)
    use_weekly = day_scale <= 4 and weekly_order > 0     # 日/小时级:启用周
    use_yearly = t_max_days >= 180 and yearly_order > 0  # 跨度 >=6个月:启用年度

    feat_parts: List[Tuple[str, np.ndarray, int]] = []

    if use_daily:
        feat_parts.append(("daily", _fourier_features(t_days, 1.0, daily_order), daily_order))
    if use_weekly:
        feat_parts.append(("weekly", _fourier_features(t_days, 7.0, weekly_order), weekly_order))
    if use_yearly:
        feat_parts.append(("yearly", _fourier_features(t_days, 365.25, yearly_order), yearly_order))

    if feat_parts:
        X_seas = np.hstack([p[1] for p in feat_parts])
        season_lam = 0.1 * np.std(y_detrend)
        if not np.isfinite(season_lam) or season_lam <= 0:
            season_lam = 1.0
        beta = _ridge_solve(X_seas, y_detrend, season_lam)
        seas_hat = X_seas @ beta
    else:
        beta = np.array([])
        seas_hat = np.zeros(n)

    residuals = y - trend_hat - seas_hat

    # 分拆各季节性成分(用于 breakdown 表)
    component_hats = {}
    offset = 0
    for label, F, order in feat_parts:
        ncols = order * 2
        component_hats[label] = F @ beta[offset:offset + ncols]
        offset += ncols

    return {
        "cp_t":         cp_t,
        "deltas":       deltas,
        "k0":           k0,
        "m0":           m0,
        "t_max_days":   t_max_days,
        "beta":         beta,
        "feat_parts":   feat_parts,
        "use_yearly":   use_yearly,
        "use_weekly":   use_weekly,
        "use_daily":    use_daily,
        "yearly_order": yearly_order,
        "weekly_order": weekly_order,
        "daily_order":  daily_order,
        "trend_hat":    trend_hat,
        "seas_hat":     seas_hat,
        "yearly_hat":   component_hats.get("yearly", np.zeros(n)),
        "weekly_hat":   component_hats.get("weekly", np.zeros(n)),
        "daily_hat":    component_hats.get("daily",  np.zeros(n)),
        "residuals":    residuals,
        "t_norm":       t_norm,
        "t_days":       t_days,
        "day_scale":    day_scale,
    }


def _forecast_prophet(
    params: dict,
    steps: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    生成未来 steps 步预测。
    Returns (future_days, trend_future, seas_future, y_pred_future)

    关键修复:dt 直接用 day_scale(中位步长)而非 总跨度/点数。
    后者在有缺口的数据上会产生偏移,导致季节性相位错乱。
    """
    t_max_days = params["t_max_days"]
    day_scale  = params["day_scale"]
    last_t_days = params["t_days"][-1]

    # ✅ 用中位步长,与 _build_future_index 完全一致
    dt = day_scale
    future_days  = last_t_days + np.arange(1, steps + 1) * dt
    future_t_norm = future_days / t_max_days   # 归一化(可能 > 1,趋势外推)

    # 未来趋势
    trend_future = _predict_trend(
        future_t_norm, params["cp_t"], params["deltas"], params["k0"], params["m0"]
    )

    # 未来季节性:按 feat_parts 顺序重建,与拟合时完全一致
    seas_future = np.zeros(steps)
    beta = params["beta"]
    offset = 0
    for label, _F_hist, order in params["feat_parts"]:
        ncols = order * 2
        period = {"daily": 1.0, "weekly": 7.0, "yearly": 365.25}[label]
        F_fut = _fourier_features(future_days, period, order)
        seas_future += F_fut @ beta[offset:offset + ncols]
        offset += ncols

    y_pred = trend_future + seas_future
    return future_days, trend_future, seas_future, y_pred


# ═══════════════════════════════════════════════════════════════════════════
#  5. 结果表构建
# ═══════════════════════════════════════════════════════════════════════════

def _build_future_index(series: pd.Series, steps: int, day_scale: float):
    """
    生成未来 DatetimeIndex。优先级:
      1) series.index.freq (存在且有效)
      2) Timedelta = day_scale 天 (使用纳秒精度,兼容亚秒级数据)
      3) median diff 兜底(基本不会触发)
      4) RangeIndex(最后兜底)
    """
    # 1) freq
    if hasattr(series.index, "freq") and series.index.freq is not None:
        try:
            return pd.date_range(series.index[-1], periods=steps + 1, freq=series.index.freq)[1:]
        except Exception:
            pass

    # 2) day_scale → Timedelta (纳秒精度,亚秒数据也能用)
    if pd.api.types.is_datetime64_any_dtype(series.index):
        try:
            ns = max(int(round(day_scale * 86400 * 1e9)), 1)  # 至少 1ns,避免 0
            delta = pd.Timedelta(nanoseconds=ns)
            if delta > pd.Timedelta(0):
                return pd.date_range(series.index[-1] + delta, periods=steps, freq=delta)
        except Exception:
            pass

        # 3) median diff 兜底
        try:
            diffs = pd.Series(series.index).diff().dropna()
            if len(diffs) > 0:
                step = diffs.median()
                if step > pd.Timedelta(0):
                    return pd.date_range(series.index[-1] + step, periods=steps, freq=step)
        except Exception:
            pass

    # 4) 最后兜底
    return pd.RangeIndex(start=len(series), stop=len(series) + steps)


def _ds_str(index) -> list:
    """
    将索引转为字符串列表。
    小时级数据用 'YYYY-MM-DD HH:MM' 防止 DuckDB 把字符串列推断为 TIMESTAMP。
    """
    if hasattr(index, "strftime"):
        if hasattr(index, "hour") and len(index) > 0 and index.hour.max() > 0:
            return index.strftime("%Y-%m-%d %H:%M").tolist()
        return index.strftime("%Y-%m-%d").tolist()
    return [str(i) for i in index]


def _build_result_df(
    series: pd.Series,
    params: dict,
    steps: int,
    day_scale: float,
    clip_negative: bool,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    fitted_y = params["trend_hat"] + params["seas_hat"]
    residuals = params["residuals"]
    z95 = 1.96

    # ── 历史部分 ────────────────────────────────────────────────────────────
    hist = pd.DataFrame({
        "ds":       _ds_str(series.index),
        "y_actual": series.values.astype(float).round(4),
        "y_pred":   fitted_y.astype(float).round(4),
        "lower_ci": np.full(len(series), np.nan, dtype=float),
        "upper_ci": np.full(len(series), np.nan, dtype=float),
        "segment":  "historical",
    })

    # ── 预测部分 ────────────────────────────────────────────────────────────
    future_idx = _build_future_index(series, steps, day_scale)
    _, _, _, y_f = _forecast_prophet(params, steps)

    sigma_f = _forecast_sigma(residuals, steps)
    lower_f = y_f - z95 * sigma_f
    upper_f = y_f + z95 * sigma_f

    if clip_negative:
        y_f = np.maximum(y_f, 0.0)
        lower_f = np.maximum(lower_f, 0.0)
        upper_f = np.maximum(upper_f, 0.0)

    future_ds = _ds_str(future_idx) if hasattr(future_idx, "strftime") else [str(i) for i in future_idx]
    fcast = pd.DataFrame({
        "ds":       future_ds,
        "y_actual": np.full(steps, np.nan, dtype=float),
        "y_pred":   y_f.astype(float).round(4),
        "lower_ci": lower_f.astype(float).round(4),
        "upper_ci": upper_f.astype(float).round(4),
        "segment":  "forecast",
    })

    result_df = pd.concat([hist, fcast], ignore_index=True)
    for col in ["y_actual", "y_pred", "lower_ci", "upper_ci"]:
        result_df[col] = pd.to_numeric(result_df[col], errors="coerce")

    # ── breakdown ───────────────────────────────────────────────────────────
    breakdown_df = pd.DataFrame({
        "ds":       _ds_str(series.index),
        "row_num":  np.arange(1, len(series) + 1, dtype=int),
        "trend":    params["trend_hat"].astype(float).round(4),
        "yearly":   params["yearly_hat"].astype(float).round(4),
        "weekly":   params["weekly_hat"].astype(float).round(4),
        "daily":    params["daily_hat"].astype(float).round(4),
        "residual": residuals.astype(float).round(4),
    })

    return result_df, breakdown_df

def _compute_error_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    residuals = y_true - y_pred
    mae = float(np.mean(np.abs(residuals)))
    rmse = float(np.sqrt(np.mean(residuals ** 2)))

    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    ss_res = float(np.sum(residuals ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    nz = y_true != 0
    mape = float(np.mean(np.abs(residuals[nz] / y_true[nz])) * 100) if nz.any() else float("nan")

    denom_wape = float(np.sum(np.abs(y_true)))
    wape = float(np.sum(np.abs(residuals)) / denom_wape * 100) if denom_wape > 0 else float("nan")

    smape_denom = (np.abs(y_true) + np.abs(y_pred))
    valid = smape_denom > 0
    smape = float(np.mean(2.0 * np.abs(y_true[valid] - y_pred[valid]) / smape_denom[valid]) * 100) if valid.any() else float("nan")

    return {
        "mae": mae, "rmse": rmse, "r2": r2, "mape": mape, "wape": wape, "smape": smape
    }


def _backtest_fold(args: Tuple[pd.Series, pd.Series, float]) -> dict:
    """Single rolling-backtest fold.

    Kept at module scope (not a closure) so it pickles cleanly for the
    ProcessPoolExecutor spawn context. Each fold runs its own ``_fit_prophet``
    (numpy ridge regression + Fourier features) which is pure CPU work —
    exactly the kind of GIL-bound loop that threads cannot parallelize.
    Returns the error metrics dict, or ``{}`` on failure (caller filters).
    """
    train_series, val_series, day_scale = args
    try:
        _, t_max_days_tmp = _to_t(train_series.index)
        yo, wo, do = _auto_fourier_orders(t_max_days_tmp, day_scale)
        p = _fit_prophet(train_series, day_scale, yo, wo, do)
        _, _, _, y_f = _forecast_prophet(p, len(val_series))
        # 若训练数据全非负，保持和主流程一致
        if (train_series.values >= 0).all():
            y_f = np.maximum(y_f, 0.0)
        return _compute_error_metrics(val_series.values.astype(float), y_f.astype(float))
    except Exception:
        return {}


def _rolling_backtest(series: pd.Series, day_scale: float, n_folds: int = 3, horizon: int = 30) -> dict:
    """
    简化版滚动回测：
    每折：前段训练、后 horizon 验证

    并行策略：n_folds 个 fold 互相独立，每折内部是纯 numpy CPU 密集
    运算（岭回归 + 傅里叶特征），GIL 下线程无法真正并行。改用
    ProcessPoolExecutor（spawn 上下文）绕开 GIL，n_folds 个进程同时
    拟合。小数据或进程池不可用时自动降级为同步。
    """
    y_all = series.values.astype(float)
    n = len(series)

    # 防止样本太小
    min_train = max(60, int(n * 0.5))
    horizon = min(horizon, max(5, n // 10))
    if n < min_train + horizon + 5:
        return {"ok": False}

    # 收集各 fold 的输入（train_series, val_series, day_scale）
    fold_args: List[Tuple[pd.Series, pd.Series, float]] = []
    for i in range(n_folds, 0, -1):
        val_end = n - (i - 1) * horizon
        val_start = val_end - horizon
        train_end = val_start
        if train_end < min_train:
            continue
        fold_args.append((series.iloc[:train_end], series.iloc[val_start:val_end], day_scale))

    if not fold_args:
        return {"ok": False}

    # CPU 密集：每折独立拟合，用进程池绕开 GIL；失败自动降级为同步
    from infrastructure.cpu_pool import map_cpu_bound
    raw_metrics = map_cpu_bound(_backtest_fold, fold_args)
    fold_metrics = [m for m in raw_metrics if m]

    if not fold_metrics:
        return {"ok": False}

    def avg(k):
        vals = [x[k] for x in fold_metrics if np.isfinite(x[k])]
        return float(np.mean(vals)) if vals else float("nan")

    return {
        "ok": True,
        "folds": len(fold_metrics),
        "val_mae": avg("mae"),
        "val_rmse": avg("rmse"),
        "val_mape": avg("mape"),
        "val_wape": avg("wape"),
        "val_smape": avg("smape"),
        "val_r2": avg("r2"),
    }

def _build_metrics_df(series: pd.Series, params: dict, backtest: Optional[dict] = None) -> pd.DataFrame:
    fitted_y = params["trend_hat"] + params["seas_hat"]
    y_true   = series.values.astype(float)

    train_m = _compute_error_metrics(y_true, fitted_y)

    # 活跃变点
    if len(params["deltas"]) > 0:
        threshold = 0.01 * np.max(np.abs(params["deltas"]))
        n_cp_active = int(np.sum(np.abs(params["deltas"]) > threshold))
    else:
        n_cp_active = 0

    rows = [
        {"metric": "模型类型",          "value": "Prophet 加法分解"},
        {"metric": "趋势类型",          "value": "分段线性（岭回归/L2 变点检测）"},
        {"metric": "日内季节性（24h）",  "value": f"傅里叶阶数 {params['daily_order']}"  if params["use_daily"]  else "未启用"},
        {"metric": "周度季节性（7d）",   "value": f"傅里叶阶数 {params['weekly_order']}" if params["use_weekly"] else "未启用"},
        {"metric": "年度季节性（365d）", "value": f"傅里叶阶数 {params['yearly_order']}" if params["use_yearly"] else "未启用"},
        {"metric": "活跃变点数",         "value": n_cp_active},
        {"metric": "训练样本数",         "value": len(series)},
        {"metric": "R²（训练集）",       "value": round(train_m["r2"], 4)},
        {"metric": "MAE（训练集）",      "value": round(train_m["mae"], 4)},
        {"metric": "RMSE（训练集）",     "value": round(train_m["rmse"], 4)},
        {"metric": "MAPE %（训练集）",   "value": round(train_m["mape"], 2) if not np.isnan(train_m["mape"]) else "N/A"},
        {"metric": "WAPE %（训练集）",   "value": round(train_m["wape"], 2) if not np.isnan(train_m["wape"]) else "N/A"},
        {"metric": "sMAPE %（训练集）",  "value": round(train_m["smape"], 2) if not np.isnan(train_m["smape"]) else "N/A"},
    ]

    if backtest and backtest.get("ok", False):
        rows += [
            {"metric": "回测折数",          "value": backtest["folds"]},
            {"metric": "R²（回测）",        "value": round(backtest["val_r2"], 4) if np.isfinite(backtest["val_r2"]) else "N/A"},
            {"metric": "MAE（回测）",       "value": round(backtest["val_mae"], 4) if np.isfinite(backtest["val_mae"]) else "N/A"},
            {"metric": "RMSE（回测）",      "value": round(backtest["val_rmse"], 4) if np.isfinite(backtest["val_rmse"]) else "N/A"},
            {"metric": "MAPE %（回测）",    "value": round(backtest["val_mape"], 2) if np.isfinite(backtest["val_mape"]) else "N/A"},
            {"metric": "WAPE %（回测）",    "value": round(backtest["val_wape"], 2) if np.isfinite(backtest["val_wape"]) else "N/A"},
            {"metric": "sMAPE %（回测）",   "value": round(backtest["val_smape"], 2) if np.isfinite(backtest["val_smape"]) else "N/A"},
        ]

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  6. Markdown 报告
# ═══════════════════════════════════════════════════════════════════════════

def _build_md(target_col, time_col, steps, series, params, metrics_df, result_df) -> str:
    forecast_rows = result_df[result_df["segment"] == "forecast"].head(10)

    L = [
        f"## Prophet 风格时间序列预测 — `{target_col}`\n",
        "### 模型概况",
        "| 指标 | 值 |", "|------|-----|",
        "| 模型类型 | Prophet 加法分解（趋势 + 季节性） |",
        f"| 时间列 | `{time_col or '（行序号）'}` |",
        f"| 训练样本数 | {len(series)} |",
        f"| 预测步数 | {steps} |",
        f"| 日内季节性（24h） | {'✓ 启用（N=' + str(params['daily_order'])  + '）' if params['use_daily']  else '未启用'} |",
        f"| 周度季节性（7d）  | {'✓ 启用（N=' + str(params['weekly_order']) + '）' if params['use_weekly'] else '未启用'} |",
        f"| 年度季节性（365d）| {'✓ 启用（N=' + str(params['yearly_order']) + '）' if params['use_yearly'] else '未启用'} |",
        "",
        "### 模型评估",
        "| 指标 | 值 |", "|------|-----|",
    ]
    for _, row in metrics_df.iterrows():
        L.append(f"| {row['metric']} | {row['value']} |")
    L.append("")

    L += ["### 预测摘要（未来数据点）",
          "| 时间点 | 预测值 | 95% 置信区间 |",
          "|--------|-------:|-------------|"]
    for _, row in forecast_rows.iterrows():
        lo = row["lower_ci"] if not pd.isna(row["lower_ci"]) else "—"
        hi = row["upper_ci"] if not pd.isna(row["upper_ci"]) else "—"
        lo_str = f"{lo:.4f}" if isinstance(lo, float) else lo
        hi_str = f"{hi:.4f}" if isinstance(hi, float) else hi
        L.append(f"| {row['ds']} | {row['y_pred']:.4f} | [{lo_str}, {hi_str}] |")
    L.append("")

    # 趋势洞察
    last_actual   = float(series.iloc[-1])
    last_pred_row = result_df[result_df["segment"] == "forecast"].iloc[-1]
    last_pred     = float(last_pred_row["y_pred"])
    direction     = "上升" if last_pred > last_actual else "下降"
    chg_pct       = abs(last_pred - last_actual) / abs(last_actual) * 100 if last_actual != 0 else 0

    if len(params["deltas"]) > 0:
        threshold = 0.01 * np.max(np.abs(params["deltas"]))
        n_cp_active = int(np.sum(np.abs(params["deltas"]) > threshold))
    else:
        n_cp_active = 0

    L += [
        "### 核心洞察",
        f"- **趋势方向**：预测期末值（{last_pred:.4f}）"
        f"较历史末值（{last_actual:.4f}）{direction}，幅度约 **{chg_pct:.1f}%**。",
        f"- **变点检测**：历史数据中识别出 {n_cp_active} 个显著趋势变点。",
    ]
    if params["use_daily"]:
        L.append(f"- **日内季节性**：振幅约 {float(np.ptp(params['daily_hat'])):.4f}（峰谷差），周期 24 小时。")
    if params["use_weekly"]:
        L.append(f"- **周度季节性**：振幅约 {float(np.ptp(params['weekly_hat'])):.4f}（峰谷差），周期 7 天。")
    if params["use_yearly"]:
        L.append(f"- **年度季节性**：振幅约 {float(np.ptp(params['yearly_hat'])):.4f}（峰谷差），周期 365 天。")

    L += [
        "",
        "> **图表建议**",
        "> - Line_Chart(analysis_result)：x=ds，y=y_pred + y_actual，按 segment 分组着色",
        "> - Line_Chart(analysis_breakdown)：x=ds，y=trend + yearly + weekly + daily — 成分分解可视化",
        "> - analysis_metrics 表直接展示 R²/MAE/RMSE",
    ]
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
#  7. 主入口
# ═══════════════════════════════════════════════════════════════════════════

def run(
    df:             pd.DataFrame,
    target_column:  str,
    groupby_column: Optional[str] = None,
    n_deciles:      int = 0,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    """
    Parameters
    ----------
    df             : 原始 DataFrame
    target_column  : 目标数值列
    groupby_column : 时间列名(默认自动探测)
    n_deciles      : 预测步数(默认 30)
    """
    progress = progress_callback or (lambda _pct, _message="": None)
    progress(5, "正在校验 Prophet 输入")
    if target_column not in df.columns:
        raise ValueError(f"目标列 '{target_column}' 不存在。可用列：{', '.join(df.columns[:20])}")
    if not pd.api.types.is_numeric_dtype(df[target_column]):
        raise ValueError(f"目标列 '{target_column}' 不是数值类型,Prophet 预测要求连续数值目标变量。")

    try:
        steps = int(n_deciles)
    except (TypeError, ValueError):
        steps = 0
    if steps <= 0:
        steps = _DEFAULT_STEPS

    progress(18, "正在准备时间序列")
    time_col = _detect_time_col(df, groupby_column or "")
    series, day_scale = _prepare_series(df, time_col, target_column)

    if len(series) < 10:
        raise ValueError(f"有效数据点不足({len(series)} 个),Prophet 至少需要 10 个数据点。")

    # ─────────────────────────────────────────────────────────────────────
    #  数据量保护:超过阈值时降采样到日级别
    #  小时级 10000+ 点上,8 阶年度 Fourier 无法解释噪声、lstsq 矩阵过大,
    #  日级降采样后 yearly + weekly 季节性效果显著更好,且预测更稳定。
    # ─────────────────────────────────────────────────────────────────────
    resampled = False
    original_len = len(series)
    selected_freq = "original"
    if time_col and isinstance(series.index, pd.DatetimeIndex) and len(series) > _RESAMPLE_THRESHOLD:
        # 候选：4小时、8小时、1天
        cand = ["4H", "8H", "D"]
        progress(30, "正在评估降采样频率")
        new_series, selected_freq = _select_best_resample(series, cand, steps)
        if selected_freq != "original":
            series = new_series
            # 重新计算 day_scale
            deltas = pd.Series(series.index).diff().dt.total_seconds().dropna() / 86400
            day_scale = float(deltas.median()) if len(deltas) else 1.0
            if not np.isfinite(day_scale) or day_scale <= 0:
                day_scale = 1.0
            resampled = True
            print(
                f"[Time_Series_Prophet] 自动降采样: 原 {original_len} 点 -> '{selected_freq}'({_RESAMPLE_AGG}+插值) -> {len(series)} 点")

    # 非负数据检测(用于预测裁剪)
    clip_negative = bool((series.values >= 0).all())

    _, t_max_days_tmp = _to_t(series.index)
    yearly_order, weekly_order, daily_order = _auto_fourier_orders(t_max_days_tmp, day_scale)
    progress(48, "正在拟合趋势和季节性")
    params = _fit_prophet(series, day_scale, yearly_order, weekly_order, daily_order)

    # 构建结果表
    progress(72, "正在生成 Prophet 预测")
    result_df, breakdown_df = _build_result_df(series, params, steps, day_scale, clip_negative)
    progress(84, "正在执行滚动回测")
    bt = _rolling_backtest(series, day_scale, n_folds=3, horizon=min(steps, max(7, len(series) // 12)))
    metrics_df = _build_metrics_df(series, params, backtest=bt)

    # 在 metrics 表里记录降采样信息
    if resampled:
        extra = pd.DataFrame([{
            "metric": "数据降采样",
            "value":  f"原 {original_len} 点 → '{selected_freq}' ({_RESAMPLE_AGG}+插值) → {len(series)} 点",
        }])
        metrics_df = pd.concat([extra, metrics_df], ignore_index=True)
    if clip_negative:
        extra2 = pd.DataFrame([{
            "metric": "预测下限裁剪",
            "value":  "已启用(训练数据全部非负,预测值裁剪到 ≥0)",
        }])
        metrics_df = pd.concat([metrics_df, extra2], ignore_index=True)

    markdown = _build_md(
        target_col = target_column,
        time_col   = time_col,
        steps      = steps,
        series     = series,
        params     = params,
        metrics_df = metrics_df,
        result_df  = result_df,
    )

    progress(98, "Prophet 分析计算完成")
    return result_df, breakdown_df, metrics_df, markdown
