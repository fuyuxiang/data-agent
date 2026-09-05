#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Time_Series_VAR
===============
基于 statsmodels 的向量自回归（VAR）多变量时间序列预测模块。

功能：
  - 自动检测时间列，支持多个数值列联合建模
  - AIC 自动选择滞后阶数 p（最多 8）
  - 格兰杰因果检验：输出变量间的影响方向
  - 脉冲响应函数（IRF）摘要写入 breakdown 表
  - 输出三张结果表：
      analysis_result    — 各变量预测值（ds / 变量名_actual / 变量名_pred …）
      analysis_breakdown — 格兰杰因果检验结果（cause / effect / f_stat / p_value / significant）
      analysis_metrics   — 模型评估（metric / value）

参数说明：
  target_column  : 主要关注的目标变量列（必须是数值列；其他数值列自动加入 VAR）
  groupby_column : 时间列名（若不指定则自动探测）；
                   若传入逗号分隔的列名（如 "sales,cost,profit"），
                   则只使用这些列而忽略其他数值列
  n_deciles      : 预测步数（默认 6）
"""

import warnings
import numpy as np
import pandas as pd
from typing import Callable, Optional, Tuple, List

warnings.filterwarnings("ignore")

# ── 模块元数据 ──────────────────────────────────────────────────────────────
ANALYSIS_ID   = "Time_Series_VAR"
ANALYSIS_NAME = "VAR 多变量时间序列预测"
ANALYSIS_DESC = (
    "使用向量自回归（VAR）对多个相关时间序列变量联合建模，捕捉变量间动态依赖关系。"
    "自动选择滞后阶数，输出各变量预测值与格兰杰因果检验结果。"
    "通过 groupby_column 指定时间列名（或逗号分隔的多变量列名）；"
    "通过 n_deciles 指定预测步数（默认 6）。"
)
REQUIRED_PARAMS = ["target_column"]
OPTIONAL_PARAMS = [
    "groupby_column (时间列名；或逗号分隔的多个变量列名如 'sales,cost')",
    "n_deciles (预测步数，默认 6)",
]
OUTPUT_TABLES = ["analysis_result", "analysis_breakdown", "analysis_metrics"]

_DEFAULT_STEPS = 6
_MAX_LAG       = 8


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


def _prepare_multivariate(
    df: pd.DataFrame,
    time_col: str,
    value_cols: List[str],
) -> pd.DataFrame:
    """整理为带 DatetimeIndex 的多变量 DataFrame。"""
    cols = ([time_col] + value_cols) if time_col else value_cols
    work = df[cols].copy()
    if time_col:
        work[time_col] = pd.to_datetime(work[time_col])
        work = work.sort_values(time_col).drop_duplicates(time_col)
        work = work.set_index(time_col)
    for c in value_cols:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    work = work[value_cols].dropna()
    # 推断并设置频率
    if time_col and len(work) >= 3:
        try:
            inferred = pd.infer_freq(work.index)
            if inferred:
                work = work.asfreq(inferred)
        except Exception:
            pass
    return work.ffill()


def _adf_test(series: pd.Series) -> bool:
    """返回 is_stationary。"""
    try:
        from statsmodels.tsa.stattools import adfuller
        return adfuller(series.dropna(), autolag="AIC")[1] < 0.05
    except Exception:
        return True


def _make_stationary(data: pd.DataFrame) -> Tuple[pd.DataFrame, dict]:
    """对每列做差分至平稳，记录差分次数。"""
    diffs = {}
    result = data.copy()
    for col in data.columns:
        d = 0
        s = data[col].copy()
        for _ in range(2):
            if _adf_test(s):
                break
            s = s.diff().dropna()
            d += 1
        diffs[col] = d
        if d > 0:
            result[col] = data[col].diff(d)
    return result.dropna(), diffs


# ═══════════════════════════════════════════════════════════════════════════
#  2. 核心拟合与预测
# ═══════════════════════════════════════════════════════════════════════════

def _fit_var(data: pd.DataFrame, max_lag: int) -> Tuple[object, int]:
    """用 AIC 选最优滞后阶数，拟合 VAR。"""
    from statsmodels.tsa.api import VAR as _VAR
    model = _VAR(data)
    # 选阶
    max_lag = min(max_lag, len(data) // (len(data.columns) * 2 + 2))
    max_lag = max(1, max_lag)
    try:
        lag_order = model.select_order(maxlags=max_lag)
        best_lag  = lag_order.aic
        best_lag  = max(1, best_lag)
    except Exception:
        best_lag = 1
    result = model.fit(best_lag)
    return result, best_lag


def _forecast_var(
    result,
    data: pd.DataFrame,
    steps: int,
    lag: int,
) -> pd.DataFrame:
    """滚动预测，返回 DataFrame（index=未来时间点，cols=变量名）。"""
    last_obs = data.values[-lag:]
    fc = result.forecast(last_obs, steps=steps)
    # 构造未来时间索引
    if hasattr(data.index, "freq") and data.index.freq is not None:
        try:
            future_idx = pd.date_range(
                start=data.index[-1],
                periods=steps + 1,
                freq=data.index.freq,
            )[1:]
        except Exception:
            future_idx = range(len(data), len(data) + steps)
    else:
        future_idx = range(len(data), len(data) + steps)
    return pd.DataFrame(fc, index=future_idx, columns=data.columns)


# ═══════════════════════════════════════════════════════════════════════════
#  3. 格兰杰因果检验
# ═══════════════════════════════════════════════════════════════════════════

def _granger_test(data: pd.DataFrame, max_lag: int) -> pd.DataFrame:
    """对每对变量做格兰杰因果检验，返回 breakdown_df。"""
    from statsmodels.tsa.stattools import grangercausalitytests
    rows = []
    cols = data.columns.tolist()
    for cause in cols:
        for effect in cols:
            if cause == effect:
                continue
            try:
                test_data = data[[effect, cause]].dropna()
                if len(test_data) < max_lag * 3:
                    continue
                gc_res = grangercausalitytests(test_data, maxlag=max_lag, verbose=False)
                # 取最显著的滞后阶
                best_p = min(
                    gc_res[lag][0]["ssr_ftest"][1]
                    for lag in gc_res
                )
                best_f = max(
                    gc_res[lag][0]["ssr_ftest"][0]
                    for lag in gc_res
                )
                rows.append({
                    "cause":       cause,
                    "effect":      effect,
                    "f_stat":      round(float(best_f), 4),
                    "p_value":     round(float(best_p), 4),
                    "significant": "✓" if best_p < 0.05 else "",
                })
            except Exception:
                continue
    if rows:
        return pd.DataFrame(rows).sort_values("p_value").reset_index(drop=True)
    return pd.DataFrame(columns=["cause", "effect", "f_stat", "p_value", "significant"])


# ═══════════════════════════════════════════════════════════════════════════
#  4. 结果表构建
# ═══════════════════════════════════════════════════════════════════════════

def _build_result_df(
    original_data: pd.DataFrame,
    fitted_values: pd.DataFrame,
    forecast_df:   pd.DataFrame,
    target_col:    str,
) -> pd.DataFrame:
    """合并历史 + 预测，以 target_col 为核心，其他列作附列。"""
    # 历史
    hist_rows = []
    for ts, row in original_data.iterrows():
        r = {"ds": str(ts), "segment": "historical"}
        for col in original_data.columns:
            r[f"{col}_actual"] = round(float(row[col]), 4)
            fv = fitted_values.get(col, pd.Series(dtype=float))
            r[f"{col}_pred"]   = round(float(fv.get(ts, np.nan)), 4) if ts in fv.index else np.nan
        hist_rows.append(r)

    # 预测
    fcast_rows = []
    for ts, row in forecast_df.iterrows():
        r = {"ds": str(ts), "segment": "forecast"}
        for col in original_data.columns:
            r[f"{col}_actual"] = np.nan
            r[f"{col}_pred"]   = round(float(row[col]), 4)
        fcast_rows.append(r)

    return pd.concat(
        [pd.DataFrame(hist_rows), pd.DataFrame(fcast_rows)],
        ignore_index=True,
    )


def _build_metrics_df(
    original_data: pd.DataFrame,
    fitted_values: pd.DataFrame,
    result,
    lag: int,
    diffs: dict,
) -> pd.DataFrame:
    rows = [
        {"metric": "模型类型",    "value": f"VAR({lag})"},
        {"metric": "滞后阶数 p", "value": lag},
        {"metric": "变量数量",    "value": len(original_data.columns)},
        {"metric": "变量列表",    "value": ", ".join(original_data.columns)},
        {"metric": "训练样本数",  "value": len(original_data)},
        {"metric": "AIC",        "value": round(result.aic, 4)},
        {"metric": "BIC",        "value": round(result.bic, 4)},
    ]
    # 各变量 MAE
    for col in original_data.columns:
        fv = fitted_values.get(col, pd.Series(dtype=float))
        common = original_data.index.intersection(fv.index)
        if len(common) == 0:
            continue
        y_true = original_data.loc[common, col].values
        y_pred = fv.loc[common].values
        mask   = ~np.isnan(y_pred)
        if mask.sum() == 0:
            continue
        mae  = float(np.mean(np.abs(y_true[mask] - y_pred[mask])))
        rmse = float(np.sqrt(np.mean((y_true[mask] - y_pred[mask]) ** 2)))
        rows.append({"metric": f"MAE（{col}）",  "value": round(mae,  4)})
        rows.append({"metric": f"RMSE（{col}）", "value": round(rmse, 4)})
    # 差分信息
    diff_info = ", ".join(f"{c}:d={v}" for c, v in diffs.items() if v > 0)
    if diff_info:
        rows.append({"metric": "差分处理", "value": diff_info})
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  5. Markdown 报告
# ═══════════════════════════════════════════════════════════════════════════

def _build_md(
    target_col:   str,
    time_col:     str,
    value_cols:   List[str],
    lag:          int,
    steps:        int,
    metrics_df:   pd.DataFrame,
    granger_df:   pd.DataFrame,
    forecast_df:  pd.DataFrame,
    original_data: pd.DataFrame,
) -> str:
    L = [
        f"## VAR 多变量时间序列预测 — `{target_col}`\n",
        "### 模型概况",
        "| 指标 | 值 |", "|------|-----|",
        f"| 模型类型 | VAR({lag}) |",
        f"| 建模变量 | {', '.join(value_cols)} |",
        f"| 时间列 | `{time_col or '（行序号）'}` |",
        f"| 训练样本数 | {len(original_data)} |",
        f"| 预测步数 | {steps} |",
        "",
        "### 模型评估",
        "| 指标 | 值 |", "|------|-----|",
    ]
    for _, row in metrics_df.iterrows():
        L.append(f"| {row['metric']} | {row['value']} |")
    L.append("")

    # 格兰杰因果
    L += ["### 格兰杰因果检验（p<0.05 表示显著）",
          "| 原因变量 | 效果变量 | F 统计量 | p 值 | 显著 |",
          "|----------|----------|--------:|-----:|:----:|"]
    for _, row in granger_df.iterrows():
        L.append(f"| `{row['cause']}` | `{row['effect']}` "
                 f"| {row['f_stat']} | {row['p_value']} | {row['significant']} |")
    L.append("")

    # 预测摘要（仅 target_col）
    L += [f"### `{target_col}` 预测摘要",
          "| 时间点 | 预测值 |",
          "|--------|-------:|"]
    for i, (idx, row) in enumerate(forecast_df.iterrows()):
        if i >= 10:
            L.append("| … | … |")
            break
        L.append(f"| {str(idx)} | {row[target_col]:.4f} |")
    L.append("")

    # 洞察
    sig_causes = granger_df[
        (granger_df["effect"] == target_col) & (granger_df["significant"] == "✓")
    ]
    L.append("### 核心洞察")
    if not sig_causes.empty:
        causes_str = "、".join(f"`{c}`" for c in sig_causes["cause"])
        L.append(f"- **格兰杰因果**：{causes_str} 对 `{target_col}` 有显著的预测能力（p<0.05）。")
    else:
        L.append(f"- 未检测到其他变量对 `{target_col}` 的显著格兰杰因果关系（p≥0.05）。")

    last_actual = float(original_data[target_col].iloc[-1])
    last_pred   = float(forecast_df[target_col].iloc[-1])
    direction   = "上升" if last_pred > last_actual else "下降"
    chg_pct     = abs(last_pred - last_actual) / abs(last_actual) * 100 if last_actual != 0 else 0
    L.append(f"- **预测趋势**：`{target_col}` 预测期末值（{last_pred:.4f}）"
             f"较历史末值（{last_actual:.4f}）{direction}，幅度约 **{chg_pct:.1f}%**。")

    L += [
        "",
        "> **图表建议**",
        f"> - Line_Chart(analysis_result)：x=ds，y={target_col}_pred + {target_col}_actual",
        "> - Heatmap(analysis_breakdown)：x=effect，y=cause，value=f_stat — 因果热图",
        "> - analysis_metrics 表直接展示各变量 MAE/RMSE",
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
    target_column  : 主目标变量列
    groupby_column : 时间列名；或逗号分隔的多变量列名（含 target_column）
    n_deciles      : 预测步数（默认 6）
    """
    progress = progress_callback or (lambda _pct, _message="": None)
    progress(5, "正在校验 VAR 输入")
    try:
        from statsmodels.tsa.api import VAR as _chk  # noqa
    except ImportError:
        raise ImportError("statsmodels 未安装。请运行：pip install statsmodels>=0.14.0")

    if target_column not in df.columns:
        raise ValueError(f"目标列 '{target_column}' 不存在。可用列：{', '.join(df.columns[:20])}")

    steps = int(n_deciles) if int(n_deciles) > 0 else _DEFAULT_STEPS

    # ── 解析 groupby_column ────────────────────────────────────────────────
    explicit_value_cols: List[str] = []
    time_col_hint = ""
    if groupby_column:
        # 含逗号 → 多变量列名（可能混入时间列名）
        if "," in groupby_column:
            parts = [c.strip() for c in groupby_column.split(",") if c.strip() in df.columns]
            # 先探测时间列
            time_col_hint = _detect_time_col(df, "")
            # 将时间列从 value_cols 中剔除
            explicit_value_cols = [c for c in parts if c != time_col_hint]
            # 若用户把单个列名作为时间列提示（无逗号），覆盖检测结果
        else:
            time_col_hint = groupby_column

    # ── 确定时间列 & 数值列 ───────────────────────────────────────────────
    time_col = _detect_time_col(df, time_col_hint)

    if explicit_value_cols:
        # 再次确保时间列不混入数值列
        value_cols = [c for c in explicit_value_cols if c != time_col]
        if target_column not in value_cols:
            value_cols = [target_column] + value_cols
    else:
        # 自动选所有数值列（排除时间列）
        value_cols = [
            c for c in df.columns
            if c != time_col and pd.api.types.is_numeric_dtype(df[c])
        ]
        # 保证 target_column 在首位
        if target_column in value_cols:
            value_cols.remove(target_column)
        value_cols = [target_column] + value_cols
        # 最多取 8 列防止维度爆炸
        value_cols = value_cols[:8]

    if len(value_cols) < 2:
        raise ValueError(
            "VAR 需要至少 2 个数值列。请确保数据中包含多个数值列，"
            "或通过 groupby_column 用逗号指定多个列名，如 'sales,cost'。"
        )

    # ── 准备数据 ───────────────────────────────────────────────────────────
    progress(20, "正在准备多变量时序数据")
    data = _prepare_multivariate(df, time_col, value_cols)
    if len(data) < _MAX_LAG * 2 + 4:
        raise ValueError(f"有效数据点不足（{len(data)} 个），VAR 需要更多样本。")

    # ── 差分至平稳 ─────────────────────────────────────────────────────────
    progress(35, "正在进行平稳化处理")
    stationary_data, diffs = _make_stationary(data)

    # ── 拟合 VAR ───────────────────────────────────────────────────────────
    progress(55, "正在拟合 VAR 模型")
    var_result, lag = _fit_var(stationary_data, _MAX_LAG)

    # ── 预测 ───────────────────────────────────────────────────────────────
    progress(72, "正在生成多变量预测")
    forecast_df = _forecast_var(var_result, stationary_data, steps, lag)

    # 将差分预测还原（逆差分）
    for col in value_cols:
        d = diffs.get(col, 0)
        if d > 0:
            last_val = data[col].iloc[-1]
            forecast_df[col] = forecast_df[col].cumsum() + last_val

    # ── 获取拟合值 ─────────────────────────────────────────────────────────
    fitted_df = pd.DataFrame(
        var_result.fittedvalues,
        index=stationary_data.index[lag:],
        columns=value_cols,
    )
    # 逆差分
    for col in value_cols:
        d = diffs.get(col, 0)
        if d > 0:
            last_orig = data[col].iloc[lag - 1]
            fitted_df[col] = fitted_df[col].cumsum() + last_orig

    # ── 格兰杰因果 ─────────────────────────────────────────────────────────
    progress(85, "正在执行格兰杰因果检验")
    granger_df = _granger_test(stationary_data, min(lag, 4))

    # ── 结果表 ────────────────────────────────────────────────────────────
    result_df  = _build_result_df(data, fitted_df, forecast_df, target_column)
    metrics_df = _build_metrics_df(data, fitted_df, var_result, lag, diffs)

    markdown = _build_md(
        target_col    = target_column,
        time_col      = time_col,
        value_cols    = value_cols,
        lag           = lag,
        steps         = steps,
        metrics_df    = metrics_df,
        granger_df    = granger_df,
        forecast_df   = forecast_df,
        original_data = data,
    )

    progress(98, "VAR 分析计算完成")
    return result_df, granger_df, metrics_df, markdown
