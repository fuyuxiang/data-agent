#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Logistic_Regression Analysis
=============================
从零实现二元 & 多分类逻辑回归（仅依赖 pandas + numpy，无需 scikit-learn）：
  - 二分类  — sigmoid + 二元交叉熵，批量梯度下降
  - 多分类  — softmax + OvR（One-vs-Rest）策略，自动检测

特性：
  - 数值特征 Z-score 标准化（消除量纲差异）
  - 类别特征 One-Hot 编码（自动检测字符串列）
  - L2 正则化（lambda 默认 0.01）防过拟合
  - 70/30 自动划分训练 / 测试集
  - 输出三张结果表：
      analysis_result    — 系数表（特征 / 系数 / OR 值 / 重要性占比）→ Bar_Chart
      analysis_breakdown — 混淆矩阵长格式（actual / predicted / count）→ Heatmap
      analysis_roc       — ROC 曲线点序列（class / fpr / tpr / auc）→ Line_Chart
"""

import numpy as np
import pandas as pd
from typing import Any, Dict, List, Optional, Tuple


# ── 模块元数据（供 registry 读取）─────────────────────────────────────────

ANALYSIS_ID   = "Logistic_Regression"
ANALYSIS_NAME = "逻辑回归分析（Logistic Regression）"
ANALYSIS_DESC = (
    "使用逻辑回归对数据进行二分类或多分类分析（OvR 策略）。"
    "输出特征系数与重要性、训练/测试准确率、混淆矩阵及 ROC 曲线（含 AUC）。"
    "通过 groupby_column 参数传入正则化强度（lambda，默认 0.01）；"
    "通过 n_deciles 参数传入最大迭代次数（默认 1000）。"
)
REQUIRED_PARAMS = ["target_column"]
OPTIONAL_PARAMS = [
    "groupby_column (L2 regularization lambda, default 0.01)",
    "n_deciles (max_iter: default 1000)",
]
OUTPUT_TABLES = ["analysis_result", "analysis_breakdown", "analysis_roc"]

_LEARNING_RATE  = 0.1    # 梯度下降学习率
_DEFAULT_LAMBDA = 0.01   # L2 正则化系数
_DEFAULT_ITER   = 1000   # 最大迭代次数
_MIN_ROWS       = 6      # 数据不足此行时不拆分训练/测试集
_MAX_ONEHOT     = 20     # 单列 One-Hot 最大类别数（超过则跳过）


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
    df[target_col] = df[target_col].fillna("unknown").astype(str)
    return df


def _encode_features(
    train_df: pd.DataFrame,
    test_df:  pd.DataFrame,
    target_col: str,
) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """
    对特征列做 One-Hot（类别）+ Z-score（数值）编码。

    Returns
    -------
    X_train, X_test : float64 ndarray，已包含截距列（全 1）
    feat_names      : 特征名列表（对应 X 的列，不含截距）
    """
    feature_cols = [c for c in train_df.columns if c != target_col]
    encoded_train_parts: List[pd.DataFrame] = []
    encoded_test_parts:  List[pd.DataFrame] = []
    feat_names: List[str] = []

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
            feat_names.append(col)
        else:
            n_uniq = train_df[col].nunique()
            if n_uniq < 2 or n_uniq > _MAX_ONEHOT:
                continue
            # 使用训练集的类别集合，避免测试集引入新类别
            cats = sorted(train_df[col].dropna().unique().tolist())
            for cat in cats[1:]:  # 去掉第一个类别（基准类）防多重共线
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
                feat_names.append(name)

    if not feat_names:
        raise ValueError("预处理后无有效特征列，请检查输入数据。")

    X_tr = pd.concat(encoded_train_parts, axis=1).values.astype(np.float64)
    X_te = pd.concat(encoded_test_parts,  axis=1).values.astype(np.float64)

    # 在最左侧添加截距列（全 1）
    ones_tr = np.ones((X_tr.shape[0], 1))
    ones_te = np.ones((X_te.shape[0], 1))
    X_tr = np.hstack([ones_tr, X_tr])
    X_te = np.hstack([ones_te, X_te])

    return X_tr, X_te, feat_names


def _encode_labels(
    train_series: pd.Series,
    test_series:  pd.Series,
    classes: List[str],
) -> Tuple[np.ndarray, np.ndarray]:
    """将字符串标签转为整数索引。"""
    cls2idx = {c: i for i, c in enumerate(classes)}
    y_tr = np.array([cls2idx.get(str(v), 0) for v in train_series])
    y_te = np.array([cls2idx.get(str(v), 0) for v in test_series])
    return y_tr, y_te


def _split(
    df: pd.DataFrame, test_size: float = 0.3, seed: int = 42
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if len(df) < _MIN_ROWS:
        return df, df
    n_test = max(1, int(len(df) * test_size))
    test  = df.sample(n=n_test, random_state=seed)
    train = df.drop(test.index)
    return train, test


# ═══════════════════════════════════════════════════════════════════════════
#  2. 核心模型（批量梯度下降）
# ═══════════════════════════════════════════════════════════════════════════

def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))


def _softmax(z: np.ndarray) -> np.ndarray:
    """z: (n_samples, n_classes)"""
    z_shifted = z - z.max(axis=1, keepdims=True)
    e = np.exp(z_shifted)
    return e / e.sum(axis=1, keepdims=True)


def _fit_binary(
    X: np.ndarray,
    y: np.ndarray,
    lam: float,
    max_iter: int,
    lr: float = _LEARNING_RATE,
) -> np.ndarray:
    """
    二分类梯度下降（L2 正则化）。

    Parameters
    ----------
    X       : (n_samples, n_features+1) —— 含截距列
    y       : (n_samples,) 0/1 标签
    lam     : L2 正则化系数
    max_iter: 最大迭代次数

    Returns
    -------
    w : (n_features+1,) 权重向量
    """
    n, d = X.shape
    w = np.zeros(d)
    for _ in range(max_iter):
        p = _sigmoid(X @ w)
        grad = X.T @ (p - y) / n
        grad[1:] += lam * w[1:]   # 截距不正则化
        w -= lr * grad
    return w


def _fit_ovr(
    X: np.ndarray,
    y: np.ndarray,
    n_classes: int,
    lam: float,
    max_iter: int,
) -> np.ndarray:
    """
    OvR 多分类：为每个类别拟合一个二分类器。

    Returns
    -------
    W : (n_classes, n_features+1)
    """
    W = np.zeros((n_classes, X.shape[1]))
    for k in range(n_classes):
        y_bin = (y == k).astype(float)
        W[k] = _fit_binary(X, y_bin, lam, max_iter)
    return W


def _predict_binary(X: np.ndarray, w: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Returns (pred_labels 0/1, proba for class=1)"""
    p = _sigmoid(X @ w)
    return (p >= 0.5).astype(int), p


def _predict_ovr(X: np.ndarray, W: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Returns
    -------
    preds   : (n_samples,) integer class indices
    probas  : (n_samples, n_classes) probability matrix (row-normalised)
    """
    raw = np.column_stack([_sigmoid(X @ W[k]) for k in range(W.shape[0])])
    row_sum = raw.sum(axis=1, keepdims=True)
    row_sum = np.where(row_sum == 0, 1.0, row_sum)
    probas = raw / row_sum
    preds  = probas.argmax(axis=1)
    return preds, probas


# ═══════════════════════════════════════════════════════════════════════════
#  3. 评估
# ═══════════════════════════════════════════════════════════════════════════

def _accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float((y_true == y_pred).mean()) if len(y_true) else 0.0


def _confusion_matrix_df(
    y_true: np.ndarray, y_pred: np.ndarray, classes: List[str]
) -> pd.DataFrame:
    idx2cls = {i: c for i, c in enumerate(classes)}
    actual    = [idx2cls.get(int(v), str(v)) for v in y_true]
    predicted = [idx2cls.get(int(v), str(v)) for v in y_pred]
    counts = (
        pd.DataFrame({"actual": actual, "predicted": predicted})
        .groupby(["actual", "predicted"], observed=True)
        .size()
        .reset_index(name="count")
    )
    # Fill in all (actual × predicted) combinations so the heatmap is always a full matrix.
    full_index = pd.MultiIndex.from_product([classes, classes], names=["actual", "predicted"])
    counts = (
        counts.set_index(["actual", "predicted"])
        .reindex(full_index, fill_value=0)
        .reset_index()
    )
    return counts


# ═══════════════════════════════════════════════════════════════════════════
#  4. 系数表（特征重要性）
# ═══════════════════════════════════════════════════════════════════════════

def _coef_df(
    feat_names: List[str],
    W: np.ndarray,
    classes: List[str],
    binary: bool,
) -> pd.DataFrame:
    """
    构建系数表。

    - 二分类：直接取 w[1:] （跳过截距）
    - 多分类：取各类别系数的 L2 范数作为"综合重要性"

    Columns: rank / feature / coefficient / odds_ratio / importance_pct
    """
    if binary:
        coefs = W[0, 1:]   # 取第一行（正类），跳过截距列
    else:
        # OvR：各类别系数向量的 L2 范数（每列特征的综合重要性）
        coefs = np.linalg.norm(W[:, 1:], axis=0)

    abs_coefs = np.abs(coefs)
    total     = abs_coefs.sum() or 1.0
    rows = sorted(
        [
            {
                "feature":        fn,
                "coefficient":    round(float(coefs[i]), 6),
                "odds_ratio":     round(float(np.exp(np.clip(coefs[i], -10, 10))), 4),
                "importance_pct": round(float(abs_coefs[i] / total * 100), 2),
            }
            for i, fn in enumerate(feat_names)
        ],
        key=lambda x: x["importance_pct"],
        reverse=True,
    )
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank

    return pd.DataFrame(rows)[
        ["rank", "feature", "coefficient", "odds_ratio", "importance_pct"]
    ]


# ═══════════════════════════════════════════════════════════════════════════
#  5. ROC 曲线（纯 numpy，无 sklearn）
# ═══════════════════════════════════════════════════════════════════════════

def _compute_roc(
    y_true:  np.ndarray,
    probas:  np.ndarray,
    classes: List[str],
) -> pd.DataFrame:
    rows: List[dict] = []

    for k, cls in enumerate(classes):
        y_bin   = (y_true == k).astype(int)
        y_score = probas[:, k]

        total_pos = int(y_bin.sum())
        total_neg = len(y_bin) - total_pos
        if total_pos == 0 or total_neg == 0:
            continue

        pairs = sorted(zip(y_score.tolist(), y_bin.tolist()), key=lambda x: -x[0])

        tpr_cur = fpr_cur = 0.0
        fpr_list = [0.0]
        tpr_list = [0.0]

        for score, label in pairs:
            if label == 1:
                tpr_cur += 1.0 / total_pos
            else:
                fpr_cur += 1.0 / total_neg
            fpr_list.append(round(fpr_cur, 6))
            tpr_list.append(round(tpr_cur, 6))

        auc = abs(sum(
            (fpr_list[i] - fpr_list[i - 1]) * (tpr_list[i] + tpr_list[i - 1]) / 2
            for i in range(1, len(fpr_list))
        ))
        auc = round(auc, 4)

        for fpr_v, tpr_v in zip(fpr_list, tpr_list):
            rows.append({"class": cls, "fpr": fpr_v, "tpr": tpr_v, "auc": auc})

    if not rows:
        return pd.DataFrame(columns=["class", "fpr", "tpr", "auc"])
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  6. Markdown 报告
# ═══════════════════════════════════════════════════════════════════════════

def _build_md(
    target_col:  str,
    classes:     List[str],
    binary:      bool,
    lam:         float,
    max_iter:    int,
    n_train:     int,
    n_test:      int,
    n_features:  int,
    train_acc:   float,
    test_acc:    float,
    coef_df:     pd.DataFrame,
    roc_df:      pd.DataFrame,
) -> str:
    mode_str = "二分类（Binary）" if binary else f"多分类 OvR（{len(classes)} 类）"
    cls_str  = "、".join(str(c) for c in classes[:8])
    if len(classes) > 8:
        cls_str += "..."

    L = [
        f"## 逻辑回归分析 - `{target_col}`\n",
        "### 模型概况",
        "| 指标 | 值 |", "|------|-----|",
        f"| 分类模式 | {mode_str} |",
        f"| 目标类别 | {cls_str} |",
        f"| 训练样本 | {n_train} |",
        f"| 测试样本 | {n_test} |",
        f"| 特征数量 | {n_features} |",
        f"| L2 正则化 λ | {lam} |",
        f"| 最大迭代次数 | {max_iter} |",
        "",
        "### 准确率",
        "| 集合 | 准确率 |", "|------|--------|",
        f"| 训练集 | **{train_acc:.2%}** |",
        f"| 测试集 | **{test_acc:.2%}** |",
        "",
    ]

    # ── ROC / AUC ──────────────────────────────────────────────────────────
    if not roc_df.empty:
        auc_rows = (
            roc_df.drop_duplicates("class")[["class", "auc"]]
            .sort_values("auc", ascending=False)
        )
        L += [
            "### ROC / AUC",
            "| 类别 | AUC | 评级 |",
            "|------|-----|------|",
        ]
        for _, row in auc_rows.iterrows():
            rating = "优秀" if row["auc"] >= 0.9 else ("良好" if row["auc"] >= 0.75 else "一般")
            L.append(f"| {row['class']} | **{row['auc']:.4f}** | {rating} |")
        L += [
            "",
            "> ROC 曲线数据存储于 `analysis_roc` 表（class / fpr / tpr / auc）。",
            "> 绘图方法：Line_Chart，x=fpr，y=tpr，按 class 分组着色。",
            "",
        ]

    # ── 系数表 ─────────────────────────────────────────────────────────────
    coef_label = "系数（综合重要性）" if not binary else "系数"
    L += [
        "### 特征系数与重要性",
        f"| 排名 | 特征 | {coef_label} | OR 值 | 重要性占比 |",
        "|:----:|------|----------:|------:|----------:|",
    ]
    for _, row in coef_df.iterrows():
        bar = "|" * max(1, int(row["importance_pct"] / 5))
        L.append(
            f"| {int(row['rank'])} | `{row['feature']}` "
            f"| {row['coefficient']:+.6f} | {row['odds_ratio']:.4f} "
            f"| {row['importance_pct']:.1f}% {bar} |"
        )
    L.append("")

    # ── 核心洞察 ───────────────────────────────────────────────────────────
    top = coef_df.iloc[0]
    gap = train_acc - test_acc
    L += ["### 核心洞察",
          f"- **最重要特征**：`{top['feature']}`，重要性占比 **{top['importance_pct']:.1f}%**"]

    if not binary:
        L.append(
            "  系数为多类 OvR 各向量的 L2 范数，反映该特征在所有类别中的综合判别力。"
        )

    if top["coefficient"] > 0:
        L.append(
            f"  `{top['feature']}` 系数为正（OR={top['odds_ratio']:.4f}），"
            f"该特征值越大，正类概率越高。"
        )
    else:
        L.append(
            f"  `{top['feature']}` 系数为负（OR={top['odds_ratio']:.4f}），"
            f"该特征值越大，正类概率越低。"
        )

    if test_acc >= 0.85:
        L.append(f"- 测试准确率 {test_acc:.2%}，模型泛化能力优秀。")
    elif test_acc >= 0.65:
        L.append(f"- 测试准确率 {test_acc:.2%}，模型有一定判别力，可考虑调整正则化参数。")
    else:
        L.append(f"- 测试准确率 {test_acc:.2%}，特征区分度有限，建议补充更多特征或清洗数据。")

    if gap > 0.15:
        L.append(
            f"- 过拟合风险：训练/测试准确率相差 {gap:.2%}，"
            f"建议增大 L2 正则化系数（groupby_column 参数）。"
        )

    if not roc_df.empty:
        best = roc_df.drop_duplicates("class").sort_values("auc", ascending=False).iloc[0]
        L.append(
            f"- 最高 AUC = **{best['auc']:.4f}**（{best['class']} 类，OvR）。"
        )

    L.append("")
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
#  7. 主入口
# ═══════════════════════════════════════════════════════════════════════════

def run(
    df:             pd.DataFrame,
    target_column:  str,
    groupby_column: Optional[str] = None,
    n_deciles:      int = 0,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    """
    运行逻辑回归分析。

    Parameters
    ----------
    df             : 原始数据 DataFrame
    target_column  : 目标列（分类标签）
    groupby_column : L2 正则化系数 lambda（字符串，默认 '0.01'）
    n_deciles      : 最大迭代次数（默认 1000，传 0 时使用默认值）

    Returns
    -------
    coef_df    : 系数 / 重要性表              → analysis_result
    cm_df      : 混淆矩阵（长格式）           → analysis_breakdown
    roc_df     : ROC 曲线点序列               → analysis_roc
    markdown   : Markdown 分析报告
    """
    # ── 参数解析 ──────────────────────────────────────────────────────────
    try:
        lam = float(groupby_column) if groupby_column else _DEFAULT_LAMBDA
        if lam <= 0:
            lam = _DEFAULT_LAMBDA
    except (ValueError, TypeError):
        lam = _DEFAULT_LAMBDA

    max_iter = int(n_deciles) if int(n_deciles) > 0 else _DEFAULT_ITER

    if target_column not in df.columns:
        raise ValueError(
            f"目标列 '{target_column}' 不存在。"
            f"可用列：{', '.join(df.columns[:20])}"
        )

    # ── 预处理 ────────────────────────────────────────────────────────────
    df = _fill_missing(df, target_column)
    df[target_column] = df[target_column].astype(str)

    classes = sorted(df[target_column].unique().tolist())
    if len(classes) < 2:
        raise ValueError(f"目标列 '{target_column}' 只有 1 种取值，无法分类。")

    binary = (len(classes) == 2)

    # ── 训练 / 测试分割 ───────────────────────────────────────────────────
    train_df, test_df = _split(df)

    # ── 特征编码 ──────────────────────────────────────────────────────────
    X_train, X_test, feat_names = _encode_features(train_df, test_df, target_column)
    y_train, y_test             = _encode_labels(
        train_df[target_column], test_df[target_column], classes
    )

    # ── 模型训练 ──────────────────────────────────────────────────────────
    if binary:
        w  = _fit_binary(X_train, y_train.astype(float), lam, max_iter)
        W  = w.reshape(1, -1)   # 统一为 (n_classes, n_feats+1) 格式
    else:
        W  = _fit_ovr(X_train, y_train, len(classes), lam, max_iter)

    # ── 预测 & 评估 ───────────────────────────────────────────────────────
    if binary:
        tr_pred, tr_prob1 = _predict_binary(X_train, W[0])
        te_pred, te_prob1 = _predict_binary(X_test,  W[0])
        # 统一为 (n_samples, n_classes) 格式
        tr_probas = np.column_stack([1 - tr_prob1, tr_prob1])
        te_probas = np.column_stack([1 - te_prob1, te_prob1])
    else:
        tr_pred, tr_probas = _predict_ovr(X_train, W)
        te_pred, te_probas = _predict_ovr(X_test,  W)

    train_acc = _accuracy(y_train, tr_pred)
    test_acc  = _accuracy(y_test,  te_pred)

    # ── 结果表 ────────────────────────────────────────────────────────────
    coef_df_out = _coef_df(feat_names, W, classes, binary)
    cm_df       = _confusion_matrix_df(y_test, te_pred, classes)
    roc_df      = _compute_roc(y_test, te_probas, classes)

    # ── Markdown 报告 ─────────────────────────────────────────────────────
    markdown = _build_md(
        target_col  = target_column,
        classes     = classes,
        binary      = binary,
        lam         = lam,
        max_iter    = max_iter,
        n_train     = len(train_df),
        n_test      = len(test_df),
        n_features  = len(feat_names),
        train_acc   = train_acc,
        test_acc    = test_acc,
        coef_df     = coef_df_out,
        roc_df      = roc_df,
    )

    return coef_df_out, cm_df, roc_df, markdown
