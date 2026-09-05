#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Time_Series_ARIMA
=================
基于 pmdarima auto_arima + statsmodels 的 ARIMA / ARMA 时间序列预测模块。

功能：
  - 自动检测时间列与数值列
  - ADF 单位根检验，自动确定差分阶数 d
  - pmdarima auto_arima stepwise 搜索全量数据最优 (p, d, q)，或手动指定
  - 支持置信区间输出
  - 残差诊断（标准化残差）
  - 输出三张结果表：
      analysis_result    — 预测值 + 历史拟合（ds / y_actual / y_pred / lower_ci / upper_ci / segment）
      analysis_breakdown — 残差诊断（ds / row_num / residual / std_residual）
      analysis_metrics   — 模型评估指标（metric / value）
"""

import warnings
import numpy as np
import pandas as pd
from typing import Callable, Optional, Tuple

warnings.filterwarnings("ignore")

# ── 模块元数据 ──────────────────────────────────────────────────────────────
ANALYSIS_ID   = "Time_Series_ARIMA"
ANALYSIS_NAME = "ARIMA 时间序列预测"
ANALYSIS_DESC = (
    "使用 ARIMA 模型对单变量时间序列进行建模与预测。"
    "pmdarima auto_arima stepwise 在全量数据上搜索最优阶数，输出预测值、置信区间与残差诊断。"
    "通过 groupby_column 指定时间列名（或 'p,d,q' 手动设阶）；"
    "通过 n_deciles 指定预测步数（默认 12）。"
)
REQUIRED_PARAMS = ["target_column"]
OPTIONAL_PARAMS = [
    "groupby_column (时间列名，或手动阶数 'p,d,q'，默认自动)",
    "n_deciles (预测步数，默认 12)",
]
OUTPUT_TABLES = ["analysis_result", "analysis_breakdown", "analysis_metrics"]

_MAX_P = 5
_MAX_Q = 5
_MAX_D = 2
_DEFAULT_STEPS = 12


# ═══════════════════════════════════════════════════════════════════════════
#  1. 工具函数
# ═══════════════════════════════════════════════════════════════════════════

def _detect_time_col(df: pd.DataFrame, hint: str = "") -> str:
    """找时间列：优先用 hint，再按 dtype，最后按列名关键词。"""
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


def _prepare_series(df: pd.DataFrame, time_col: str, value_col: str) -> pd.Series:
    work = df[[time_col, value_col]].copy() if time_col else df[[value_col]].copy()
    if time_col:
        work[time_col] = pd.to_datetime(work[time_col])
        work = work.sort_values(time_col).drop_duplicates(time_col)
        work = work.set_index(time_col)
    series = work[value_col].astype(float).dropna()
    if time_col and len(series) >= 3:
        try:
            inferred = pd.infer_freq(series.index)
            if inferred:
                series = series.asfreq(inferred)
            # 注意：infer_freq 失败时不强制 asfreq，避免在缺口处插入 NaN
            # 让真实间隔保留，预测索引在 _fit_predict 里手动构造
        except Exception:
            pass
    return series


def _adf_test(series: pd.Series) -> Tuple[bool, float]:
    """ADF 检验，返回 (is_stationary, p_value)。"""
    try:
        from statsmodels.tsa.stattools import adfuller
        result = adfuller(series.dropna(), autolag="AIC")
        return result[1] < 0.05, float(result[1])
    except Exception:
        return True, 0.0


def _parse_order(hint: str) -> Optional[Tuple[int, int, int]]:
    """解析 'p,d,q' 格式的手动阶数。"""
    try:
        parts = [int(x.strip()) for x in hint.split(",")]
        if len(parts) == 3:
            return tuple(parts)
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  2. 选阶：auto_arima stepwise（全量数据）
# ═══════════════════════════════════════════════════════════════════════════

def _auto_order(series: pd.Series) -> Tuple[int, int, int]:
    """
    用 pmdarima auto_arima stepwise 在全量数据上搜索最优 (p, d, q)。
    stepwise=True 使用 Hyndman-Khandakar 算法，复杂度远低于暴力网格，
    同时保证在全量数据上以 AIC 为准则选阶，建模质量最优。
    """
    from pmdarima import auto_arima
    model = auto_arima(
        series,
        start_p=0, max_p=_MAX_P,
        start_q=0, max_q=_MAX_Q,
        max_d=_MAX_D,
        seasonal=False,       # 非季节性；季节性请用 SARIMA 模块
        stepwise=True,        # Hyndman-Khandakar stepwise，质量与全量网格等价
        information_criterion="aic",
        error_action="ignore",
        suppress_warnings=True,
        with_intercept="auto",
    )
    return model.order   # (p, d, q)


# ═══════════════════════════════════════════════════════════════════════════
#  3. 核心拟合与预测
# ═══════════════════════════════════════════════════════════════════════════

def _fit_predict(series, order, steps):
    from statsmodels.tsa.arima.model import ARIMA as _ARIMA
    model = _ARIMA(series, order=order)
    result = model.fit(method_kwargs={"warn_convergence": False})

    p, d, q = order
    warmup = max(d, 1)
    fitted = result.fittedvalues.copy().astype(float)
    fitted.iloc[:warmup] = np.nan

    fc = result.get_forecast(steps=steps)
    fc_mean = fc.predicted_mean
    fc_ci   = fc.conf_int(alpha=0.05)

    # —— 关键修复:如果预测索引不是 datetime,根据历史 median diff 重建 ——
    if isinstance(series.index, pd.DatetimeIndex) and not isinstance(fc_mean.index, pd.DatetimeIndex):
        diffs = series.index.to_series().diff().dropna()
        if len(diffs) > 0:
            step = diffs.median()
            future_idx = pd.date_range(
                start=series.index[-1] + step,
                periods=steps,
                freq=step,
            )
            fc_mean.index = future_idx
            fc_ci.index = future_idx

    forecast_df = pd.DataFrame({
        "mean":  fc_mean.values,
        "lower": fc_ci.iloc[:, 0].values,
        "upper": fc_ci.iloc[:, 1].values,
    }, index=fc_mean.index)

    return fitted, forecast_df, result.resid, result


# ═══════════════════════════════════════════════════════════════════════════
#  4. 结果表构建
# ═══════════════════════════════════════════════════════════════════════════

def _build_result_df(
    series: pd.Series,
    fitted: pd.Series,
    forecast_df: pd.DataFrame,
) -> pd.DataFrame:
    hist = pd.DataFrame({
        "ds":       series.index,
        "y_actual": series.values,
        "y_pred":   fitted.reindex(series.index).values,
        "lower_ci": np.nan,
        "upper_ci": np.nan,
        "segment":  "historical",
    })
    fcast = pd.DataFrame({
        "ds":       forecast_df.index,
        "y_actual": np.nan,
        "y_pred":   forecast_df["mean"].values,
        "lower_ci": forecast_df["lower"].values,
        "upper_ci": forecast_df["upper"].values,
        "segment":  "forecast",
    })
    result = pd.concat([hist, fcast], ignore_index=True)
    for col in ["y_actual", "y_pred", "lower_ci", "upper_ci"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(4)
    result["ds"] = result["ds"].astype(str)
    return result


def _build_breakdown_df(residuals: pd.Series) -> pd.DataFrame:
    """残差诊断表。row_num 为整数序号，Scatter_Plot 用它作 x 轴（ds 是字符串，不能作散点图 x 轴）。"""
    res = residuals.dropna()
    std = res.std() or 1.0
    return pd.DataFrame({
        "ds":           res.index.astype(str),
        "row_num":      np.arange(1, len(res) + 1, dtype=int),
        "residual":     res.values.round(4),
        "std_residual": (res.values / std).round(4),
    }).reset_index(drop=True)


def _build_metrics_df(
    series: pd.Series,
    fitted: pd.Series,
    result,
    order: Tuple[int, int, int],
    adf_pval: float,
) -> pd.DataFrame:
    common = series.index.intersection(fitted.index)
    y_true = series.loc[common].values
    y_pred = fitted.loc[common].values
    mask   = ~np.isnan(y_pred)
    y_true, y_pred = y_true[mask], y_pred[mask]

    mae  = float(np.mean(np.abs(y_true - y_pred)))
    rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))
    nonzero = y_true != 0
    mape = float(np.mean(np.abs((y_true[nonzero] - y_pred[nonzero]) / y_true[nonzero])) * 100) \
           if nonzero.any() else float("nan")

    rows = [
        {"metric": "模型阶数 (p,d,q)",   "value": str(order)},
        {"metric": "ADF p值（原始序列）", "value": round(adf_pval, 4)},
        {"metric": "AIC",              "value": round(result.aic,  4)},
        {"metric": "BIC",              "value": round(result.bic,  4)},
        {"metric": "MAE（训练集）",     "value": round(mae,  4)},
        {"metric": "RMSE（训练集）",    "value": round(rmse, 4)},
        {"metric": "MAPE %（训练集）",  "value": round(mape, 2) if not np.isnan(mape) else "N/A"},
        {"metric": "训练样本数",         "value": len(series)},
    ]
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  5. Markdown 报告
# ═══════════════════════════════════════════════════════════════════════════

def _build_md(
    target_col:  str,
    time_col:    str,
    order:       Tuple[int, int, int],
    adf_pval:    float,
    stationary:  bool,
    steps:       int,
    metrics_df:  pd.DataFrame,
    forecast_df: pd.DataFrame,
    series:      pd.Series,
) -> str:
    p, d, q = order
    model_type = "ARMA" if d == 0 else "ARIMA"

    L = [
        f"## {model_type} 时间序列预测 — `{target_col}`\n",
        "### 模型概况",
        "| 指标 | 值 |", "|------|-----|",
        f"| 模型类型 | {model_type}({p},{d},{q}) |",
        "| 选阶方法 | auto_arima stepwise（AIC，全量数据） |",
        f"| 时间列 | `{time_col or '（行序号）'}` |",
        f"| 训练样本数 | {len(series)} |",
        f"| 预测步数 | {steps} |",
        f"| ADF 检验 p值 | {adf_pval:.4f}（{'平稳 ✓' if stationary else '非平稳，已差分'}） |",
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
        f"，变化幅度约 **{chg_pct:.1f}%**。",
    ]
    if d == 0:
        L.append("- 序列原始平稳（d=0），使用 ARMA 模型直接拟合。")
    else:
        L.append(f"- 序列经过 {d} 阶差分后达到平稳，ARIMA 差分项 d={d}。")

    L += [
        "",
        "> **图表建议**",
        "> - Line_Chart(analysis_result)：x=ds，y=[y_actual, y_pred]，展示历史拟合与未来预测",
        "> - Scatter_Plot(analysis_breakdown)：x=row_num，y=std_residual — 检查残差随机性",
        ">   （analysis_breakdown 含列：ds, row_num, residual, std_residual；row_num 为整数序号，必须用它作 x 轴）",
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
    df             : 原始数据 DataFrame
    target_column  : 目标数值列
    groupby_column : 时间列名；或 'p,d,q' 手动指定阶数（优先解析为阶数）
    n_deciles      : 预测步数（默认 12，传 0 使用默认值）

    Returns
    -------
    result_df    → analysis_result
    breakdown_df → analysis_breakdown
    metrics_df   → analysis_metrics
    markdown     : Markdown 分析报告
    """
    progress = progress_callback or (lambda _pct, _message="": None)
    progress(5, "正在校验 ARIMA 输入")
    try:
        import pmdarima  # noqa — 提前检测依赖
    except ImportError:
        raise ImportError("pmdarima 未安装。请运行：pip install pmdarima")

    if target_column not in df.columns:
        raise ValueError(f"目标列 '{target_column}' 不存在。可用列：{', '.join(df.columns[:20])}")

    try:
        steps = int(n_deciles)
    except (TypeError, ValueError):
        steps = 0
    if steps <= 0:
        steps = _DEFAULT_STEPS

    # ── 解析 groupby_column ────────────────────────────────────────────────
    manual_order = None
    time_col_hint = ""
    if groupby_column:
        parsed = _parse_order(groupby_column)
        if parsed is not None:
            manual_order = parsed
        else:
            time_col_hint = groupby_column

    # ── 检测时间列 & 准备序列 ───────────────────────────────────────────────
    time_col = _detect_time_col(df, time_col_hint)
    series   = _prepare_series(df, time_col, target_column)
    if len(series) < 8:
        raise ValueError(f"有效数据点不足（{len(series)} 个），ARIMA 至少需要 8 个数据点。")

    # ── 平稳性检验 ─────────────────────────────────────────────────────────
    progress(20, "正在检验序列平稳性")
    stationary, adf_pval = _adf_test(series)

    # ── 确定阶数 ───────────────────────────────────────────────────────────
    progress(35, "正在选择 ARIMA 阶数")
    if manual_order is not None:
        order = manual_order
    else:
        order = _auto_order(series)   # stepwise，全量数据，AIC 最优

    # ── 拟合 & 预测 ────────────────────────────────────────────────────────
    progress(65, "正在拟合 ARIMA 并预测")
    fitted, forecast_df, residuals, model_result = _fit_predict(series, order, steps)

    # ── 结果表 ─────────────────────────────────────────────────────────────
    progress(88, "正在整理预测结果")
    result_df    = _build_result_df(series, fitted, forecast_df)
    breakdown_df = _build_breakdown_df(residuals)
    metrics_df   = _build_metrics_df(series, fitted, model_result, order, adf_pval)

    # ── Markdown ───────────────────────────────────────────────────────────
    markdown = _build_md(
        target_col  = target_column,
        time_col    = time_col,
        order       = order,
        adf_pval    = adf_pval,
        stationary  = stationary,
        steps       = steps,
        metrics_df  = metrics_df,
        forecast_df = forecast_df,
        series      = series,
    )

    progress(98, "ARIMA 分析计算完成")
    return result_df, breakdown_df, metrics_df, markdown
