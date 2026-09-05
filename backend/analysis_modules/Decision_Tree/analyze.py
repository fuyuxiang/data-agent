#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Decision_Tree Analysis
======================
从零实现三种经典决策树（仅依赖 pandas + numpy，无需 scikit-learn）：
  - ID3   — 信息增益（Information Gain）
  - C4.5  — 增益率（Gain Ratio），解决 ID3 偏好多取值属性的问题
  - CART  — 基尼指数（Gini Index），生成二叉树

特性：
  - 类别型 & 数值型特征（数值型自动按四分位数分桶）
  - 可选 max_depth 限制树深（通过 n_deciles 参数传入，0 = 不限）
  - 70/30 自动划分训练/测试集并报告准确率
  - 叶节点存储类别概率分布，支持 ROC 曲线计算（无需 sklearn）
  - 输出三张结果表：
      analysis_result    — 特征重要性（Bar_Chart）
      analysis_breakdown — 混淆矩阵长格式（Heatmap）
      analysis_roc       — ROC 曲线点序列（Line_Chart, x=fpr, y=tpr）
"""

import numpy as np
import pandas as pd
from typing import Any, Dict, List, Optional, Tuple


# ── 模块元数据（供 registry 读取）─────────────────────────────────────────

ANALYSIS_ID   = "Decision_Tree"
ANALYSIS_NAME = "决策树分析（Decision Tree）"
ANALYSIS_DESC = (
    "使用 ID3 / C4.5 / CART 决策树对数据进行分类分析。"
    "输出特征重要性、训练/测试准确率、混淆矩阵及 ROC 曲线（含 AUC）。"
    "通过 groupby_column 参数指定算法（ID3/C4.5/CART，默认 C4.5），"
    "通过 n_deciles 参数指定最大树深（0 = 不限，默认不限）。"
)
REQUIRED_PARAMS = ["target_column"]
OPTIONAL_PARAMS = [
    "groupby_column (algorithm: ID3 / C4.5 / CART, default C4.5)",
    "n_deciles (max_depth: 0=unlimited, default 0)",
]
OUTPUT_TABLES = ["analysis_result", "analysis_breakdown", "analysis_roc"]

_N_BINS         = 4    # 数值特征分桶数（四分位）
_MIN_ROWS       = 6    # 数据少于此值时不做 train/test 分割
_MAX_TREE_LINES = 80   # Markdown 中展示树文本的最大行数


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


def _bin_numerics(df: pd.DataFrame, target_col: str,
                  n_bins: int = _N_BINS) -> pd.DataFrame:
    """将取值 > 10 的数值列分桶为 Q1/Q2/Q3/Q4 字符串标签。"""
    df = df.copy()
    for col in df.columns:
        if col == target_col:
            continue
        if pd.api.types.is_numeric_dtype(df[col]) and df[col].nunique() > 10:
            try:
                labels = [f"Q{i+1}" for i in range(n_bins)]
                df[col] = pd.qcut(
                    df[col].rank(method="first"),
                    q=n_bins, labels=labels, duplicates="drop"
                ).astype(str)
            except Exception:
                df[col] = df[col].astype(str)
    return df


def _to_str_features(df: pd.DataFrame, target_col: str) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if col != target_col:
            df[col] = df[col].astype(str)
    df[target_col] = df[target_col].astype(str)
    return df


def _split(df: pd.DataFrame, test_size: float = 0.3,
           seed: int = 42) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if len(df) < _MIN_ROWS:
        return df, df
    n_test = max(1, int(len(df) * test_size))
    test  = df.sample(n=n_test, random_state=seed)
    train = df.drop(test.index)
    return train, test


def _col_vals(df: pd.DataFrame, target_col: str) -> Dict[str, List]:
    """记录每列全部取值，用于补充子集中缺失的分支。"""
    return {col: list(df[col].unique())
            for col in df.columns if col != target_col}


# ═══════════════════════════════════════════════════════════════════════════
#  2. 基础度量
# ═══════════════════════════════════════════════════════════════════════════

def _entropy(series: pd.Series) -> float:
    p = series.value_counts(normalize=True)
    return float(-np.sum(p * np.log2(p + 1e-12)))


def _info_gain(df: pd.DataFrame, feat: str, target: str) -> float:
    base = _entropy(df[target])
    weighted = sum(
        (len(sub) / len(df)) * _entropy(sub[target])
        for _, sub in df.groupby(feat, observed=True)
    )
    return base - weighted


def _gain_ratio(df: pd.DataFrame, feat: str, target: str) -> float:
    gain = _info_gain(df, feat, target)
    p = df[feat].value_counts(normalize=True)
    iv = float(-np.sum(p * np.log2(p + 1e-12)))
    return gain / iv if iv > 1e-12 else 0.0


def _gini(series: pd.Series) -> float:
    p = series.value_counts(normalize=True)
    return float(1.0 - np.sum(p ** 2))


def _gini_split(df: pd.DataFrame, feat: str,
                target: str) -> Tuple[Any, float]:
    """返回 (最优切分值, 该切分的加权基尼指数)。"""
    best_val, best_gi = None, float("inf")
    for val, sub in df.groupby(feat, observed=True):
        gi = (len(sub) / len(df)) * _gini(sub[target])
        if gi < best_gi:
            best_gi, best_val = gi, val
    return best_val, best_gi


# ═══════════════════════════════════════════════════════════════════════════
#  3. 特征选择
# ═══════════════════════════════════════════════════════════════════════════

def _pick_id3(df, feats, target):
    return max(feats, key=lambda f: _info_gain(df, f, target))


def _pick_c45(df, feats, target):
    gains = {f: _info_gain(df, f, target) for f in feats}
    avg   = np.mean(list(gains.values()))
    good  = [f for f, g in gains.items() if g >= avg] or feats
    return max(good, key=lambda f: _gain_ratio(df, f, target))


def _pick_cart(df, feats, target):
    best_feat, best_val, best_gi = None, None, float("inf")
    for f in feats:
        val, gi = _gini_split(df, f, target)
        if gi < best_gi:
            best_gi, best_feat, best_val = gi, f, val
    return best_feat, best_val


# ═══════════════════════════════════════════════════════════════════════════
#  4. 树构建
#  叶节点格式：{"_label": "ClassName", "_p": {"ClassName": 0.8, ...}}
#  内部节点格式：{"feature_name": {"value1": subtree, ...}}
# ═══════════════════════════════════════════════════════════════════════════

def _most_with_proba(series: pd.Series) -> dict:
    """构造叶节点：存储多数类标签 + 类别概率分布（供 ROC 使用）。"""
    dist  = {str(k): float(v)
             for k, v in series.value_counts(normalize=True).items()}
    label = str(series.value_counts().index[0])
    return {"_label": label, "_p": dist}


def _stop(df, target, feats, depth, max_depth) -> bool:
    if df[target].nunique() == 1:                         return True
    if not feats:                                         return True
    if all(df[f].nunique() <= 1 for f in feats):          return True
    if max_depth is not None and depth >= max_depth:       return True
    return False


def _build_id3(df, target, feats, cv, depth=0, max_depth=None):
    if _stop(df, target, feats, depth, max_depth):
        return _most_with_proba(df[target])
    best = _pick_id3(df, feats, target)
    rem  = [f for f in feats if f != best]
    tree = {best: {}}
    for miss in set(cv.get(best, [])) - set(df[best].unique()):
        tree[best][miss] = _most_with_proba(df[target])
    for val, sub in df.groupby(best, observed=True):
        tree[best][val] = _build_id3(
            sub.drop(columns=[best]), target, rem, cv, depth + 1, max_depth)
    return tree


def _build_c45(df, target, feats, cv, depth=0, max_depth=None):
    if _stop(df, target, feats, depth, max_depth):
        return _most_with_proba(df[target])
    best = _pick_c45(df, feats, target)
    rem  = [f for f in feats if f != best]
    tree = {best: {}}
    for miss in set(cv.get(best, [])) - set(df[best].unique()):
        tree[best][miss] = _most_with_proba(df[target])
    for val, sub in df.groupby(best, observed=True):
        tree[best][val] = _build_c45(
            sub.drop(columns=[best]), target, rem, cv, depth + 1, max_depth)
    return tree


def _build_cart(df, target, feats, depth=0, max_depth=None):
    if _stop(df, target, feats, depth, max_depth):
        return _most_with_proba(df[target])
    best_feat, best_val = _pick_cart(df, feats, target)
    left  = df[df[best_feat] == best_val].drop(columns=[best_feat])
    right = df[df[best_feat] != best_val]
    rem   = [f for f in feats if f != best_feat]
    return {best_feat: {
        best_val: (_build_cart(left,  target, rem,   depth + 1, max_depth)
                   if not left.empty  else _most_with_proba(df[target])),
        "Others": (_build_cart(right, target, feats, depth + 1, max_depth)
                   if not right.empty else _most_with_proba(df[target])),
    }}


# ═══════════════════════════════════════════════════════════════════════════
#  5. 预测 & 评估
# ═══════════════════════════════════════════════════════════════════════════

def _is_leaf(node) -> bool:
    """判断节点是否为叶节点（含 _p 的 dict）。"""
    return isinstance(node, dict) and "_p" in node


def _leaf_majority(node) -> Any:
    """从任意子树收集全部叶节点标签，返回多数类。"""
    leaves: List[str] = []

    def _collect(n):
        if _is_leaf(n):
            leaves.append(n["_label"])
        elif isinstance(n, dict):
            feat = list(n.keys())[0]
            for b in n[feat].values():
                _collect(b)

    _collect(node)
    return max(set(leaves), key=leaves.count) if leaves else None


def _predict_one(tree, sample: Dict, cart: bool = False) -> Any:
    if _is_leaf(tree):
        return tree["_label"]
    if not isinstance(tree, dict):
        return str(tree)
    feat = list(tree.keys())[0]
    sub  = tree[feat]
    val  = str(sample.get(feat, ""))
    if cart:
        if val in sub:          return _predict_one(sub[val],       sample, cart)
        if "Others" in sub:     return _predict_one(sub["Others"],  sample, cart)
        return _leaf_majority(tree)
    else:
        if val in sub:          return _predict_one(sub[val], sample, cart)
        return _leaf_majority(sub)


def _avg_leaf_proba(node) -> dict:
    """收集子树所有叶节点的概率分布，返回其平均值（用于处理未见取值）。"""
    leaf_probas: List[dict] = []

    def _collect(n):
        if _is_leaf(n):
            leaf_probas.append(n["_p"])
        elif isinstance(n, dict):
            feat = list(n.keys())[0]
            for b in n[feat].values():
                _collect(b)

    _collect(node)
    if not leaf_probas:
        return {}
    all_cls = set(k for d in leaf_probas for k in d)
    return {c: float(np.mean([d.get(c, 0.0) for d in leaf_probas]))
            for c in all_cls}


def _predict_proba_dict(tree, sample: Dict, cart: bool = False) -> dict:
    """返回样本到达叶节点时的类别概率分布。"""
    if _is_leaf(tree):
        return tree["_p"]
    if not isinstance(tree, dict):
        return {}
    feat = list(tree.keys())[0]
    sub  = tree[feat]
    val  = str(sample.get(feat, ""))
    if cart:
        if val in sub:          return _predict_proba_dict(sub[val],      sample, cart)
        if "Others" in sub:     return _predict_proba_dict(sub["Others"], sample, cart)
        return _avg_leaf_proba(tree)
    else:
        if val in sub:          return _predict_proba_dict(sub[val], sample, cart)
        return _avg_leaf_proba(sub)


def _evaluate(
    tree, df: pd.DataFrame, target: str, cart: bool
) -> Tuple[float, pd.DataFrame, List[str], List[dict]]:
    """
    评估树在给定数据集上的表现。

    Returns
    -------
    accuracy : float
    cm_df    : 混淆矩阵 DataFrame（actual / predicted / count）
    actuals  : 实际标签列表（用于 ROC 计算）
    probas   : 每条样本的类别概率分布列表（用于 ROC 计算）
    """
    actuals: List[str] = []
    preds:   List[str] = []
    probas:  List[dict] = []

    for _, r in df.iterrows():
        sample = r.to_dict()
        preds.append(str(_predict_one(tree, sample, cart)))
        probas.append(_predict_proba_dict(tree, sample, cart))
        actuals.append(str(r[target]))

    n   = len(actuals)
    acc = sum(a == p for a, p in zip(actuals, preds)) / n if n else 0.0

    cm_df = (
        pd.DataFrame({"actual": actuals, "predicted": preds})
        .groupby(["actual", "predicted"], observed=True)
        .size()
        .reset_index(name="count")
    )
    return acc, cm_df, actuals, probas


# ═══════════════════════════════════════════════════════════════════════════
#  6. 特征重要性
# ═══════════════════════════════════════════════════════════════════════════

def _feat_importance(df, feats, target, algo) -> pd.DataFrame:
    if algo == "CART":
        base   = _gini(df[target])
        scores = {f: max(0.0, base - _gini_split(df, f, target)[1]) for f in feats}
    else:
        scores = {f: max(0.0, _info_gain(df, f, target)) for f in feats}

    total = sum(scores.values()) or 1.0
    rows  = [
        {"rank": i + 1, "feature": f,
         "importance": round(s, 6),
         "importance_pct": round(s / total * 100, 2)}
        for i, (f, s) in enumerate(
            sorted(scores.items(), key=lambda x: x[1], reverse=True))
    ]
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  7. ROC 曲线（纯 numpy，无 sklearn）
# ═══════════════════════════════════════════════════════════════════════════

def _compute_roc(
    actuals:     List[str],
    proba_dicts: List[dict],
    classes:     List[str],
) -> pd.DataFrame:
    """
    多分类 One-vs-Rest ROC 曲线计算（纯 numpy 实现，无需 sklearn）。

    Parameters
    ----------
    actuals     : 测试集实际标签
    proba_dicts : 每条样本的类别概率分布 {class: prob}
    classes     : 所有类别（已排序）

    Returns
    -------
    DataFrame(class, fpr, tpr, auc)
    每个类一组 (fpr, tpr) 点序列，fpr 和 tpr 均已排序，可直接作为 Line_Chart 数据。
    """
    rows: List[dict] = []

    for pos_class in classes:
        y_true  = [1 if a == pos_class else 0 for a in actuals]
        y_score = [d.get(pos_class, 0.0) for d in proba_dicts]

        total_pos = int(sum(y_true))
        total_neg = len(y_true) - total_pos

        # 跳过退化情形（测试集中该类无正样本或无负样本）
        if total_pos == 0 or total_neg == 0:
            continue

        # 按预测概率降序排列，逐步累积 TPR / FPR
        pairs = sorted(zip(y_score, y_true), key=lambda x: -x[0])

        tpr_cur = fpr_cur = 0.0
        fpr_list = [0.0]
        tpr_list = [0.0]

        for _, label in pairs:
            if label == 1:
                tpr_cur += 1.0 / total_pos
            else:
                fpr_cur += 1.0 / total_neg
            fpr_list.append(round(fpr_cur, 6))
            tpr_list.append(round(tpr_cur, 6))

        # AUC（梯形法则）
        auc = abs(sum(
            (fpr_list[i] - fpr_list[i - 1]) * (tpr_list[i] + tpr_list[i - 1]) / 2
            for i in range(1, len(fpr_list))
        ))
        auc = round(auc, 4)

        for fpr_v, tpr_v in zip(fpr_list, tpr_list):
            rows.append({
                "class": pos_class,
                "fpr":   fpr_v,
                "tpr":   tpr_v,
                "auc":   auc,
            })

    if not rows:
        return pd.DataFrame(columns=["class", "fpr", "tpr", "auc"])
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  8. 树可视化
# ═══════════════════════════════════════════════════════════════════════════

def _tree_depth(node) -> int:
    if _is_leaf(node) or not isinstance(node, dict): return 0
    feat = list(node.keys())[0]
    return 1 + max((_tree_depth(b) for b in node[feat].values()), default=0)


def _tree_leaves(node) -> int:
    if _is_leaf(node) or not isinstance(node, dict): return 1
    feat = list(node.keys())[0]
    return sum(_tree_leaves(b) for b in node[feat].values())


def _tree_lines(node, prefix: str = "") -> List[str]:
    if _is_leaf(node):
        label = node["_label"]
        conf  = node["_p"].get(label, 0.0)
        return [f"{prefix}> [{label}] ({conf:.0%})"]
    if not isinstance(node, dict):
        return [f"{prefix}> [{node}]"]
    feat  = list(node.keys())[0]
    items = list(node[feat].items())
    out   = [f"{prefix}[{feat}]"]
    for i, (val, branch) in enumerate(items):
        last         = (i == len(items) - 1)
        connector    = "+-" if not last else "\\-"
        child_prefix = prefix + ("|  " if not last else "   ")
        out.append(f"{prefix}{connector} = {val}")
        out.extend(_tree_lines(branch, child_prefix))
    return out


def _tree_text(tree, max_lines: int = _MAX_TREE_LINES) -> str:
    lines = _tree_lines(tree)
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [
            f"... (共 {len(lines)} 行，仅展示前 {max_lines} 行)"
        ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
#  9. Markdown 报告
# ═══════════════════════════════════════════════════════════════════════════

def _build_md(
    algo:            str,
    target_col:      str,
    feat_imp:        pd.DataFrame,
    tree:            Any,
    train_acc:       float,
    test_acc:        float,
    n_train:         int,
    n_test:          int,
    n_classes:       int,
    class_names:     List[str],
    max_depth_param: Optional[int],
    roc_df:          pd.DataFrame,
) -> str:
    algo_name = {
        "ID3":  "ID3（信息增益）",
        "C4.5": "C4.5（增益率）",
        "CART": "CART（基尼指数）",
    }.get(algo, algo)
    depth  = _tree_depth(tree)
    leaves = _tree_leaves(tree)

    L = [
        f"## 决策树分析 - `{target_col}` - {algo_name}\n",
        "### 模型概况",
        "| 指标 | 值 |", "|------|-----|",
        f"| 算法 | {algo_name} |",
        (f"| 类别数 | {n_classes}"
         f"（{', '.join(str(c) for c in class_names[:8])}"
         f"{'...' if len(class_names) > 8 else ''}）|"),
        f"| 训练样本 | {n_train} |",
        f"| 测试样本 | {n_test} |",
        f"| 树深度 | {depth} |",
        f"| 叶节点数 | {leaves} |",
        f"| 最大深度限制 | {'不限' if max_depth_param is None else max_depth_param} |",
        "",
        "### 准确率",
        "| 集合 | 准确率 |", "|------|--------|",
        f"| 训练集 | **{train_acc:.2%}** |",
        f"| 测试集 | **{test_acc:.2%}** |",
        "",
    ]

    # ── ROC / AUC 汇总 ────────────────────────────────────────────────────
    if not roc_df.empty:
        auc_rows = roc_df.drop_duplicates("class")[["class", "auc"]].sort_values("auc", ascending=False)
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

    # ── 特征重要性 ────────────────────────────────────────────────────────
    L += [
        "### 特征重要性",
        "| 排名 | 特征 | 重要性得分 | 占比 |",
        "|:----:|------|----------:|-----:|",
    ]
    for _, row in feat_imp.iterrows():
        bar = "|" * max(1, int(row["importance_pct"] / 5))
        L.append(
            f"| {int(row['rank'])} | `{row['feature']}` "
            f"| {row['importance']:.6f} | {row['importance_pct']:.1f}% {bar} |"
        )
    L.append("")

    # ── 核心洞察 ──────────────────────────────────────────────────────────
    top = feat_imp.iloc[0]
    gap = train_acc - test_acc
    L += ["### 核心洞察",
          f"- **最重要特征**：`{top['feature']}`，重要性占比 **{top['importance_pct']:.1f}%**"]

    if test_acc >= 0.85:
        L.append(f"- 测试准确率 {test_acc:.2%}，模型泛化能力优秀。")
    elif test_acc >= 0.65:
        L.append(f"- 测试准确率 {test_acc:.2%}，可考虑调整 max_depth 进一步优化。")
    else:
        L.append(f"- 测试准确率 {test_acc:.2%}，特征区分度不足或数据量过少。")

    if gap > 0.15:
        L.append(
            f"- 过拟合风险：训练/测试准确率相差 {gap:.2%}，"
            f"建议通过 n_deciles 参数设置最大树深。"
        )

    if not roc_df.empty:
        best_row = roc_df.drop_duplicates("class").sort_values("auc", ascending=False).iloc[0]
        L.append(
            f"- 最高 AUC = **{best_row['auc']:.4f}**"
            f"（{best_row['class']} 类，OvR）。"
        )

    L += ["", "### 决策树结构", "```", _tree_text(tree), "```", ""]
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════════════
#  10. 主入口
# ═══════════════════════════════════════════════════════════════════════════

def run(
    df:             pd.DataFrame,
    target_column:  str,
    groupby_column: Optional[str] = None,
    n_deciles:      int = 0,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    """
    运行决策树分析。

    Parameters
    ----------
    df             : 原始数据 DataFrame
    target_column  : 目标列（分类标签）
    groupby_column : 算法：'ID3' / 'C4.5' / 'CART'（默认 'C4.5'）
    n_deciles      : 最大树深，0 = 不限（默认 0）

    Returns
    -------
    feat_imp_df : 特征重要性 DataFrame        → analysis_result
    cm_df       : 混淆矩阵（长格式）          → analysis_breakdown
    roc_df      : ROC 曲线点序列              → analysis_roc
    markdown    : Markdown 分析报告
    """
    # ── 参数解析 ──────────────────────────────────────────────────────────
    algo_raw  = (groupby_column or "").strip().upper()
    algorithm = algo_raw if algo_raw in ("ID3", "C4.5", "CART") else "C4.5"
    max_depth = int(n_deciles) if int(n_deciles) > 0 else None
    cart_mode = (algorithm == "CART")

    if target_column not in df.columns:
        raise ValueError(
            f"目标列 '{target_column}' 不存在。"
            f"可用列：{', '.join(df.columns[:20])}"
        )

    # ── 预处理 ────────────────────────────────────────────────────────────
    df = _fill_missing(df, target_column)
    df = _bin_numerics(df, target_column, _N_BINS)
    df = _to_str_features(df, target_column)

    feature_cols = [c for c in df.columns if c != target_column]
    if not feature_cols:
        raise ValueError("数据集中没有可用的特征列。")
    if df[target_column].nunique() < 2:
        raise ValueError(f"目标列 '{target_column}' 只有 1 种取值，无法分类。")

    # ── 训练 / 测试分割 ───────────────────────────────────────────────────
    train_df, test_df = _split(df, test_size=0.3)
    cv = _col_vals(df, target_column)

    # ── 构建决策树 ────────────────────────────────────────────────────────
    if algorithm == "ID3":
        tree = _build_id3(train_df, target_column, feature_cols, cv,
                          max_depth=max_depth)
    elif algorithm == "C4.5":
        tree = _build_c45(train_df, target_column, feature_cols, cv,
                          max_depth=max_depth)
    else:
        tree = _build_cart(train_df, target_column, feature_cols,
                           max_depth=max_depth)

    # ── 评估 ─────────────────────────────────────────────────────────────
    # 训练集：只需准确率
    train_acc, _, _, _ = _evaluate(tree, train_df, target_column, cart_mode)
    # 测试集：准确率 + 混淆矩阵 + 概率分布（用于 ROC）
    test_acc, cm_df, test_actuals, test_probas = _evaluate(
        tree, test_df, target_column, cart_mode
    )

    # ── 特征重要性（在完整数据集上计算更稳定）───────────────────────────
    feat_imp_df = _feat_importance(df, feature_cols, target_column, algorithm)

    # ── ROC 曲线（仅在测试集上计算，避免过拟合偏差）─────────────────────
    class_names = sorted(df[target_column].unique().tolist())
    roc_df = _compute_roc(test_actuals, test_probas, class_names)

    # ── Markdown 报告 ─────────────────────────────────────────────────────
    markdown = _build_md(
        algo=algorithm,
        target_col=target_column,
        feat_imp=feat_imp_df,
        tree=tree,
        train_acc=train_acc,
        test_acc=test_acc,
        n_train=len(train_df),
        n_test=len(test_df),
        n_classes=df[target_column].nunique(),
        class_names=class_names,
        max_depth_param=max_depth,
        roc_df=roc_df,
    )

    return feat_imp_df, cm_df, roc_df, markdown
