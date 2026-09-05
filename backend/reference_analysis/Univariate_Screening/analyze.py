#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Univariate Screening Regression
================================
对每个候选自变量分别与目标变量（被解释变量）做简单 OLS 回归，
输出系数、标准误、t 统计量、p 值、R²，并按显著性分类汇总。

用途：
  - 单变量筛选（找出显著影响目标变量的候选解释变量）
  - 多元回归建模前的变量预筛
  - 探索性分析（了解哪些变量与目标有显著相关性）

参数：
  target_column  — 被解释变量列名（必填）
  groupby_column — 显著性阈值，如 "0.05"（默认 "0.05"）
  n_deciles      — 未使用，保留以兼容通用接口

输出表：
  analysis_result    — 所有变量汇总（含显著 + 不显著）
  analysis_breakdown — 仅显著变量（按 p 值升序）
  analysis_metrics   — 统计摘要（候选变量数、显著数、最高 R²）
"""

import numpy as np
import pandas as pd
from typing import Any, Dict, Optional, Tuple

# ── 模块元数据 ─────────────────────────────────────────────────────────────
ANALYSIS_ID   = "Univariate_Screening"
ANALYSIS_NAME = "逐变量回归筛选（Univariate Screening）"
ANALYSIS_DESC = (
    "对每个候选自变量分别与目标变量做简单 OLS 回归，输出系数、标准误、t 统计量、"
    "p 值、R²，并按显著性（默认 p<0.05）分类汇总。适用于多元建模前的变量预筛选。"
)
REQUIRED_PARAMS = ["target_column"]
OPTIONAL_PARAMS = [
    "groupby_column (significance threshold, default '0.05')",
]
OUTPUT_TABLES = ["analysis_result", "analysis_breakdown", "analysis_metrics"]

_DEFAULT_ALPHA = 0.05
_MIN_OBS       = 10   # 有效观测数低于此值时跳过该变量


# ── OLS 工具函数 ───────────────────────────────────────────────────────────

def _ols_simple(y: np.ndarray, x: np.ndarray) -> Dict[str, float]:
    """单变量 OLS：y ~ α + β·x，返回系数及统计量。"""
    mask = ~(np.isnan(y) | np.isnan(x))
    y, x = y[mask], x[mask]
    n = len(y)
    if n < _MIN_OBS:
        return {}

    # 设计矩阵 [1, x]
    X = np.column_stack([np.ones(n), x])
    try:
        XtX_inv = np.linalg.pinv(X.T @ X)
    except Exception:
        return {}

    beta = XtX_inv @ X.T @ y          # [intercept, slope]
    y_hat = X @ beta
    residuals = y - y_hat

    # R²
    ss_res = float(residuals @ residuals)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0

    # 标准误（基于残差方差）
    df_resid = n - 2
    if df_resid <= 0:
        return {}
    s2 = ss_res / df_resid
    var_beta = s2 * XtX_inv
    se_slope = float(np.sqrt(max(var_beta[1, 1], 0)))

    slope = float(beta[1])
    intercept = float(beta[0])

    # t 统计量与 p 值
    t_stat = slope / se_slope if se_slope > 1e-12 else np.inf
    from scipy import stats as _stats
    p_val = float(2 * _stats.t.sf(abs(t_stat), df=df_resid))

    return {
        "intercept": intercept,
        "coefficient": slope,
        "std_err":     se_slope,
        "t_stat":      t_stat,
        "p_value":     p_val,
        "r_squared":   r2,
        "n_obs":       n,
    }


def _try_scipy(y: np.ndarray, x: np.ndarray) -> Dict[str, float]:
    """尝试使用 scipy.stats.linregress（精度更高）；失败则回退到手动 OLS。"""
    try:
        from scipy import stats as _sp
        mask = ~(np.isnan(y) | np.isnan(x))
        yv, xv = y[mask], x[mask]
        if len(yv) < _MIN_OBS:
            return {}
        res = _sp.linregress(xv, yv)
        return {
            "intercept":   float(res.intercept),
            "coefficient": float(res.slope),
            "std_err":     float(res.stderr),
            "t_stat":      float(res.slope / res.stderr) if res.stderr else np.inf,
            "p_value":     float(res.pvalue),
            "r_squared":   float(res.rvalue ** 2),
            "n_obs":       int(mask.sum()),
        }
    except Exception:
        return _ols_simple(y, x)


def _significance_stars(p: float) -> str:
    if p < 0.001: return "***"
    if p < 0.01:  return "**"
    if p < 0.05:  return "*"
    if p < 0.1:   return "."
    return ""


# ── 主入口 ────────────────────────────────────────────────────────────────

def run(
    df: pd.DataFrame,
    target_column: str,
    groupby_column: Optional[str] = None,
    n_deciles: int = 0,
    **kwargs: Any,
) -> Dict[str, pd.DataFrame]:
    """
    Parameters
    ----------
    df             : 输入数据表
    target_column  : 被解释变量列名
    groupby_column : 显著性阈值字符串（如 "0.05"，默认 0.05）
    n_deciles      : 未使用
    """
    # ── 解析显著性阈值 ─────────────────────────────────────────────────────
    try:
        alpha = float(groupby_column) if groupby_column else _DEFAULT_ALPHA
    except (ValueError, TypeError):
        alpha = _DEFAULT_ALPHA
    alpha = max(0.001, min(alpha, 0.5))   # 限制在合理范围

    # ── 校验目标变量 ───────────────────────────────────────────────────────
    if target_column not in df.columns:
        raise ValueError(f"目标变量 '{target_column}' 不在数据表中。"
                         f"可用列：{list(df.columns)}")

    y_raw = pd.to_numeric(df[target_column], errors="coerce")
    if y_raw.isna().all():
        raise ValueError(f"目标变量 '{target_column}' 无有效数值。")

    y = y_raw.to_numpy(dtype=float)

    # ── 候选自变量：所有数值列，排除目标变量 ─────────────────────────────
    candidate_cols = [
        c for c in df.columns
        if c != target_column
        and pd.api.types.is_numeric_dtype(df[c])
        and not df[c].isna().all()
    ]
    if not candidate_cols:
        raise ValueError("数据中没有可用的数值型候选自变量（已排除目标变量）。")

    # ── 逐变量回归 ────────────────────────────────────────────────────────
    rows = []
    for col in candidate_cols:
        x = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
        stats = _try_scipy(y, x)
        if not stats:
            rows.append({
                "variable":    col,
                "coefficient": None,
                "std_err":     None,
                "t_stat":      None,
                "p_value":     None,
                "r_squared":   None,
                "n_obs":       int((~np.isnan(x)).sum()),
                "significant": False,
                "stars":       "",
                "direction":   "",
            })
            continue

        p = stats["p_value"]
        coef = stats["coefficient"]
        rows.append({
            "variable":    col,
            "coefficient": round(coef, 6),
            "std_err":     round(stats["std_err"], 6),
            "t_stat":      round(stats["t_stat"], 4),
            "p_value":     round(p, 6),
            "r_squared":   round(stats["r_squared"], 6),
            "n_obs":       stats["n_obs"],
            "significant": p < alpha,
            "stars":       _significance_stars(p),
            "direction":   "+" if coef > 0 else "-",
        })

    # ── 构建输出表 ────────────────────────────────────────────────────────
    result_df = pd.DataFrame(rows).sort_values("p_value", na_position="last")

    # analysis_result — 全量结果
    analysis_result = result_df[[
        "variable", "coefficient", "std_err", "t_stat",
        "p_value", "stars", "r_squared", "n_obs", "direction",
    ]].copy()
    analysis_result.columns = [
        "变量", "系数", "标准误", "t统计量",
        "p值", "显著性", "R²", "有效观测数", "方向",
    ]

    # analysis_breakdown — 仅显著变量（p < alpha），按 p 值升序
    sig_df = result_df[result_df["significant"]].copy()
    if sig_df.empty:
        analysis_breakdown = pd.DataFrame(columns=analysis_result.columns)
    else:
        analysis_breakdown = sig_df[[
            "variable", "coefficient", "std_err", "t_stat",
            "p_value", "stars", "r_squared", "n_obs", "direction",
        ]].copy()
        analysis_breakdown.columns = analysis_result.columns

    # analysis_metrics — 摘要统计
    n_total     = len(result_df)
    n_sig       = int(result_df["significant"].sum())
    n_insig     = n_total - n_sig
    best_r2_row = result_df.dropna(subset=["r_squared"]).nlargest(1, "r_squared")
    best_var    = best_r2_row["variable"].iloc[0] if not best_r2_row.empty else "—"
    best_r2     = round(best_r2_row["r_squared"].iloc[0], 6) if not best_r2_row.empty else None

    analysis_metrics = pd.DataFrame([
        {"指标": "目标变量",          "值": target_column},
        {"指标": "显著性阈值 α",       "值": str(alpha)},
        {"指标": "候选自变量总数",      "值": str(n_total)},
        {"指标": f"显著变量数 (p<{alpha})", "值": str(n_sig)},
        {"指标": "不显著变量数",        "值": str(n_insig)},
        {"指标": "最高单变量 R²",       "值": str(best_r2)},
        {"指标": "R² 最高变量",         "值": best_var},
    ])

    return {
        "analysis_result":    analysis_result,
        "analysis_breakdown": analysis_breakdown,
        "analysis_metrics":   analysis_metrics,
    }
