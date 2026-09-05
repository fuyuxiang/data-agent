#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Time_Series_SARIMA
==================
基于 pmdarima auto_arima + statsmodels SARIMAX 的季节性时间序列预测模块。

功能：
  - 自动检测时间列，推断数据频率与季节周期 s
  - 大数据量自动降采样到日级别（避免 SARIMA 在 10000+ 点 × m=24 时卡死）
  - pmdarima auto_arima stepwise 在全量数据上搜索最优 (p,d,q)(P,D,Q)[s]
  - 季节分解（趋势 / 季节性 / 残差）写入 breakdown 表
  - 输出三张结果表：
      analysis_result    — 预测 + 历史拟合（ds / y_actual / y_pred / lower_ci / upper_ci / segment）
      analysis_breakdown — 季节分解（ds / trend / seasonal / residual）
      analysis_metrics   — 模型评估（metric / value）
"""

import warnings
import numpy as np
import pandas as pd
from typing import Callable, Optional, Tuple, List

warnings.filterwarnings("ignore")

# ── 模块元数据 ──────────────────────────────────────────────────────────────
ANALYSIS_ID   = "Time_Series_SARIMA"
ANALYSIS_NAME = "SARIMA 季节性时间序列预测"
ANALYSIS_DESC = (
    "使用 SARIMA 模型对含季节性波动的单变量时间序列建模预测。"
    "自动推断季节周期、执行 ADF 检验、AIC 选阶，输出预测值与季节分解结果。"
    "通过 groupby_column 指定时间列名或季节周期（纯数字如 '12'）；"
    "通过 n_deciles 指定预测步数（默认 12）。"
)
REQUIRED_PARAMS = ["target_column"]
OPTIONAL_PARAMS = [
    "groupby_column (时间列名，或季节周期数字如 '12'/'4'/'7'，默认自动推断)",
    "n_deciles (预测步数，默认 12)",
]
OUTPUT_TABLES = ["analysis_result", "analysis_breakdown", "analysis_metrics"]

_DEFAULT_STEPS = 12
_FREQ_TO_PERIOD = {"D": 7, "W": 52, "MS": 12, "M": 12, "QS": 4, "Q": 4, "AS": 1, "A": 1, "H": 24}

# 降采样配置
_RESAMPLE_THRESHOLD = 2000      # 超过此点数触发降采样
_RESAMPLE_FREQ      = "D"       # 降采样目标频率（日）
_RESAMPLE_AGG       = "mean"    # 聚合方式: mean 保持量纲, 适用于任意连续变量


# ═══════════════════════════════════════════════════════════════════════════
#  1. 工具函数
# ═══════════════════════════════════════════════════════════════════════════

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


def _prepare_series(df: pd.DataFrame, time_col: str, value_col: str) -> Tuple[pd.Series, str]:
    """返回 (series, inferred_freq_str)。"""
    work = df[[time_col, value_col]].copy() if time_col else df[[value_col]].copy()
    if time_col:
        work[time_col] = pd.to_datetime(work[time_col])
        work = work.sort_values(time_col).drop_duplicates(time_col)
        work = work.set_index(time_col)
    series = work[value_col].astype(float).dropna()
    freq_str = ""
    if time_col and len(series) >= 3:
        try:
            inferred = pd.infer_freq(series.index)
            if inferred:
                series = series.asfreq(inferred)
                freq_str = inferred
        except Exception:
            pass
    return series, freq_str


def _resample_to_regular(series: pd.Series, freq: str, agg: str) -> pd.Series:
    """
    降采样到规则频率，并保证返回的 Series 有合法 freq 属性。

    关键：不能直接 .dropna()，否则有缺口的数据（如 Bike Sharing 每月 20-31 日缺失）
    会被 dropna 删成不规则索引，丢失 freq → SARIMAX 退化为整数索引 → 预测时间错位。

    做法：resample 保留 NaN → 时间插值 → ffill/bfill 兜底 → asfreq 显式确认频率。
    """
    # 1) 聚合（保留 NaN 行,频率结构完整）
    resampled = getattr(series.resample(freq), agg)()
    # 2) 按时间距离插值,填补聚合产生的 NaN
    resampled = resampled.interpolate(method="time")
    # 3) 首尾 NaN 兜底（插值不能外推）
    resampled = resampled.ffill().bfill()
    # 4) 显式确认 freq（防御性,某些 pandas 版本插值后会丢失 freq）
    resampled = resampled.asfreq(freq)
    return resampled


def _infer_season_period(freq_str: str, series: pd.Series) -> int:
    """根据频率字符串推断季节周期，无法推断时用 FFT 检测。"""
    for key, period in _FREQ_TO_PERIOD.items():
        if freq_str.upper().startswith(key):
            return period
    # FFT 频率检测
    if len(series) >= 24:
        try:
            vals = series.values - series.values.mean()
            fft = np.abs(np.fft.rfft(vals))
            freqs = np.fft.rfftfreq(len(vals))
            # 排除直流分量
            dominant_idx = np.argmax(fft[1:]) + 1
            dominant_freq = freqs[dominant_idx]
            if dominant_freq > 0:
                period = round(1.0 / dominant_freq)
                if 2 <= period <= len(series) // 2:
                    return period
        except Exception:
            pass
    return 12  # 默认月度


def _adf_test(series: pd.Series) -> Tuple[bool, float]:
    try:
        from statsmodels.tsa.stattools import adfuller
        r = adfuller(series.dropna(), autolag="AIC")
        return r[1] < 0.05, float(r[1])
    except Exception:
        return True, 0.0


def _auto_order(series: pd.Series, season_period: int) -> Tuple[Tuple[int,int,int], Tuple[int,int,int,int]]:
    """
    用 pmdarima auto_arima stepwise 在全量数据上搜索最优 SARIMA 阶数。
    stepwise=True 使用 Hyndman-Khandakar 算法，在保证建模质量的同时避免组合爆炸。
    返回 (order, seasonal_order) = ((p,d,q), (P,D,Q,s))。
    """
    from pmdarima import auto_arima
    model = auto_arima(
        series,
        start_p=0, max_p=3,
        start_q=0, max_q=3,
        max_d=2,
        start_P=0, max_P=2,
        start_Q=0, max_Q=2,
        max_D=1,
        seasonal=True,
        m=season_period,
        stepwise=True,
        information_criterion="aic",
        error_action="ignore",
        suppress_warnings=True,
        with_intercept="auto",
    )
    return model.order, model.seasonal_order


# ═══════════════════════════════════════════════════════════════════════════
#  2. 季节分解
# ═══════════════════════════════════════════════════════════════════════════

def _seasonal_decompose(series: pd.Series, period: int) -> pd.DataFrame:
    """STL 或经典加法分解，返回 breakdown_df。
    包含 row_num 整数列，方便 Scatter_Plot 用作 x 轴（日期字符串不可直接作为数值轴）。
    """
    try:
        from statsmodels.tsa.seasonal import seasonal_decompose as _sd
        dec = _sd(series.dropna(), model="additive", period=period, extrapolate_trend="freq")
        df = pd.DataFrame({
            "ds":       dec.trend.index.astype(str),
            "trend":    dec.trend.values.round(4),
            "seasonal": dec.seasonal.values.round(4),
            "residual": dec.resid.values.round(4),
        }).dropna().reset_index(drop=True)
        df.insert(1, "row_num", np.arange(1, len(df) + 1, dtype=int))
        return df
    except Exception:
        return pd.DataFrame(columns=["ds", "row_num", "trend", "seasonal", "residual"])


# ═══════════════════════════════════════════════════════════════════════════
#  3. 核心拟合与预测
# ═══════════════════════════════════════════════════════════════════════════

def _fit_predict(
    series: pd.Series,
    order: Tuple[int, int, int],
    seasonal_order: Tuple[int, int, int, int],
    steps: int,
) -> Tuple[pd.Series, pd.DataFrame, pd.Series, object, int]:
    from statsmodels.tsa.statespace.sarimax import SARIMAX as _SARIMAX
    model = _SARIMAX(
        series,
        order=order,
        seasonal_order=seasonal_order,
        enforce_stationarity=False,
        enforce_invertibility=False,
    )
    result = model.fit(disp=False, maxiter=100)

    # SARIMAX.fittedvalues 在初始化期（warmup 步）返回的是状态空间初始化值，
    # 不是原始尺度的拟合值。warmup = max(p+P*s, q+Q*s, d+D*s)。
    # 正确做法：跳过初始化期，只保留收敛后的拟合值，其余填 NaN。
    p, d, q = order
    P, D, Q, s = seasonal_order
    warmup = max(p + P * s, q + Q * s, d + D * s, 1)

    # 直接取 fittedvalues，但将前 warmup 步置为 NaN
    fitted = result.fittedvalues.copy().astype(float)
    fitted.iloc[:warmup] = np.nan

    fc   = result.get_forecast(steps=steps)
    mean = fc.predicted_mean
    ci   = fc.conf_int(alpha=0.05)

    # ─────────────────────────────────────────────────────────────────────
    #  防御性修复: 如果历史索引是 datetime 但预测索引退化成了整数
    #  (statsmodels 在 freq 缺失时会用 RangeIndex), 根据历史频率/中位间隔
    #  手动重建未来的 datetime 索引。否则下游会把整数当字符串排序,
    #  导致预测点在图上错位甚至"消失"。
    # ─────────────────────────────────────────────────────────────────────
    if isinstance(series.index, pd.DatetimeIndex) and not isinstance(mean.index, pd.DatetimeIndex):
        freq = series.index.freq
        if freq is not None:
            future_idx = pd.date_range(
                start=series.index[-1] + freq,
                periods=steps,
                freq=freq,
            )
        else:
            diffs = series.index.to_series().diff().dropna()
            step = diffs.median() if len(diffs) > 0 else pd.Timedelta(days=1)
            future_idx = pd.date_range(
                start=series.index[-1] + step,
                periods=steps,
                freq=step,
            )
        mean.index = future_idx
        ci.index   = future_idx

    forecast_df = pd.DataFrame({
        "mean":  mean.values,
        "lower": ci.iloc[:, 0].values,
        "upper": ci.iloc[:, 1].values,
    }, index=mean.index)

    return fitted, forecast_df, result.resid, result, warmup


# ═══════════════════════════════════════════════════════════════════════════
#  4. 结果表
# ═══════════════════════════════════════════════════════════════════════════

def _build_result_df(series, fitted, forecast_df) -> pd.DataFrame:
    # fitted 已经把初始化期（warmup）设为 NaN，直接 reindex 对齐
    fitted_aligned = fitted.reindex(series.index)
    y_pred_hist = fitted_aligned.values.copy()
    # 确保数值精度，保留 NaN
    y_pred_hist = np.where(np.isnan(y_pred_hist), np.nan,
                           np.round(y_pred_hist.astype(float), 4))

    hist = pd.DataFrame({
        "ds":       series.index.astype(str),
        "y_actual": series.values.round(4),
        "y_pred":   y_pred_hist,
        "lower_ci": np.nan,
        "upper_ci": np.nan,
        "segment":  "historical",
    })
    fcast = pd.DataFrame({
        "ds":       forecast_df.index.astype(str),
        "y_actual": np.nan,
        "y_pred":   forecast_df["mean"].values.round(4),
        "lower_ci": forecast_df["lower"].values.round(4),
        "upper_ci": forecast_df["upper"].values.round(4),
        "segment":  "forecast",
    })
    result = pd.concat([hist, fcast], ignore_index=True)
    for col in ["y_actual", "y_pred", "lower_ci", "upper_ci"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(4)
    return result


def _build_metrics_df(series, fitted, model_result, order, seasonal_order, adf_pval, period) -> pd.DataFrame:
    # 只对 warmup 期之后（非 NaN）的拟合值计算误差指标
    common = series.index.intersection(fitted.index)
    y_true = series.loc[common].values.astype(float)
    y_pred = fitted.loc[common].values.astype(float)
    mask   = ~np.isnan(y_pred)
    y_true, y_pred = y_true[mask], y_pred[mask]

    mae  = float(np.mean(np.abs(y_true - y_pred)))
    rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))
    nz   = y_true != 0
    mape = float(np.mean(np.abs((y_true[nz] - y_pred[nz]) / y_true[nz])) * 100) if nz.any() else float("nan")

    p, d, q = order
    P, D, Q, s = seasonal_order
    rows = [
        {"metric": "模型阶数 (p,d,q)",          "value": f"({p},{d},{q})"},
        {"metric": "季节阶数 (P,D,Q,s)",         "value": f"({P},{D},{Q},{s})"},
        {"metric": "检测到的季节周期 s",          "value": period},
        {"metric": "ADF p值（原始序列）",         "value": round(adf_pval, 4)},
        {"metric": "AIC",                        "value": round(model_result.aic, 4)},
        {"metric": "BIC",                        "value": round(model_result.bic, 4)},
        {"metric": "MAE（训练集）",               "value": round(mae,  4)},
        {"metric": "RMSE（训练集）",              "value": round(rmse, 4)},
        {"metric": "MAPE %（训练集）",            "value": round(mape, 2) if not np.isnan(mape) else "N/A"},
        {"metric": "训练样本数",                  "value": len(series)},
    ]
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  5. Markdown 报告
# ═══════════════════════════════════════════════════════════════════════════

def _build_md(target_col, time_col, order, seasonal_order, adf_pval,
              stationary, steps, metrics_df, forecast_df, series, period) -> str:
    p, d, q = order
    P, D, Q, s = seasonal_order

    L = [
        f"## SARIMA 季节性时间序列预测 — `{target_col}`\n",
        "### 模型概况",
        "| 指标 | 值 |", "|------|-----|",
        f"| 模型类型 | SARIMA({p},{d},{q})({P},{D},{Q})[{s}] |",
        "| 选阶方法 | auto_arima stepwise（AIC，全量数据） |",
        f"| 时间列 | `{time_col or '（行序号）'}` |",
        f"| 训练样本数 | {len(series)} |",
        f"| 季节周期 s | {period} |",
        f"| 预测步数 | {steps} |",
        f"| ADF p值 | {adf_pval:.4f}（{'平稳 ✓' if stationary else '非平稳，已差分'}） |",
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
    for i, (idx, row) in enumerate(forecast_df.iterrows()):
        if i >= 10:
            L.append("| … | … | … |")
            break
        L.append(f"| {str(idx)} | {row['mean']:.4f} | [{row['lower']:.4f}, {row['upper']:.4f}] |")
    L.append("")

    last_actual = float(series.iloc[-1])
    last_pred   = float(forecast_df["mean"].iloc[-1])
    direction   = "上升" if last_pred > last_actual else "下降"
    chg_pct     = abs(last_pred - last_actual) / abs(last_actual) * 100 if last_actual != 0 else 0

    L += [
        "### 核心洞察",
        f"- **趋势方向**：预测期末值（{last_pred:.4f}）较历史末值（{last_actual:.4f}）{direction}"
        f"，幅度约 **{chg_pct:.1f}%**。",
        f"- **季节性**：检测到周期 s={period}，季节差分 D={D}，"
        f"{'模型包含季节成分' if P > 0 or Q > 0 else '季节参数较弱'}。",
        "",
        "> **图表建议**",
        "> - Line_Chart(analysis_result)：x=ds，y=y_pred + y_actual，按 segment 分组着色",
        "> - Line_Chart(analysis_breakdown)：x=ds，y=trend + seasonal — 季节分解可视化",
        "> - analysis_metrics 表直接展示模型指标",
    ]
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
#  6. 主入口
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
    groupby_column : 时间列名；或纯数字字符串表示季节周期（如 '12'）
    n_deciles      : 预测步数（默认 12）
    """
    progress = progress_callback or (lambda _pct, _message="": None)
    progress(5, "正在校验 SARIMA 输入")
    try:
        import pmdarima  # noqa
    except ImportError:
        raise ImportError("pmdarima 未安装。请运行: pip install pmdarima")

    if target_column not in df.columns:
        raise ValueError(f"目标列 '{target_column}' 不存在。可用列: {', '.join(df.columns[:20])}")

    try:
        steps = int(n_deciles)
    except (TypeError, ValueError):
        steps = 0
    if steps <= 0:
        steps = _DEFAULT_STEPS

    # 解析 groupby_column
    manual_period = None
    time_col_hint = ""
    if groupby_column:
        try:
            v = int(groupby_column.strip())
            if 2 <= v <= 365:
                manual_period = v
        except (ValueError, AttributeError):
            time_col_hint = groupby_column

    # 检测时间列 & 准备序列
    progress(15, "正在准备季节性序列")
    time_col = _detect_time_col(df, time_col_hint)
    series, freq_str = _prepare_series(df, time_col, target_column)

    # ─────────────────────────────────────────────────────────────────────
    #  数据量保护 + 自动降采样
    #  SARIMA 在 10000+ 点 × m=24 的组合下拟合极慢（单次几分钟,
    #  stepwise 累计可达数小时）。超过阈值时先聚合到日级别,
    #  既能控制计算量,又能将小时周期（m=24）转换为周周期（m=7）,大幅加速。
    #
    #  关键: 使用 _resample_to_regular 而非简单的 resample().dropna(),
    #  否则有缺口的数据会在 dropna 后变成不规则索引,丢失 freq 属性,
    #  导致 SARIMAX 用整数索引预测,未来时间点全部错位。
    # ─────────────────────────────────────────────────────────────────────
    resampled = False
    original_len = len(series)
    if time_col and isinstance(series.index, pd.DatetimeIndex) and len(series) > _RESAMPLE_THRESHOLD:
        series = _resample_to_regular(series, _RESAMPLE_FREQ, _RESAMPLE_AGG)
        freq_str = _RESAMPLE_FREQ
        resampled = True
        freq_attr = series.index.freqstr or "未知"
        print(f"[Time_Series_SARIMA] 数据点数 {original_len} 超过阈值 "
              f"{_RESAMPLE_THRESHOLD},已按 '{_RESAMPLE_FREQ}' 聚合（{_RESAMPLE_AGG}）"
              f"+ 时间插值 → {len(series)} 个点(freq={freq_attr})")

    if len(series) < 16:
        raise ValueError(f"有效数据点不足({len(series)} 个),SARIMA 至少需要 16 个数据点。")

    # 季节周期
    period = manual_period if manual_period else _infer_season_period(freq_str, series)
    # 数据量不足以支撑该周期时降级
    if len(series) < period * 2 + 4:
        period = 2

    # 平稳性
    progress(28, "正在检验序列平稳性")
    stationary, adf_pval = _adf_test(series)

    # auto_arima stepwise 全量选阶
    progress(42, "正在选择 SARIMA 阶数")
    order, seasonal_order = _auto_order(series, period)
    P, D, Q, s = seasonal_order

    # 拟合 & 预测
    progress(68, "正在拟合 SARIMA 并预测")
    fitted, forecast_df, residuals, model_result, warmup = _fit_predict(series, order, seasonal_order, steps)

    # 结果表
    progress(88, "正在整理预测和季节分解结果")
    result_df    = _build_result_df(series, fitted, forecast_df)
    breakdown_df = _seasonal_decompose(series, period)
    metrics_df   = _build_metrics_df(series, fitted, model_result, order, seasonal_order, adf_pval, period)

    # 在 metrics 表里记录降采样信息,方便上游展示
    if resampled:
        extra = pd.DataFrame([{
            "metric": "数据降采样",
            "value":  f"原 {original_len} 点 → '{_RESAMPLE_FREQ}' ({_RESAMPLE_AGG}+插值) → {len(series)} 点",
        }])
        metrics_df = pd.concat([extra, metrics_df], ignore_index=True)

    markdown = _build_md(
        target_col     = target_column,
        time_col       = time_col,
        order          = order,
        seasonal_order = seasonal_order,
        adf_pval       = adf_pval,
        stationary     = stationary,
        steps          = steps,
        metrics_df     = metrics_df,
        forecast_df    = forecast_df,
        series         = series,
        period         = s if s > 1 else period,
    )

    progress(98, "SARIMA 分析计算完成")
    return result_df, breakdown_df, metrics_df, markdown
