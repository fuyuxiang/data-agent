#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression Analysis
===================
从零实现线性回归与多项式回归（仅依赖 pandas + numpy，无需 scikit-learn）：
  - 简单线性回归  — OLS（最小二乘法）解析解
  - 多元线性回归  — OLS 矩阵解 (X'X)⁻¹X'y，带伪逆保护
  - 多项式回归    — 通过 n_deciles 指定最高次数（默认 1 = 线性）
  - 岭回归（L2） — 通过 groupby_column 传入正则化系数 lambda（默认 0）

特性：
  - 数值特征 Z-score 标准化（消除量纲差异）
  - 类别特征 One-Hot 编码（自动检测字符串列）
  - 缺失值自动填充（数值列填中位数，类别列填众数）
  - 70/30 自动划分训练 / 测试集
  - 输出三张结果表：
      analysis_result    — 系数表（feature / coefficient / std_err / t_stat / p_value / vif）→ Bar_Chart
      analysis_breakdown — 残差诊断表（y_actual / y_pred / residual / std_residual）→ Scatter_Plot
      analysis_metrics   — 综合评估指标（metric / train_value / test_value）→ 直接展示
"""

import numpy as np
import pandas as pd
from typing import Any, Dict, List, Optional, Tuple


# ── 模块元数据（供 registry 读取）─────────────────────────────────────────

ANALYSIS_ID   = "Regression"
ANALYSIS_NAME = "线性回归分析（Linear Regression）"
ANALYSIS_DESC = (
    "使用最小二乘法（OLS）进行线性回归或多项式回归，支持多元特征与岭回归正则化。"
    "输出系数表（含 t 统计量与 p 值）、残差诊断表及 R² / RMSE / MAE 等评估指标。"
    "通过 groupby_column 参数传入岭回归系数 lambda（默认 0，即普通 OLS）；"
    "通过 n_deciles 参数传入多项式阶数（默认 1，即线性回归）。"
)
REQUIRED_PARAMS = ["target_column"]
OPTIONAL_PARAMS = [
    "groupby_column (ridge lambda, default 0 = plain OLS)",
    "n_deciles (polynomial degree, default 1 = linear)",
]
OUTPUT_TABLES = ["analysis_result", "analysis_breakdown", "analysis_metrics"]

_DEFAULT_LAMBDA = 0.0   # 岭回归正则化系数
_DEFAULT_DEGREE = 1     # 多项式阶数
_MIN_ROWS       = 6     # 数据不足此行时不拆分训练/测试集
_MAX_ONEHOT     = 20    # 单列 One-Hot 最大类别数


# ═══════════════════════════════════════════════════════════════════════════
#  1. 数据预处理
# ═══════════════════════════════════════════════════════════════════════════

def _fill_missing(df: pd.DataFrame, target_col: str) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if col == target_col:
            continue
        if df[col].isna().any():
            if pd.api.types.is_numeric_dtype(df[col]):
                df[col] = df[col].fillna(df[col].median())
            else:
                mode = df[col].mode()
                df[col] = df[col].fillna(mode.iloc[0] if not mode.empty else "unknown")
    if df[target_col].isna().any():
        df[target_col] = df[target_col].fillna(df[target_col].median())
    return df


def _split(
    df: pd.DataFrame, test_size: float = 0.3, seed: int = 42
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if len(df) < _MIN_ROWS:
        return df, df
    n_test = max(1, int(len(df) * test_size))
    test  = df.sample(n=n_test, random_state=seed)
    train = df.drop(test.index)
    return train, test


def _encode_features(
    train_df: pd.DataFrame,
    test_df:  pd.DataFrame,
    target_col: str,
    degree: int,
) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """
    对特征列做 One-Hot（类别）+ Z-score（数值）编码，然后扩展多项式项。

    Returns
    -------
    X_train, X_test : float64 ndarray，已包含截距列（全 1）
    feat_names      : 特征名列表（对应 X 的列，不含截距）
    """
    feature_cols = [c for c in train_df.columns if c != target_col]
    encoded_train_parts: List[pd.DataFrame] = []
    encoded_test_parts:  List[pd.DataFrame] = []
    base_feat_names: List[str] = []

    for col in feature_cols:
        if pd.api.types.is_numeric_dtype(train_df[col]):
            mu  = train_df[col].mean()
            std = train_df[col].std() or 1.0
            enc_tr = pd.DataFrame(
                {col: (train_df[col] - mu) / std}, index=train_df.index
            )
            enc_te = pd.DataFrame(
                {col: (test_df[col] - mu) / std}, index=test_df.index
            )
            encoded_train_parts.append(enc_tr)
            encoded_test_parts.append(enc_te)
            base_feat_names.append(col)
        else:
            n_uniq = train_df[col].nunique()
            if n_uniq < 2 or n_uniq > _MAX_ONEHOT:
                continue
            cats = sorted(train_df[col].dropna().unique().tolist())
            for cat in cats[1:]:
                name = f"{col}_{cat}"
                encoded_train_parts.append(
                    pd.DataFrame(
                        {name: (train_df[col] == cat).astype(float)},
                        index=train_df.index,
                    )
                )
                encoded_test_parts.append(
                    pd.DataFrame(
                        {name: (test_df[col] == cat).astype(float)},
                        index=test_df.index,
                    )
                )
                base_feat_names.append(name)

    if not base_feat_names:
        raise ValueError("预处理后无有效特征列，请检查输入数据。")

    X_tr_base = pd.concat(encoded_train_parts, axis=1).values.astype(np.float64)
    X_te_base = pd.concat(encoded_test_parts,  axis=1).values.astype(np.float64)

    # ── 多项式扩展（仅对数值型基础特征；One-Hot 特征不扩展）──────────────
    if degree > 1:
        n_base = len(base_feat_names)
        poly_tr_parts = [X_tr_base]
        poly_te_parts = [X_te_base]
        poly_names    = list(base_feat_names)
        for d in range(2, degree + 1):
            # 只对原始数值列做幂次扩展（前 n_base 列中为数值的部分）
            for i, fn in enumerate(base_feat_names[:n_base]):
                if "_" not in fn or not any(c.isdigit() for c in fn):
                    new_name = f"{fn}^{d}"
                    poly_tr_parts.append((X_tr_base[:, i:i+1] ** d))
                    poly_te_parts.append((X_te_base[:, i:i+1] ** d))
                    poly_names.append(new_name)
        X_tr_final = np.hstack(poly_tr_parts)
        X_te_final = np.hstack(poly_te_parts)
        feat_names = poly_names
    else:
        X_tr_final = X_tr_base
        X_te_final = X_te_base
        feat_names = base_feat_names

    # 在最左侧添加截距列（全 1）
    ones_tr = np.ones((X_tr_final.shape[0], 1))
    ones_te = np.ones((X_te_final.shape[0], 1))
    X_tr_final = np.hstack([ones_tr, X_tr_final])
    X_te_final = np.hstack([ones_te, X_te_final])

    return X_tr_final, X_te_final, feat_names


# ═══════════════════════════════════════════════════════════════════════════
#  2. 核心 OLS / Ridge 求解
# ═══════════════════════════════════════════════════════════════════════════

def _fit_ridge(X: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    """
    岭回归解析解：w = (X'X + λI)⁻¹ X'y
    截距不正则化（λ 仅作用于特征权重，I[0,0] 设为 0）。

    Returns
    -------
    w : (n_features+1,) 权重向量（含截距）
    """
    n, d = X.shape
    reg = lam * np.eye(d)
    reg[0, 0] = 0.0          # 截距不正则化
    XtX = X.T @ X + reg
    Xty = X.T @ y
    try:
        w = np.linalg.solve(XtX, Xty)
    except np.linalg.LinAlgError:
        w = np.linalg.lstsq(XtX, Xty, rcond=None)[0]
    return w


def _predict(X: np.ndarray, w: np.ndarray) -> np.ndarray:
    return X @ w


# ═══════════════════════════════════════════════════════════════════════════
#  3. 统计量计算
# ═══════════════════════════════════════════════════════════════════════════

def _r2(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    ss_res = float(np.sum((y_true - y_pred) ** 2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0


def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)))


def _mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    mask = y_true != 0
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100)


def _coef_stats(
    X: np.ndarray,
    y: np.ndarray,
    w: np.ndarray,
    lam: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    计算 OLS 条件下的系数标准误差、t 统计量和 p 值。
    岭回归下 p 值仅供参考（不严格成立）。

    Returns
    -------
    std_errs : (d,)
    t_stats  : (d,)
    p_values : (d,)
    """
    n, d = X.shape
    y_pred = X @ w
    residuals = y - y_pred
    dof = max(n - d, 1)
    s2  = float(np.sum(residuals ** 2) / dof)

    reg = lam * np.eye(d)
    reg[0, 0] = 0.0
    XtX = X.T @ X + reg
    try:
        cov = s2 * np.linalg.inv(XtX)
    except np.linalg.LinAlgError:
        cov = s2 * np.linalg.pinv(XtX)

    var_diag = np.diag(cov)
    var_diag = np.where(var_diag < 0, 0.0, var_diag)
    std_errs = np.sqrt(var_diag)
    t_stats  = np.where(std_errs > 0, w / std_errs, 0.0)

    # 双尾 t 检验 p 值（近似正态）
    p_values = 2.0 * (1.0 - _norm_cdf(np.abs(t_stats)))
    return std_errs, t_stats, p_values


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    """标准正态 CDF 近似（无需 scipy）。"""
    return 0.5 * (1.0 + _erf(x / np.sqrt(2.0)))


def _erf(x: np.ndarray) -> np.ndarray:
    """Abramowitz & Stegun 7.1.26 近似，误差 < 1.5e-7。"""
    t = 1.0 / (1.0 + 0.3275911 * np.abs(x))
    poly = t * (0.254829592
                + t * (-0.284496736
                       + t * (1.421413741
                              + t * (-1.453152027
                                     + t * 1.061405429))))
    sign = np.where(x >= 0, 1.0, -1.0)
    return sign * (1.0 - poly * np.exp(-(x ** 2)))


def _vif(X: np.ndarray) -> np.ndarray:
    """
    方差膨胀因子（VIF）：VIF_j = 1 / (1 - R²_j)
    用各特征对其余特征做 OLS 回归，X 列从下标 1 开始（跳过截距列）。
    """
    _, d = X.shape
    vifs = np.ones(d - 1)
    Xf   = X[:, 1:]          # 去掉截距列
    n_f  = Xf.shape[1]
    for j in range(n_f):
        y_j  = Xf[:, j]
        X_j  = np.delete(Xf, j, axis=1)
        ones = np.ones((X_j.shape[0], 1))
        X_j  = np.hstack([ones, X_j])
        w_j  = _fit_ridge(X_j, y_j, 0.0)
        r2_j = _r2(y_j, X_j @ w_j)
        denom = 1.0 - r2_j
        vifs[j] = (1.0 / denom) if denom > 1e-10 else 999.0
    return vifs


# ═══════════════════════════════════════════════════════════════════════════
#  4. 结果表构建
# ═══════════════════════════════════════════════════════════════════════════

def _build_coef_df(
    feat_names: List[str],
    w: np.ndarray,
    std_errs: np.ndarray,
    t_stats: np.ndarray,
    p_values: np.ndarray,
    vifs: np.ndarray,
) -> pd.DataFrame:
    """
    系数表（含截距）。
    Columns: rank / feature / coefficient / std_err / t_stat / p_value / significant / vif
    """
    rows = []
    for i, fn in enumerate(["(intercept)"] + feat_names):
        idx = i  # w[0] = 截距，w[1:] = 特征
        vif_val = float(vifs[i - 1]) if i >= 1 else float("nan")
        rows.append({
            "feature":     fn,
            "coefficient": round(float(w[idx]), 6),
            "std_err":     round(float(std_errs[idx]), 6),
            "t_stat":      round(float(t_stats[idx]), 4),
            "p_value":     round(float(p_values[idx]), 4),
            "significant": "✓" if float(p_values[idx]) < 0.05 else "",
            "vif":         round(vif_val, 2) if not np.isnan(vif_val) else None,
        })

    df = pd.DataFrame(rows)
    # 按 |t_stat| 排序（截距排最后）
    intercept_row = df[df["feature"] == "(intercept)"]
    feat_rows     = df[df["feature"] != "(intercept)"].copy()
    feat_rows["_abs_t"] = feat_rows["t_stat"].abs()
    feat_rows = feat_rows.sort_values("_abs_t", ascending=False).drop(columns="_abs_t")
    feat_rows.insert(0, "rank", range(1, len(feat_rows) + 1))
    intercept_row = intercept_row.copy()
    intercept_row.insert(0, "rank", 0)
    result = pd.concat([feat_rows, intercept_row], ignore_index=True)
    return result[["rank", "feature", "coefficient", "std_err", "t_stat", "p_value", "significant", "vif"]]


def _build_residual_df(
    y_true: np.ndarray,
    y_pred: np.ndarray,
) -> pd.DataFrame:
    """残差诊断表。"""
    residuals = y_true - y_pred
    std_res_denom = residuals.std() or 1.0
    std_residuals = residuals / std_res_denom
    return pd.DataFrame({
        "y_actual":     np.round(y_true, 6),
        "y_pred":       np.round(y_pred, 6),
        "residual":     np.round(residuals, 6),
        "std_residual": np.round(std_residuals, 4),
    })


def _build_metrics_df(
    train_r2: float, test_r2: float,
    train_rmse: float, test_rmse: float,
    train_mae: float, test_mae: float,
    train_mape: float, test_mape: float,
    n_train: int, n_test: int,
    n_features: int, lam: float, degree: int,
) -> pd.DataFrame:
    rows = [
        {"metric": "R²",         "train_value": round(train_r2,   4), "test_value": round(test_r2,   4)},
        {"metric": "RMSE",       "train_value": round(train_rmse, 4), "test_value": round(test_rmse, 4)},
        {"metric": "MAE",        "train_value": round(train_mae,  4), "test_value": round(test_mae,  4)},
        {"metric": "MAPE (%)",   "train_value": round(train_mape, 2) if not np.isnan(train_mape) else None,
                                  "test_value":  round(test_mape,  2) if not np.isnan(test_mape)  else None},
        {"metric": "N Samples",  "train_value": n_train,  "test_value": n_test},
        {"metric": "N Features", "train_value": n_features, "test_value": n_features},
        {"metric": "Ridge λ",    "train_value": lam,      "test_value": lam},
        {"metric": "Poly Degree","train_value": degree,   "test_value": degree},
    ]
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  5. Markdown 报告
# ═══════════════════════════════════════════════════════════════════════════

def _build_md(
    target_col:  str,
    degree:      int,
    lam:         float,
    n_train:     int,
    n_test:      int,
    n_features:  int,
    train_r2:    float,
    test_r2:     float,
    train_rmse:  float,
    test_rmse:   float,
    coef_df:     pd.DataFrame,
) -> str:
    mode = f"多项式回归（阶数 {degree}）" if degree > 1 else "多元线性回归（OLS）"
    if lam > 0:
        mode += f" + 岭回归（λ={lam}）"

    L = [
        f"## 线性回归分析 - `{target_col}`\n",
        "### 模型概况",
        "| 指标 | 值 |", "|------|-----|",
        f"| 回归类型 | {mode} |",
        f"| 训练样本 | {n_train} |",
        f"| 测试样本 | {n_test} |",
        f"| 特征数量 | {n_features} |",
        f"| Ridge λ  | {lam} |",
        f"| 多项式阶数 | {degree} |",
        "",
        "### 拟合优度",
        "| 指标 | 训练集 | 测试集 |",
        "|------|--------|--------|",
        f"| **R²** | **{train_r2:.4f}** | **{test_r2:.4f}** |",
        f"| RMSE  | {train_rmse:.4f} | {test_rmse:.4f} |",
        "",
    ]

    # ── 系数表（前 10）──────────────────────────────────────────────────────
    feat_only = coef_df[coef_df["feature"] != "(intercept)"].head(10)
    L += [
        "### 特征系数（按 |t| 排序，前 10）",
        "| 排名 | 特征 | 系数 | t 统计量 | p 值 | 显著 | VIF |",
        "|:----:|------|-----:|--------:|-----:|:----:|----:|",
    ]
    for _, row in feat_only.iterrows():
        vif_str = f"{row['vif']:.1f}" if row["vif"] is not None else "—"
        L.append(
            f"| {int(row['rank'])} | `{row['feature']}` "
            f"| {row['coefficient']:+.4f} | {row['t_stat']:.4f} "
            f"| {row['p_value']:.4f} | {row['significant']} | {vif_str} |"
        )
    L.append("")

    # ── 核心洞察 ────────────────────────────────────────────────────────────
    sig_feats = feat_only[feat_only["significant"] == "✓"]
    L.append("### 核心洞察")

    if test_r2 >= 0.8:
        L.append(f"- 测试集 R² = **{test_r2:.4f}**，模型拟合优秀，可解释 {test_r2:.1%} 的目标方差。")
    elif test_r2 >= 0.5:
        L.append(f"- 测试集 R² = **{test_r2:.4f}**，模型有一定解释力，可考虑增加更多特征或提高多项式阶数。")
    else:
        L.append(f"- 测试集 R² = **{test_r2:.4f}**，线性关系较弱，建议检查数据或尝试非线性模型。")

    overfit_gap = train_r2 - test_r2
    if overfit_gap > 0.15:
        L.append(
            f"- 过拟合警告：训练/测试 R² 相差 {overfit_gap:.4f}，"
            f"建议增大岭回归系数（groupby_column 参数）或减少多项式阶数。"
        )

    if not sig_feats.empty:
        top = sig_feats.iloc[0]
        direction = "正向" if top["coefficient"] > 0 else "负向"
        L.append(
            f"- 最显著特征：`{top['feature']}`（系数 {top['coefficient']:+.4f}，"
            f"p={top['p_value']:.4f}，{direction}影响目标变量）。"
        )
    else:
        L.append("- 无特征在 p<0.05 水平上显著，建议检查数据质量或特征选择。")

    # VIF 多重共线性警告
    high_vif = feat_only[feat_only["vif"].apply(lambda v: v is not None and v > 10)]
    if not high_vif.empty:
        names = ", ".join(f"`{r['feature']}`" for _, r in high_vif.iterrows())
        L.append(f"- 多重共线性警告：{names} VIF > 10，建议考虑删除冗余特征或使用岭回归。")

    L += [
        "",
        "> 系数表存储于 `analysis_result`，绘图方法：Bar_Chart，x=feature，y=coefficient。",
        "> 残差诊断数据存储于 `analysis_breakdown`，绘图方法：Scatter_Plot，x=y_pred，y=std_residual。",
        "> 综合指标存储于 `analysis_metrics`（metric / train_value / test_value）。",
        "",
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
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    """
    运行线性回归分析。

    Parameters
    ----------
    df             : 原始数据 DataFrame
    target_column  : 目标列（连续数值）
    groupby_column : 岭回归系数 lambda（字符串，默认 '0'）
    n_deciles      : 多项式阶数（默认 1，传 0 时使用默认值 1）

    Returns
    -------
    coef_df      : 系数表                  → analysis_result
    residual_df  : 残差诊断表              → analysis_breakdown
    metrics_df   : 综合评估指标表          → analysis_metrics
    markdown     : Markdown 分析报告
    """
    # ── 参数解析 ──────────────────────────────────────────────────────────
    try:
        lam = float(groupby_column) if groupby_column else _DEFAULT_LAMBDA
        if lam < 0:
            lam = _DEFAULT_LAMBDA
    except (ValueError, TypeError):
        lam = _DEFAULT_LAMBDA

    degree = int(n_deciles) if int(n_deciles) > 0 else _DEFAULT_DEGREE
    degree = max(1, min(degree, 5))  # 阶数限制 1-5

    if target_column not in df.columns:
        raise ValueError(
            f"目标列 '{target_column}' 不存在。"
            f"可用列：{', '.join(df.columns[:20])}"
        )

    if not pd.api.types.is_numeric_dtype(df[target_column]):
        raise ValueError(
            f"目标列 '{target_column}' 不是数值类型，线性回归要求连续数值目标变量。"
            f"若需分类分析，请使用 Logistic_Regression 模块。"
        )

    # ── 预处理 ────────────────────────────────────────────────────────────
    df = _fill_missing(df, target_column)

    if len(df) < 3:
        raise ValueError(f"数据行数不足（{len(df)} 行），无法进行回归分析。")

    # ── 训练 / 测试分割 ───────────────────────────────────────────────────
    train_df, test_df = _split(df)

    # ── 特征编码 ──────────────────────────────────────────────────────────
    X_train, X_test, feat_names = _encode_features(train_df, test_df, target_column, degree)
    y_train = train_df[target_column].values.astype(np.float64)
    y_test  = test_df[target_column].values.astype(np.float64)

    # ── 模型拟合 ──────────────────────────────────────────────────────────
    w = _fit_ridge(X_train, y_train, lam)

    # ── 预测 ──────────────────────────────────────────────────────────────
    y_train_pred = _predict(X_train, w)
    y_test_pred  = _predict(X_test,  w)

    # ── 统计量 ────────────────────────────────────────────────────────────
    std_errs, t_stats, p_values = _coef_stats(X_train, y_train, w, lam)

    vifs_arr = np.full(len(feat_names), float("nan"))
    if len(feat_names) >= 2 and X_train.shape[0] > X_train.shape[1]:
        try:
            vifs_arr = _vif(X_train)
        except Exception:
            pass

    # ── 评估指标 ──────────────────────────────────────────────────────────
    train_r2   = _r2(y_train, y_train_pred)
    test_r2    = _r2(y_test,  y_test_pred)
    train_rmse = _rmse(y_train, y_train_pred)
    test_rmse  = _rmse(y_test,  y_test_pred)
    train_mae  = _mae(y_train, y_train_pred)
    test_mae   = _mae(y_test,  y_test_pred)
    train_mape = _mape(y_train, y_train_pred)
    test_mape  = _mape(y_test,  y_test_pred)

    # ── 结果表 ────────────────────────────────────────────────────────────
    coef_df_out    = _build_coef_df(feat_names, w, std_errs, t_stats, p_values, vifs_arr)
    residual_df    = _build_residual_df(y_test, y_test_pred)
    metrics_df     = _build_metrics_df(
        train_r2, test_r2, train_rmse, test_rmse,
        train_mae, test_mae, train_mape, test_mape,
        len(train_df), len(test_df), len(feat_names), lam, degree,
    )

    # ── Markdown 报告 ─────────────────────────────────────────────────────
    markdown = _build_md(
        target_col  = target_column,
        degree      = degree,
        lam         = lam,
        n_train     = len(train_df),
        n_test      = len(test_df),
        n_features  = len(feat_names),
        train_r2    = train_r2,
        test_r2     = test_r2,
        train_rmse  = train_rmse,
        test_rmse   = test_rmse,
        coef_df     = coef_df_out,
    )

    return coef_df_out, residual_df, metrics_df, markdown
