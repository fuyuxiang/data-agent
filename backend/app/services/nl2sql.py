"""
NL2SQL 多 Agent 规划服务（Intent -> Coder -> Reviewer -> Self-Correction）

主流程：
1. Intent Agent 识别意图（chat/search/list/count）
2. Coder Agent 用 LLM 生成 DSL，再生成 SQL
3. Reviewer Agent 检查安全/可执行性/性能风险
4. Self-Correction：Reviewer 不通过时将错误反馈给 LLM 自动修正
5. 输出可执行计划或拒绝计划（不降级到规则 SQL）
"""

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

@dataclass
class QueryPlan:
    """查询计划"""
    intent: str
    sql: str
    params: List
    filters: Dict


def _parse_top_k(text: str, default: int = 20) -> int:
    """解析 TOP K 数量"""
    match = re.search(r"(前|top|TOP)\s*(\d+)", text)
    if match:
        return int(match.group(2))
    match = re.search(r"(\d+)\s*条", text)
    if match:
        return int(match.group(1))
    return default


# 闲聊模式
_CHAT_PATTERNS = [
    # 问候
    "你好", "您好", "hello", "hi", "嗨", "hey",
    # 感谢/告别
    "谢谢", "感谢", "再见", "拜拜", "bye",
    # 自我介绍/能力询问
    "你是谁", "你叫什么", "你能做什么", "你会什么", "怎么用", "使用说明", "帮助",
    # 纯闲聊
    "今天天气", "讲个笑话", "你好吗",
]

# 数据库查询关键词
_QUERY_KEYWORDS = [
    "查询", "查看", "搜索", "查找", "查一下", "找一下", "列出",
    "统计", "多少", "数量", "总数", "分布", "TOP", "top", "排名",
    "对比", "比较", "差异", "vs", "VS",
    "告警", "事件", "设备", "街道", "区县", "置信度", "工单",
    "最近", "今天", "昨天", "本月", "本周",
]

# 结构化查询强信号
_STRUCTURED_KEYWORDS = [
    "统计", "多少", "数量", "总数", "分布", "TOP", "top", "排名",
    "对比", "比较", "差异", "vs", "VS",
    "按街道", "按设备", "按类型", "按区", "按算法", "GROUP", "group",
]

# 视觉/内容描述关键词
_VISUAL_KEYWORDS = [
    # 颜色
    "红色", "蓝色", "白色", "黑色", "黄色", "绿色", "灰色", "橙色", "棕色",
    # 物体
    "挖掘机", "卡车", "轿车", "货车", "吊车", "推土机", "铲车", "摩托", "自行车",
    "人", "行人", "工人", "安全帽", "围栏", "塔吊", "电线杆", "铁塔",
    # 场景描述
    "沙地", "土坡", "工地", "草地", "马路", "停车场", "河边", "树林",
    # 动作/状态
    "停在", "停放", "行驶", "施工", "挖掘", "倒塌", "倾斜",
    # 搜图意图
    "找图", "找图片", "搜图", "类似的", "相似的", "像这样", "长什么样",
    "有没有", "有什么图", "图片", "照片",
]

_RAW_LIST_HINTS = [
    "明细", "详情", "样例", "样本", "原始数据",
]

_TABLE_POLICY_MODES = {"auto", "exact", "all"}


def _is_visual_query(text: str) -> bool:
    """判断是否为视觉内容描述类查询，应走向量检索"""
    # 有结构化强信号 → 不走向量
    if any(k in text for k in _STRUCTURED_KEYWORDS):
        return False
    # 命中视觉关键词
    visual_hits = sum(1 for k in _VISUAL_KEYWORDS if k in text)
    if visual_hits >= 1:
        return True
    return False


def _parse_intent(text: str) -> str:
    """解析用户意图"""
    t = text.strip()
    # 先检查是否是视觉内容描述（走向量检索）
    if _is_visual_query(t):
        return "search"
    # 再检查是否命中查询关键词（优先级最高）
    if any(k in t for k in _QUERY_KEYWORDS):
        if any(k in t for k in ["多少", "统计", "数量", "总数", "分布", "TOP", "top", "排名", "对比", "比较", "差异", "vs", "VS", "构成", "结构", "占比", "组成", "拆分"]):
            return "count"
        return "list"
    # 再检查是否是闲聊
    t_lower = t.lower()
    if any(p in t_lower for p in _CHAT_PATTERNS):
        return "chat"
    # 短文本且无查询关键词 → 大概率闲聊
    if len(t) <= 6:
        return "chat"
    return "list"


def _normalize_table_policy(raw_policy: Any) -> Dict[str, Any]:
    """标准化 LLM 输出的 table_policy"""
    mode = "auto"
    count: Optional[int] = None

    if isinstance(raw_policy, dict):
        mode = str(raw_policy.get("mode") or raw_policy.get("type") or "auto").strip().lower()
        count_raw = raw_policy.get("count")
        try:
            if count_raw is not None:
                parsed = int(count_raw)
                if parsed > 0:
                    count = parsed
        except Exception:
            count = None
    elif isinstance(raw_policy, (int, float)):
        parsed = int(raw_policy)
        if parsed > 0:
            mode = "exact"
            count = parsed
    elif isinstance(raw_policy, str):
        text = raw_policy.strip().lower()
        if text in _TABLE_POLICY_MODES:
            mode = text
        else:
            try:
                parsed = int(text)
                if parsed > 0:
                    mode = "exact"
                    count = parsed
            except Exception:
                mode = "auto"

    if mode not in _TABLE_POLICY_MODES:
        mode = "auto"
    if mode != "exact":
        count = None

    policy: Dict[str, Any] = {"mode": mode}
    if count is not None:
        policy["count"] = count
    return policy


def _required_table_count_from_policy(policy: Dict[str, Any], selected_count: int) -> Optional[int]:
    """根据 table_policy 计算最小表数量要求"""
    if selected_count <= 1:
        return None

    mode = str(policy.get("mode") or "auto").lower()
    if mode == "all":
        return selected_count
    if mode == "exact":
        try:
            count = int(policy.get("count"))
            if count > 0:
                return min(count, selected_count)
        except Exception:
            return None
    return None


def _validate_dsl_table_policy(dsl: Dict[str, Any], selected_tables: List[str]) -> List[str]:
    """校验 DSL 的 table_policy 与 tables 是否一致"""
    selected = list(dict.fromkeys([str(t).strip() for t in (selected_tables or []) if str(t).strip()]))
    selected_count = len(selected)
    if selected_count <= 1:
        return []

    if dsl.get("table_policy_missing"):
        return ["dsl_missing_table_policy"]

    policy = dsl.get("table_policy")
    if not isinstance(policy, dict):
        return ["dsl_missing_table_policy"]

    mode = str(policy.get("mode") or "").lower()
    if mode not in _TABLE_POLICY_MODES:
        return [f"dsl_invalid_table_policy_mode:{mode or 'empty'}"]
    if mode == "exact":
        count = policy.get("count")
        if not isinstance(count, int) or count <= 0:
            return [f"dsl_invalid_table_policy_count:{count}"]

    required_count = _required_table_count_from_policy(policy, selected_count)
    if required_count is None:
        return []

    dsl_tables = dsl.get("tables") if isinstance(dsl, dict) else []
    dsl_table_names = list(dict.fromkeys([str(t).strip() for t in (dsl_tables or []) if str(t).strip()]))
    if len(dsl_table_names) < required_count:
        return [f"dsl_table_count_too_small:required>={required_count},actual={len(dsl_table_names)}"]
    return []


def _choose_fallback_tables_by_policy(fallback_tables: List[str], policy: Dict[str, Any]) -> List[str]:
    """当 DSL 缺 tables 时，基于 table_policy 选择回填表集合"""
    tables = list(dict.fromkeys([str(t).strip() for t in (fallback_tables or []) if str(t).strip()]))
    if not tables:
        return []

    mode = str(policy.get("mode") or "auto").lower()
    if mode == "all":
        return tables
    if mode == "exact":
        try:
            count = int(policy.get("count"))
            if count > 0:
                return tables[: min(count, len(tables))]
        except Exception:
            pass
    return tables[:1]


def _string_overlap_score(a: str, b: str) -> float:
    """字符串相似度评分（0~1），兼容中英文列名/短语"""
    a_norm = _normalize_name_for_match(a)
    b_norm = _normalize_name_for_match(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm == b_norm:
        return 1.0
    if a_norm in b_norm or b_norm in a_norm:
        return 0.86
    set_a = set(a_norm)
    set_b = set(b_norm)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return len(set_a & set_b) / union


def _column_match_score(column_name: str, hint: str) -> float:
    """列名与查询提示词匹配分数（0~1）"""
    col = str(column_name or "")
    h = str(hint or "")
    if not col or not h:
        return 0.0
    col_lower = col.lower()
    h_lower = h.lower()
    if col_lower == h_lower:
        return 1.0
    if h_lower in col_lower or col_lower in h_lower:
        return 0.92
    score = _string_overlap_score(col, h)

    semantic_groups = [
        (["月", "月份", "账期", "month", "date", "时间", "日期"], ["month", "ym", "date", "time", "day", "year", "账期", "月份", "日期", "时间"]),
        (["省", "省份", "省分", "城市", "地区", "prov", "city"], ["省", "省分", "省份", "prov", "city", "地区"]),
        (["渠道", "channel"], ["渠道", "channel"]),
        (["产品", "product", "prod"], ["产品", "prod", "product"]),
        (["用户", "账号", "acct", "user"], ["用户", "账号", "acct", "user"]),
    ]
    for hint_tokens, col_tokens in semantic_groups:
        if any(token in h_lower for token in hint_tokens) and any(token in col_lower for token in col_tokens):
            score = max(score, 0.82)

    return score


def _is_numeric_type(column_type: str) -> bool:
    """判断字段类型是否为数值型"""
    dt = str(column_type or "").upper()
    return any(k in dt for k in ["INT", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL"])


def _is_metric_like_column(column_name: str) -> bool:
    """根据列名判断是否度量/指标字段"""
    name = str(column_name or "").lower()
    metric_tokens = [
        "fee", "amount", "revenue", "income", "cost", "price", "value", "cnt", "count", "num",
        "收入", "金额", "费用", "计费", "出账", "折扣", "调账", "流量", "语音", "短信", "数量", "使用量",
    ]
    return any(token in name for token in metric_tokens)


def _extract_dimension_hints(text: str) -> List[str]:
    """从问题中提取维度提示词（用于 GROUP BY / JOIN 对齐）"""
    q = (text or "").strip()
    if not q:
        return []

    hints: List[str] = []

    for pattern in [
        r"(?:按|各|每)\s*([^\s，。,.]{1,24})",
        r"(?:相同|同)\s*([^\s，。,.和与及]{1,16})",
    ]:
        for raw in re.findall(pattern, q):
            cand = (raw or "").strip(" ，。,.、")
            if not cand:
                continue
            # 截断语义尾巴，保留维度核心短语
            for cut_word in ["的", "进行", "统计", "汇总", "分析", "收入", "金额", "用户数", "数量", "分布", "差异", "对比", "明细", "记录"]:
                idx = cand.find(cut_word)
                if idx > 0:
                    cand = cand[:idx]
            cand = cand.strip()
            if cand:
                hints.append(cand)

    # 显式时间/地域语义提示
    q_lower = q.lower()
    if any(k in q_lower for k in ["月份", "按月", "月度", "month", "账期"]):
        hints.append("月份")
    if any(k in q for k in ["省", "省份", "省分", "地区", "城市"]):
        hints.append("省份")
    if "渠道" in q:
        hints.append("渠道")
    if "产品" in q:
        hints.append("产品")

    # 去重保序
    return list(dict.fromkeys([h for h in hints if h]))


def _pick_metric_column(
    columns: List[str],
    question: str,
    *,
    prefer_amount: bool = False,
    prefer_count_metric: bool = False,
) -> Optional[str]:
    """通用指标字段选择（基于 schema + 问句语义）"""
    if not columns:
        return None
    q = (question or "").lower()
    scored: List[Tuple[float, str]] = []

    amount_hints = ["收入", "金额", "费用", "计费", "出账", "fee", "amount", "revenue", "income", "cost", "price"]
    count_hints = ["用户数", "数量", "条数", "记录数", "个数", "count", "cnt", "num", "user", "acct"]

    for col in columns:
        col_text = str(col or "")
        if not col_text:
            continue
        col_lower = col_text.lower()
        score = 0.0

        if _is_metric_like_column(col_text):
            score += 0.25
        if "id" == col_lower or col_lower.endswith("_id"):
            score -= 0.2
        if col_lower.startswith("rsrv_"):
            score -= 0.3

        score += _column_match_score(col_text, q) * 0.9

        if prefer_amount or any(k in q for k in amount_hints):
            score += max((_column_match_score(col_text, hint) for hint in amount_hints), default=0.0) * 0.9
        if prefer_count_metric or any(k in q for k in count_hints):
            score += max((_column_match_score(col_text, hint) for hint in count_hints), default=0.0) * 0.8

        if score > 0:
            scored.append((score, col_text))

    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return scored[0][1]


def _detect_aggregation_mode(text: str) -> str:
    """识别聚合模式：count/sum/avg/max/min"""
    q = (text or "").lower()
    if any(k in q for k in ["平均", "均值", "avg"]):
        return "avg"
    if any(k in q for k in ["最大", "最高", "max"]):
        return "max"
    if any(k in q for k in ["最小", "最低", "min"]):
        return "min"
    if any(k in q for k in ["记录数", "条数", "多少条", "多少行", "count", "数量", "多少", "个数"]):
        if not any(k in q for k in ["收入", "金额", "费用", "fee", "amount", "revenue", "income"]):
            return "count"
    return "sum"


def _get_table_schema(table_name: str) -> List[Dict]:
    """获取表结构信息"""
    from app.orchestrator_duckdb import get_engine
    engine = get_engine()
    engine.connect()
    try:
        cols = engine.get_schema(table_name)
        return cols
    except Exception:
        return []


def _normalize_name_for_match(name: str) -> str:
    """标准化名称，便于匹配"""
    if not name:
        return ""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", name.lower())


def _normalize_table_name(name: str) -> str:
    """标准化表名（与 DuckDB 加载逻辑保持一致）"""
    if not name:
        return ""
    table_name = name
    if table_name.endswith(".csv"):
        table_name = table_name[:-4]
    if "_" in table_name:
        parts = table_name.split("_", 1)
        if len(parts[0]) == 8 and parts[0].isdigit():
            table_name = parts[1]
    return table_name.replace("-", "_").replace(".", "_")


def _resolve_candidate_tables(table_names: Optional[List[str]]) -> List[str]:
    """将前端传入的候选表名映射为 DuckDB 中真实可用的表名"""
    if not table_names:
        return []

    from app.orchestrator_duckdb import get_engine
    engine = get_engine()
    engine.connect()
    existing_tables = engine.get_tables()
    if not existing_tables:
        return table_names

    # 归一化映射：前端表名可能和 DuckDB 实际表名存在格式差异
    normalized_existing = {
        _normalize_name_for_match(_normalize_table_name(tbl)): tbl
        for tbl in existing_tables
    }
    original_existing = {tbl.lower(): tbl for tbl in existing_tables}

    resolved: List[str] = []
    for raw_name in table_names:
        if not raw_name:
            continue
        lower_name = raw_name.lower()
        normalized_name = _normalize_name_for_match(_normalize_table_name(raw_name))

        if lower_name in original_existing:
            resolved.append(original_existing[lower_name])
            continue
        if normalized_name in normalized_existing:
            resolved.append(normalized_existing[normalized_name])
            continue

    # 如果一个都没映射上，回退原始值，保持兼容
    return list(dict.fromkeys(resolved)) if resolved else table_names


def _rank_candidate_tables(text: str, table_names: List[str], intent: str) -> List[Dict[str, Any]]:
    """候选表排序（无业务规则）：保持用户选择顺序"""
    if not table_names:
        return []

    ranked: List[Dict[str, Any]] = []
    for idx, table_name in enumerate(table_names):
        ranked.append({
            "table": table_name,
            "score": max(1, len(table_names) - idx),
            "reason": "按用户选择顺序",
            "order": idx,
        })

    ranked.sort(key=lambda item: (-int(item.get("score", 0)), int(item.get("order", 0))))
    return ranked


def _get_llm_config(config: Dict):
    """提取 LLM 连接配置"""
    llm_cfg = config.get("llm", {})

    # 优先使用环境变量 LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    api_key = os.getenv("LLM_API_KEY") or llm_cfg.get("api_key")
    if not api_key:
        raise RuntimeError("LLM API key not configured")

    base_url = os.getenv("LLM_BASE_URL") or llm_cfg.get("base_url", "https://api.deepseek.com")
    model = os.getenv("LLM_MODEL") or llm_cfg.get("model", "deepseek-chat")
    url = base_url.rstrip("/") + "/v1/chat/completions"
    timeout = llm_cfg.get("timeout", 30)

    return api_key, url, model, timeout


def _call_llm_chat(api_key: str, url: str, model: str, timeout: int,
                   system_prompt: str, user_prompt: str) -> str:
    """通用 LLM 调用"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        # 部分 OpenAI 兼容网关不支持 response_format，降级重试一次
        if status_code in {400, 422}:
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            response = requests.post(url, headers=headers, json=fallback_payload, timeout=timeout)
            response.raise_for_status()
        else:
            raise
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _strip_think_blocks(text: str) -> str:
    """去除常见推理标签，保留可解析内容"""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<\s*/?\s*think\s*>", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _extract_first_json_object(text: str) -> Optional[str]:
    """从文本中提取第一个完整 JSON 对象"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start: idx + 1]
        start = text.find("{", start + 1)
    return None


def _parse_llm_json(content: str) -> dict:
    """从 LLM 输出中提取 JSON 对象"""
    text = _strip_think_blocks(content.strip())
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    candidate = _extract_first_json_object(text) or text
    try:
        return json.loads(candidate)
    except Exception as exc:
        raise RuntimeError(f"LLM 输出不是有效 JSON: {content}") from exc


def _build_schema_prompt_for_selected_tables(table_names: List[str]) -> str:
    """仅为候选表构建 schema 文本，避免 LLM 误用其他表"""
    parts: List[str] = []
    for table_name in table_names:
        columns = _get_table_schema(table_name)
        if not columns:
            continue
        lines = [f"表: {table_name}"]
        for col in columns:
            col_name = col.get("column_name", "")
            col_type = col.get("column_type", "")
            nullable = col.get("null", "")
            lines.append(f"  - {col_name} ({col_type}) 可空 {nullable}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _build_nl2dsl_system_prompt(schema_prompt: str, table_scope_hint: str = "", intent_hint: str = "list") -> str:
    """构建 NL->DSL 的 system prompt"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "你是一个 NL2SQL 规划器，请先输出结构化 DSL，不要直接输出 SQL。\n\n"
        f"# 当前时间\n{now_str}\n"
        "所有涉及'最近N天'、'本月'、'今天'、'昨天'等相对时间的表达，都必须基于上面的当前时间计算。\n\n"
        "# 数据库 Schema\n"
        f"{schema_prompt}\n\n"
        f"{table_scope_hint}"
        "# 输出要求\n"
        "1. 仅使用 schema 中存在的表和字段，不得臆造。\n"
        "2. 仅输出 JSON 对象，不要任何额外文本。\n"
        "3. intent 仅允许 list 或 count。\n"
        "4. where 中值必须保留原始语义，暂不参数化。\n"
        "5. joins 的 left/right 使用 table.column 形式。\n"
        "6. select/group_by/order_by/where 的 column 均使用原字段名或 table.column。\n"
        "7. 必须输出 table_policy：mode 仅允许 auto/exact/all；mode=exact 时必须提供 count>0。\n"
        "8. 若问题有明确表数量要求，table_policy 必须表达该要求，且 tables 与之匹配。\n\n"
        f"# intent 提示\n用户问题倾向: {intent_hint}\n\n"
        "# DSL JSON Schema\n"
        '{'
        '"intent":"list|count",'
        '"table_policy":{"mode":"auto|exact|all","count":"mode=exact时必填正整数"},'
        '"tables":["table_a"],'
        '"select":[{"column":"table_a.col|col|*","agg":"none|count|sum|avg|max|min","alias":"可选"}],'
        '"joins":[{"type":"inner|left|right|full","left":"table_a.col","right":"table_b.col"}],'
        '"where":[{"column":"table_a.col|col","op":"=|!=|>|>=|<|<=|between|in|like","value":"任意JSON值"}],'
        '"group_by":["table_a.col|col"],'
        '"order_by":[{"column":"table_a.col|col|alias","direction":"asc|desc"}],'
        '"limit":100'
        '}'
    )


def _build_sql_from_dsl_system_prompt(schema_prompt: str, table_scope_hint: str = "") -> str:
    """构建 DSL->SQL 的 system prompt"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "你是一个 SQL 生成器。给定已校验 DSL，请严格按 DSL 生成 DuckDB SQL。\n\n"
        f"# 当前时间\n{now_str}\n\n"
        "# 数据库 Schema\n"
        f"{schema_prompt}\n\n"
        f"{table_scope_hint}"
        "# 强约束\n"
        "1. 只允许 SELECT 查询。\n"
        "2. 只能使用 DSL 中 tables/joins/select/where/group_by/order_by/limit 指定的信息。\n"
        "3. where 的值必须使用 ? 占位符，params 顺序与 SQL 占位符一致。\n"
        "4. 不要补充 DSL 未要求的过滤条件或 join。\n"
        "5. 若无法生成可执行 SQL，输出空 sql（由上层触发重试）。\n\n"
        "# 输出格式\n"
        '{"intent":"count|list","sql":"...","params":[...],"filters":{}}'
    )


def _canonical_ref_name(ref: str) -> str:
    """标准化列引用（去引号/空白，仅保留 table.col 或 col）"""
    text = str(ref or "").strip().strip('"').strip("'")
    text = re.sub(r"\s+", "", text)
    return text


def _split_ref(ref: str) -> Tuple[Optional[str], str]:
    """拆分列引用为 table/column"""
    norm = _canonical_ref_name(ref)
    if "." in norm:
        left, right = norm.split(".", 1)
        return left or None, right
    return None, norm


def _normalize_dsl(
    obj: Dict[str, Any],
    intent_hint: str,
    fallback_tables: List[str],
) -> Dict[str, Any]:
    """将 LLM 输出规范化为统一 DSL 结构"""
    if not isinstance(obj, dict):
        raise RuntimeError("DSL 输出不是 JSON 对象")

    raw_intent = str(obj.get("intent") or intent_hint or "list").lower()
    intent = "count" if raw_intent == "count" else "list"

    raw_tables = obj.get("tables")
    if not isinstance(raw_tables, list):
        raw_tables = obj.get("from")
    raw_table_policy = obj.get("table_policy")
    table_policy_missing = "table_policy" not in obj
    table_policy = _normalize_table_policy(raw_table_policy)
    tables: List[str] = []
    if isinstance(raw_tables, list):
        for item in raw_tables:
            val = str(item or "").strip()
            if val:
                tables.append(val)
    if not tables and fallback_tables:
        tables = _choose_fallback_tables_by_policy(fallback_tables, table_policy)
    tables = list(dict.fromkeys(tables))

    def _normalize_select(items: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        if not isinstance(items, list):
            return normalized
        for item in items:
            if isinstance(item, str):
                col = _canonical_ref_name(item)
                if col:
                    normalized.append({"column": col, "agg": "none"})
                continue
            if not isinstance(item, dict):
                continue
            col = _canonical_ref_name(str(item.get("column") or item.get("expr") or ""))
            if not col:
                continue
            agg = str(item.get("agg") or item.get("func") or "none").lower()
            if agg not in {"none", "count", "sum", "avg", "max", "min"}:
                agg = "none"
            alias = str(item.get("alias") or "").strip()
            row: Dict[str, Any] = {"column": col, "agg": agg}
            if alias:
                row["alias"] = alias
            normalized.append(row)
        return normalized

    def _normalize_joins(items: Any) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        if not isinstance(items, list):
            return normalized
        for item in items:
            if not isinstance(item, dict):
                continue
            left = _canonical_ref_name(item.get("left"))
            right = _canonical_ref_name(item.get("right"))
            if not left or not right:
                continue
            jtype = str(item.get("type") or "inner").lower()
            if jtype not in {"inner", "left", "right", "full"}:
                jtype = "inner"
            normalized.append({"type": jtype, "left": left, "right": right})
        return normalized

    def _normalize_where(items: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        if not isinstance(items, list):
            return normalized
        for item in items:
            if not isinstance(item, dict):
                continue
            col = _canonical_ref_name(item.get("column"))
            if not col:
                continue
            op = str(item.get("op") or "=").lower()
            if op not in {"=", "!=", ">", ">=", "<", "<=", "between", "in", "like"}:
                op = "="
            normalized.append({"column": col, "op": op, "value": item.get("value")})
        return normalized

    def _normalize_group(items: Any) -> List[str]:
        vals: List[str] = []
        if isinstance(items, list):
            for item in items:
                col = _canonical_ref_name(item)
                if col:
                    vals.append(col)
        return list(dict.fromkeys(vals))

    def _normalize_order(items: Any) -> List[Dict[str, str]]:
        vals: List[Dict[str, str]] = []
        if not isinstance(items, list):
            return vals
        for item in items:
            if isinstance(item, str):
                col = _canonical_ref_name(item)
                if col:
                    vals.append({"column": col, "direction": "desc"})
                continue
            if not isinstance(item, dict):
                continue
            col = _canonical_ref_name(item.get("column"))
            if not col:
                continue
            direction = str(item.get("direction") or "desc").lower()
            if direction not in {"asc", "desc"}:
                direction = "desc"
            vals.append({"column": col, "direction": direction})
        return vals

    select = _normalize_select(obj.get("select"))
    joins = _normalize_joins(obj.get("joins"))
    where = _normalize_where(obj.get("where"))
    group_by = _normalize_group(obj.get("group_by"))
    order_by = _normalize_order(obj.get("order_by"))

    limit_val = obj.get("limit", 1000 if intent == "list" else None)
    limit: Optional[int] = None
    if limit_val is not None:
        try:
            parsed = int(limit_val)
            if parsed > 0:
                limit = min(parsed, 2000)
        except Exception:
            limit = None

    if intent == "count" and not select:
        select = [{"column": "*", "agg": "count", "alias": "数量"}]

    return {
        "intent": intent,
        "table_policy": table_policy,
        "table_policy_missing": table_policy_missing,
        "tables": tables,
        "select": select,
        "joins": joins,
        "where": where,
        "group_by": group_by,
        "order_by": order_by,
        "limit": limit,
    }


def _build_table_column_index(table_names: List[str]) -> Dict[str, set]:
    """构建 table->column_set 索引"""
    index: Dict[str, set] = {}
    for table_name in table_names or []:
        cols = _get_table_schema(table_name)
        index[table_name.lower()] = {
            str(col.get("column_name", "")).strip().lower()
            for col in cols
            if col.get("column_name")
        }
    return index


def _validate_column_ref(ref: str, table_column_index: Dict[str, set], preferred_tables: List[str]) -> bool:
    """校验列引用是否可在候选 schema 中解析"""
    table, col = _split_ref(ref)
    col_lower = str(col or "").strip().lower()
    if not col_lower:
        return False
    if col_lower == "*":
        return True
    if table:
        cols = table_column_index.get(table.lower())
        return bool(cols and col_lower in cols)

    # 未指定表时，允许在任一候选表命中；若候选为空，视为未知
    candidates = preferred_tables or list(table_column_index.keys())
    for t in candidates:
        cols = table_column_index.get(str(t).lower())
        if cols and col_lower in cols:
            return True
    return False


def _validate_dsl(dsl: Dict[str, Any], allowed_tables: List[str]) -> List[str]:
    """DSL 确定性校验：表/字段/操作符/连接关系"""
    errors: List[str] = []
    tables = dsl.get("tables") or []
    if not tables:
        errors.append("dsl_missing_tables")
        return errors

    allowed_lower = {t.lower() for t in (allowed_tables or [])}
    for table_name in tables:
        if allowed_lower and table_name.lower() not in allowed_lower:
            errors.append(f"dsl_table_not_allowed:{table_name}")

    table_column_index = _build_table_column_index(tables)
    if not table_column_index:
        errors.append("dsl_tables_schema_unavailable")
        return errors

    for sel in dsl.get("select") or []:
        if not _validate_column_ref(sel.get("column", ""), table_column_index, tables):
            errors.append(f"dsl_invalid_select_column:{sel.get('column')}")

    for item in dsl.get("where") or []:
        if not _validate_column_ref(item.get("column", ""), table_column_index, tables):
            errors.append(f"dsl_invalid_where_column:{item.get('column')}")
        op = str(item.get("op") or "=").lower()
        if op == "between":
            value = item.get("value")
            if not isinstance(value, list) or len(value) != 2:
                errors.append(f"dsl_invalid_between_value:{item.get('column')}")
        if op == "in":
            value = item.get("value")
            if not isinstance(value, list) or len(value) == 0:
                errors.append(f"dsl_invalid_in_value:{item.get('column')}")

    for item in dsl.get("group_by") or []:
        if not _validate_column_ref(item, table_column_index, tables):
            errors.append(f"dsl_invalid_group_by:{item}")

    for item in dsl.get("order_by") or []:
        col = item.get("column", "")
        if not _validate_column_ref(col, table_column_index, tables):
            # order by 也允许使用 select alias
            aliases = {str(s.get("alias", "")).strip().lower() for s in dsl.get("select") or [] if s.get("alias")}
            if str(col).strip().lower() not in aliases:
                errors.append(f"dsl_invalid_order_by:{col}")

    for join in dsl.get("joins") or []:
        if not _validate_column_ref(join.get("left", ""), table_column_index, tables):
            errors.append(f"dsl_invalid_join_left:{join.get('left')}")
        if not _validate_column_ref(join.get("right", ""), table_column_index, tables):
            errors.append(f"dsl_invalid_join_right:{join.get('right')}")

    return list(dict.fromkeys(errors))


def _validate_sql_matches_dsl(sql: str, dsl: Dict[str, Any]) -> List[str]:
    """校验 SQL 与 DSL 核心约束是否一致"""
    errors: List[str] = []
    sql_text = str(sql or "")
    if not sql_text.strip():
        return ["sql_empty"]

    sql_lower = sql_text.lower()
    for table_name in dsl.get("tables") or []:
        if table_name.lower() not in sql_lower:
            errors.append(f"sql_missing_table:{table_name}")

    for join in dsl.get("joins") or []:
        left_col = _split_ref(join.get("left", ""))[1].lower()
        right_col = _split_ref(join.get("right", ""))[1].lower()
        if left_col and left_col not in sql_lower:
            errors.append(f"sql_missing_join_col:{left_col}")
        if right_col and right_col not in sql_lower:
            errors.append(f"sql_missing_join_col:{right_col}")

    for item in dsl.get("where") or []:
        col = _split_ref(item.get("column", ""))[1].lower()
        if col and col not in sql_lower:
            errors.append(f"sql_missing_where_col:{col}")

    group_by = dsl.get("group_by") or []
    if group_by and "group by" not in sql_lower:
        errors.append("sql_missing_group_by_clause")

    order_by = dsl.get("order_by") or []
    if order_by and "order by" not in sql_lower:
        errors.append("sql_missing_order_by_clause")

    limit = dsl.get("limit")
    if limit and "limit" not in sql_lower:
        errors.append("sql_missing_limit_clause")

    return list(dict.fromkeys(errors))


def _call_deepseek_nl2dsl(
    question: str,
    config: Dict,
    db_path: str,
    table_names: Optional[List[str]],
    intent_hint: str,
) -> Dict[str, Any]:
    """调用 LLM 生成 DSL"""
    from app.services.schema_meta import build_schema_prompt

    api_key, url, model, timeout = _get_llm_config(config)
    selected_tables = table_names or []
    if selected_tables:
        schema_prompt = _build_schema_prompt_for_selected_tables(selected_tables)
        if not schema_prompt:
            schema_prompt = build_schema_prompt(db_path)
        table_scope_hint = (
            "# 表访问范围限制\n"
            f"本次只允许使用以下表：{', '.join(selected_tables)}。\n"
            "禁止使用未在列表中的表。\n\n"
        )
    else:
        schema_prompt = build_schema_prompt(db_path)
        table_scope_hint = ""

    system_prompt = _build_nl2dsl_system_prompt(schema_prompt, table_scope_hint=table_scope_hint, intent_hint=intent_hint)
    selected_tables_hint = f"\n候选表: {', '.join(selected_tables)}" if selected_tables else ""
    user_prompt = f"问题: {question}{selected_tables_hint}\n请输出 DSL JSON。"
    content = _call_llm_chat(api_key, url, model, timeout, system_prompt, user_prompt)
    raw_obj = _parse_llm_json(content)
    return _normalize_dsl(
        raw_obj,
        intent_hint=intent_hint,
        fallback_tables=selected_tables,
    )


def _call_deepseek_nl2dsl_with_retry(
    question: str,
    config: Dict,
    db_path: str,
    table_names: Optional[List[str]],
    allowed_tables: List[str],
    intent_hint: str,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """调用 LLM 生成 DSL，失败自动重试"""
    llm_cfg = config.get("llm", {}) if isinstance(config, dict) else {}
    try:
        max_attempts = int(llm_cfg.get("dsl_retry_attempts", llm_cfg.get("retry_attempts", 3)))
    except Exception:
        max_attempts = 3
    max_attempts = max(1, min(6, max_attempts))

    try:
        retry_interval = float(llm_cfg.get("retry_interval_sec", 0.6))
    except Exception:
        retry_interval = 0.6
    retry_interval = max(0.0, min(5.0, retry_interval))

    errors: List[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            dsl = _call_deepseek_nl2dsl(
                question=question,
                config=config,
                db_path=db_path,
                table_names=table_names,
                intent_hint=intent_hint,
            )
            dsl_errors = _validate_dsl(dsl, allowed_tables=allowed_tables)
            if dsl_errors:
                raise RuntimeError("DSL 校验失败: " + ";".join(dsl_errors[:6]))
            semantic_errors = _validate_dsl_table_policy(dsl, table_names or allowed_tables)
            if semantic_errors:
                raise RuntimeError("DSL 语义校验失败: " + ";".join(semantic_errors[:6]))
            return dsl, {
                "success": True,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "errors": errors,
            }
        except Exception as exc:
            err = str(exc)
            errors.append(err[:240])
            print(f"[llm_retry] DSL 生成失败 {attempt}/{max_attempts}: {err}")
            if attempt < max_attempts and retry_interval > 0:
                time.sleep(retry_interval)

    return None, {
        "success": False,
        "attempt": max_attempts,
        "max_attempts": max_attempts,
        "errors": errors,
    }


def _call_deepseek_sql_from_dsl(
    question: str,
    dsl: Dict[str, Any],
    config: Dict,
    db_path: str,
    table_names: Optional[List[str]],
    error_feedback: str = "",
) -> QueryPlan:
    """调用 LLM 基于 DSL 生成 SQL"""
    from app.services.schema_meta import build_schema_prompt

    api_key, url, model, timeout = _get_llm_config(config)
    selected_tables = table_names or []
    if selected_tables:
        schema_prompt = _build_schema_prompt_for_selected_tables(selected_tables)
        if not schema_prompt:
            schema_prompt = build_schema_prompt(db_path)
        table_scope_hint = (
            "# 表访问范围限制\n"
            f"本次只允许使用以下表：{', '.join(selected_tables)}。\n"
            "禁止使用未在列表中的表。\n\n"
        )
    else:
        schema_prompt = build_schema_prompt(db_path)
        table_scope_hint = ""

    system_prompt = _build_sql_from_dsl_system_prompt(schema_prompt, table_scope_hint=table_scope_hint)
    feedback_block = f"\n上轮失败原因: {error_feedback}\n" if error_feedback else ""
    selected_tables_hint = f"\n候选表: {', '.join(selected_tables)}" if selected_tables else ""
    user_prompt = (
        f"用户问题: {question}\n"
        f"{selected_tables_hint}\n"
        f"DSL: {json.dumps(dsl, ensure_ascii=False)}\n"
        f"{feedback_block}"
        "请输出 SQL JSON。"
    )
    content = _call_llm_chat(api_key, url, model, timeout, system_prompt, user_prompt)
    obj = _parse_llm_json(content)

    intent = str(obj.get("intent") or dsl.get("intent") or "list").lower()
    if intent not in {"list", "count"}:
        intent = "count" if dsl.get("intent") == "count" else "list"

    sql = obj.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise RuntimeError("LLM 输出缺少可执行 SQL")

    params = obj.get("params")
    if not isinstance(params, list):
        params = []

    filters = obj.get("filters")
    if not isinstance(filters, dict):
        filters = {}
    filters["dsl"] = dsl

    return QueryPlan(intent=intent, sql=sql, params=params, filters=filters)


def _call_deepseek_sql_from_dsl_with_retry(
    question: str,
    dsl: Dict[str, Any],
    config: Dict,
    db_path: str,
    table_names: Optional[List[str]],
    allowed_tables: Optional[List[str]],
) -> Tuple[Optional[QueryPlan], Dict[str, Any]]:
    """调用 LLM 基于 DSL 生成 SQL 并重试"""
    llm_cfg = config.get("llm", {}) if isinstance(config, dict) else {}
    try:
        max_attempts = int(llm_cfg.get("sql_retry_attempts", llm_cfg.get("retry_attempts", 3)))
    except Exception:
        max_attempts = 3
    max_attempts = max(1, min(6, max_attempts))

    try:
        retry_interval = float(llm_cfg.get("retry_interval_sec", 0.6))
    except Exception:
        retry_interval = 0.6
    retry_interval = max(0.0, min(5.0, retry_interval))

    errors: List[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            feedback = errors[-1] if errors else ""
            plan = _call_deepseek_sql_from_dsl(
                question=question,
                dsl=dsl,
                config=config,
                db_path=db_path,
                table_names=table_names,
                error_feedback=feedback,
            )
            if not _is_plan_sql_valid(plan, allowed_tables=allowed_tables):
                raise RuntimeError("SQL 安全校验失败")
            if not _is_plan_sql_precheck_ok(plan):
                raise RuntimeError("SQL EXPLAIN 预检失败")
            dsl_errors = _validate_sql_matches_dsl(plan.sql, dsl)
            if dsl_errors:
                raise RuntimeError("SQL 与 DSL 不一致: " + ";".join(dsl_errors[:6]))

            filters = dict(plan.filters or {})
            filters["llm_retry"] = {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "errors": errors,
            }
            plan.filters = filters
            return plan, {
                "success": True,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "errors": errors,
            }
        except Exception as exc:
            err = str(exc)
            errors.append(err[:240])
            print(f"[llm_retry] SQL 生成失败 {attempt}/{max_attempts}: {err}")
            if attempt < max_attempts and retry_interval > 0:
                time.sleep(retry_interval)

    return None, {
        "success": False,
        "attempt": max_attempts,
        "max_attempts": max_attempts,
        "errors": errors,
    }


def _has_llm_config(config: Dict) -> bool:
    """是否存在可用 LLM 配置"""
    llm_cfg = config.get("llm", {})
    api_key = os.getenv("LLM_API_KEY") or llm_cfg.get("api_key")
    return bool(api_key)


def _is_sql_text_valid(sql: str) -> bool:
    """SQL 文本是否基本可用"""
    return bool(sql and sql.strip() and "FROM None" not in sql)


def _is_plan_sql_valid(plan: QueryPlan, allowed_tables: Optional[List[str]] = None) -> bool:
    """计划 SQL 是否可执行（含安全校验）"""
    if not _is_sql_text_valid(plan.sql):
        return False
    try:
        from app.services.guardrails import SQLGuardrail
        SQLGuardrail.validate_sql(plan.sql, allowed_tables=allowed_tables or None)
        return True
    except Exception:
        return False


def _is_plan_sql_precheck_ok(plan: QueryPlan) -> bool:
    """通过 DuckDB EXPLAIN 做可执行性预检（字段/表名/占位符）"""
    if not _is_sql_text_valid(plan.sql):
        return False
    try:
        from app.orchestrator_duckdb import get_engine

        engine = get_engine()
        engine.connect()
        result = engine.explain(plan.sql, plan.params or [])
        return bool(result.get("ok"))
    except Exception:
        return False


def _is_generic_list_sql(sql: str) -> bool:
    """是否是通用兜底明细 SQL（SELECT * FROM ... LIMIT N）"""
    normalized = re.sub(r"\s+", " ", (sql or "").strip()).upper()
    if not normalized.startswith("SELECT * FROM "):
        return False
    if any(token in normalized for token in (" WHERE ", " GROUP BY ", " HAVING ", " JOIN ", " UNION ", " ORDER BY ")):
        return False
    return True


def _is_explicit_raw_list_request(question: str) -> bool:
    """用户是否明确要求原始明细列表"""
    q = (question or "").strip()
    if any(hint in q for hint in _RAW_LIST_HINTS):
        return True
    if re.search(r"(前|最近)\s*\d+\s*条", q, flags=re.IGNORECASE):
        return True
    return False


def _should_reject_generic_list(question: str, plan: QueryPlan) -> bool:
    """是否应拒绝执行通用兜底明细 SQL"""
    return (
        plan.intent == "list"
        and _is_generic_list_sql(plan.sql)
        and not _is_explicit_raw_list_request(question)
    )


def _build_reject_plan(intent: str, reason: str, llm_error: str = "") -> QueryPlan:
    """构建拒绝执行的查询计划（用于返回可读错误）"""
    filters: Dict[str, Any] = {
        "plan_rejected": True,
        "reject_reason": reason,
    }
    if llm_error:
        filters["llm_error"] = llm_error[:200]
    return QueryPlan(intent=intent or "list", sql="", params=[], filters=filters)


def _with_plan_context(
    plan: QueryPlan,
    selected_table: Optional[str],
    candidate_tables: List[str],
    table_selection_reason: str,
    plan_source: str,
    candidate_table_ranking: Optional[List[Dict[str, Any]]] = None,
    semantic_judge: Optional[Dict[str, Any]] = None,
    confidence: Optional[float] = None,
) -> QueryPlan:
    """统一补充计划上下文信息"""
    filters = dict(plan.filters or {})
    if selected_table:
        filters["selected_table"] = selected_table
    if candidate_tables:
        filters["candidate_tables"] = candidate_tables
    if candidate_table_ranking:
        filters["candidate_table_ranking"] = candidate_table_ranking[:8]
    if table_selection_reason:
        filters["table_selection_reason"] = table_selection_reason
    filters["plan_source"] = plan_source
    if semantic_judge:
        filters["semantic_judge"] = semantic_judge
    if confidence is not None:
        try:
            filters["confidence"] = max(0.0, min(1.0, float(confidence)))
        except Exception:
            pass
    plan.filters = filters
    return plan


def _auto_correct_intent(plan: QueryPlan) -> QueryPlan:
    """根据 SQL 内容自动纠正 intent"""
    sql_upper = (plan.sql or "").upper()
    has_aggregate = any(fn in sql_upper for fn in ("COUNT(", "SUM(", "AVG("))
    has_group_by = "GROUP BY" in sql_upper
    if has_aggregate and has_group_by and plan.intent != "count":
        print(f"[auto_correct_intent] SQL 包含聚合+GROUP BY，intent 从 '{plan.intent}' 纠正为 'count'")
        plan.intent = "count"
    return plan


def _run_intent_agent(question: str, config: Dict) -> Dict[str, Any]:
    """Intent Agent：优先使用 LLM 识别意图，失败回退轻量规则"""
    fallback_intent = _parse_intent(question)
    if not _has_llm_config(config):
        return {
            "intent": fallback_intent,
            "confidence": 0.55,
            "source": "rule_fallback",
            "reason": "LLM 不可用，使用规则回退",
        }

    try:
        api_key, url, model, timeout = _get_llm_config(config)
        system_prompt = (
            "你是意图识别 Agent。请将用户问题识别为下列四类之一：chat/search/list/count。\n"
            "注意：search 仅用于视觉内容检索（如图片、照片、相似图检索），\n"
            "涉及数据库表/字段/统计/对比/金额/收入/账期等结构化数据问题必须归类为 list 或 count。\n"
            "仅输出 JSON：{\"intent\":\"chat|search|list|count\",\"confidence\":0~1,\"reason\":\"...\"}"
        )
        user_prompt = f"问题: {question}"
        obj = _parse_llm_json(_call_llm_chat(api_key, url, model, timeout, system_prompt, user_prompt))
        intent = str(obj.get("intent") or fallback_intent).lower()
        if intent not in {"chat", "search", "list", "count"}:
            intent = fallback_intent

        reason = str(obj.get("reason") or "LLM intent classify")

        # 强约束：仅视觉语义请求才允许进入向量检索，避免结构化查询被误分流。
        if intent == "search" and not _is_visual_query(question):
            intent = fallback_intent if fallback_intent in {"list", "count"} else "list"
            reason = f"intent_guardrail_override_to_{intent}: non_visual_query"

        try:
            confidence = float(obj.get("confidence", 0.75))
        except Exception:
            confidence = 0.75
        return {
            "intent": intent,
            "confidence": max(0.0, min(1.0, confidence)),
            "source": "llm",
            "reason": reason,
        }
    except Exception as exc:
        return {
            "intent": fallback_intent,
            "confidence": 0.5,
            "source": "rule_fallback",
            "reason": f"intent_agent_fallback:{str(exc)[:120]}",
        }


def _run_reviewer_agent(
    plan: QueryPlan,
    *,
    dsl: Optional[Dict[str, Any]],
    question: str,
    allowed_tables: Optional[List[str]],
) -> Dict[str, Any]:
    """Reviewer Agent：检查安全、可执行性与性能风险"""
    errors: List[str] = []
    warnings: List[str] = []

    if not _is_plan_sql_valid(plan, allowed_tables=allowed_tables):
        errors.append("review_security_guardrail_failed")
    if not _is_plan_sql_precheck_ok(plan):
        errors.append("review_explain_failed")

    if dsl:
        dsl_errors = _validate_sql_matches_dsl(plan.sql, dsl)
        if dsl_errors:
            errors.extend([f"review_dsl_mismatch:{e}" for e in dsl_errors[:6]])

    sql_lower = (plan.sql or "").lower()
    if plan.intent == "list" and "limit" not in sql_lower:
        warnings.append("review_list_query_without_limit")
    if re.search(r"(?is)^\s*select\s+\*", plan.sql or ""):
        warnings.append("review_select_star_detected")
    if not any(tok in question for tok in _RAW_LIST_HINTS) and _is_generic_list_sql(plan.sql):
        warnings.append("review_generic_list_sql")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "score": max(0.0, min(1.0, 1.0 - 0.25 * len(errors) - 0.05 * len(warnings))),
    }


def build_query_plan(text: str, config: Dict, db_path: str = None, table_names: List[str] = None) -> QueryPlan:
    """Multi-Agent 协同：Intent -> Coder -> Reviewer -> Self-Correction"""
    resolved_tables = _resolve_candidate_tables(table_names)
    intent_meta = _run_intent_agent(text, config)
    parsed_intent = str(intent_meta.get("intent") or "list")

    # chat/search 非 SQL 场景，直接返回
    if parsed_intent == "chat":
        return QueryPlan(
            intent="chat",
            sql="",
            params=[],
            filters={
                "plan_source": "llm",
                "agent_mode": "multi_agent",
                "confidence": float(intent_meta.get("confidence", 1.0)),
                "intent_agent": intent_meta,
            },
        )
    if parsed_intent == "search":
        top_k = _parse_top_k(text, default=10)
        return QueryPlan(
            intent="search",
            sql="",
            params=[],
            filters={
                "query_text": text,
                "top_k": top_k,
                "plan_source": "llm",
                "agent_mode": "multi_agent",
                "confidence": float(intent_meta.get("confidence", 1.0)),
                "intent_agent": intent_meta,
            },
        )

    if not resolved_tables:
        reject_reason = "未找到可查询的数据表：请先选择数据表，或检查数据集文件是否已成功加载"
        reject_plan = _build_reject_plan(parsed_intent, reject_reason)
        return _with_plan_context(
            reject_plan,
            selected_table=None,
            candidate_tables=[],
            candidate_table_ranking=[],
            table_selection_reason="",
            plan_source="reject",
            semantic_judge={"winner": "reject", "confidence": 1.0, "reason": reject_reason},
            confidence=0.0,
        )

    default_table = None
    table_selection_reason = ""
    ranked_tables = _rank_candidate_tables(text, resolved_tables, parsed_intent)
    if ranked_tables:
        default_table = ranked_tables[0].get("table")
        table_selection_reason = ranked_tables[0].get("reason", "")
        print(f"[build_query_plan] 候选表: {resolved_tables}")
        print(f"[build_query_plan] 选择表: {default_table}, 原因: {table_selection_reason}")

    if len(ranked_tables) >= 2:
        top1 = ranked_tables[0]
        top2 = ranked_tables[1]
        score_gap = int(top1.get("score", 0)) - int(top2.get("score", 0))
        if score_gap <= 6 and int(top1.get("score", 0)) < 35:
            print(f"[build_query_plan] 多表歧义提醒：top1 与 top2 分差仅 {score_gap}，继续 LLM-DSL 生成")

    llm_cfg = config.get("llm", {})
    llm_enabled = llm_cfg.get("enabled", True)
    llm_available = llm_enabled and _has_llm_config(config)

    if not llm_enabled:
        reject_reason = "当前策略要求必须由 LLM 生成 SQL，但 LLM 已禁用"
        reject_plan = _build_reject_plan(parsed_intent, reject_reason)
        return _with_plan_context(
            reject_plan,
            selected_table=default_table,
            candidate_tables=resolved_tables,
            candidate_table_ranking=ranked_tables,
            table_selection_reason=table_selection_reason,
            plan_source="reject",
            semantic_judge={"winner": "reject", "confidence": 1.0, "reason": reject_reason},
            confidence=0.0,
        )

    if not llm_available:
        reject_reason = "当前策略要求必须由 LLM 生成 SQL，但 LLM 配置不可用"
        reject_plan = _build_reject_plan(parsed_intent, reject_reason)
        return _with_plan_context(
            reject_plan,
            selected_table=default_table,
            candidate_tables=resolved_tables,
            candidate_table_ranking=ranked_tables,
            table_selection_reason=table_selection_reason,
            plan_source="reject",
            semantic_judge={"winner": "reject", "confidence": 1.0, "reason": reject_reason},
            confidence=0.0,
        )

    allowed_tables = resolved_tables if resolved_tables else []

    dsl_plan, dsl_retry_meta = _call_deepseek_nl2dsl_with_retry(
        text,
        config,
        db_path or "",
        table_names=resolved_tables,
        allowed_tables=allowed_tables,
        intent_hint=parsed_intent,
    )
    if not dsl_plan:
        dsl_errors = dsl_retry_meta.get("errors", [])
        llm_error = " | ".join(dsl_errors) if dsl_errors else "DSL 生成失败"
        reject_reason = "LLM 在重试后未生成有效 DSL，请补充更明确条件后重试"
        reject_plan = _build_reject_plan(parsed_intent, reject_reason, llm_error=llm_error)
        judge_result = {
            "winner": "reject",
            "confidence": 1.0,
            "reason": "LLM-DSL 轨道不可用",
        }
        if dsl_retry_meta:
            judge_result["dsl_retry"] = dsl_retry_meta
        return _with_plan_context(
            reject_plan,
            selected_table=default_table,
            candidate_tables=resolved_tables,
            candidate_table_ranking=ranked_tables,
            table_selection_reason=table_selection_reason,
            plan_source="reject",
            semantic_judge=judge_result,
            confidence=0.0,
        )

    llm_plan, sql_retry_meta = _call_deepseek_sql_from_dsl_with_retry(
        question=text,
        dsl=dsl_plan,
        config=config,
        db_path=db_path or "",
        table_names=resolved_tables,
        allowed_tables=allowed_tables,
    )
    if not llm_plan:
        sql_errors = sql_retry_meta.get("errors", [])
        llm_error = " | ".join(sql_errors) if sql_errors else "SQL 生成失败"
        reject_reason = "LLM 在重试后仍未生成可执行 SQL，请补充更明确条件后重试"
        reject_plan = _build_reject_plan(parsed_intent, reject_reason, llm_error=llm_error)
        judge_result = {
            "winner": "reject",
            "confidence": 1.0,
            "reason": "仅允许 LLM 输出 SQL，且 SQL 轨道不可用",
        }
        if dsl_retry_meta:
            judge_result["dsl_retry"] = dsl_retry_meta
        if sql_retry_meta:
            judge_result["sql_retry"] = sql_retry_meta
        return _with_plan_context(
            reject_plan,
            selected_table=default_table,
            candidate_tables=resolved_tables,
            candidate_table_ranking=ranked_tables,
            table_selection_reason=table_selection_reason,
            plan_source="reject",
            semantic_judge=judge_result,
            confidence=0.0,
        )

    selected_table = default_table
    dsl_tables = dsl_plan.get("tables") if isinstance(dsl_plan, dict) else None
    if isinstance(dsl_tables, list) and dsl_tables:
        selected_table = dsl_tables[0]

    reviewer_result = _run_reviewer_agent(
        llm_plan,
        dsl=dsl_plan,
        question=text,
        allowed_tables=allowed_tables,
    )

    self_correction_records: List[Dict[str, Any]] = []
    final_plan = llm_plan
    if not reviewer_result.get("passed"):
        correction_error = "; ".join(reviewer_result.get("errors", [])[:6]) or "Reviewer 检查未通过"
        correction_attempts = 2
        for idx in range(correction_attempts):
            try:
                corrected_plan = call_llm_fix_sql(text, final_plan.sql, correction_error, config, db_path=db_path)
                corrected_review = _run_reviewer_agent(
                    corrected_plan,
                    dsl=dsl_plan,
                    question=text,
                    allowed_tables=allowed_tables,
                )
                self_correction_records.append(
                    {
                        "attempt": idx + 1,
                        "error_feedback": correction_error,
                        "passed": corrected_review.get("passed", False),
                        "review": corrected_review,
                    }
                )
                if corrected_review.get("passed"):
                    final_plan = corrected_plan
                    reviewer_result = corrected_review
                    break
                correction_error = "; ".join(corrected_review.get("errors", [])[:6]) or correction_error
            except Exception as exc:
                self_correction_records.append(
                    {
                        "attempt": idx + 1,
                        "error_feedback": correction_error,
                        "passed": False,
                        "error": str(exc)[:200],
                    }
                )

    if not reviewer_result.get("passed"):
        reject_reason = "Reviewer 检查未通过：SQL 在安全或可执行性上不满足要求"
        reject_plan = _build_reject_plan(final_plan.intent, reject_reason, llm_error="; ".join(reviewer_result.get("errors", [])[:6]))
        judge_result = {
            "winner": "reject",
            "confidence": 1.0,
            "reason": reject_reason,
            "intent_agent": intent_meta,
            "dsl_retry": dsl_retry_meta,
            "sql_retry": sql_retry_meta,
            "reviewer": reviewer_result,
            "self_correction": self_correction_records,
        }
        return _with_plan_context(
            reject_plan,
            selected_table=selected_table,
            candidate_tables=resolved_tables,
            candidate_table_ranking=ranked_tables,
            table_selection_reason=table_selection_reason,
            plan_source="reject",
            semantic_judge=judge_result,
            confidence=0.0,
        )

    judge_result = {
        "winner": "multi_agent",
        "confidence": max(0.65, min(0.98, float(reviewer_result.get("score", 0.85)))),
        "reason": "Intent/Coder/Reviewer/Self-Correction 协同完成 SQL 规划",
        "intent_agent": intent_meta,
        "dsl_retry": dsl_retry_meta,
        "sql_retry": sql_retry_meta,
        "reviewer": reviewer_result,
        "self_correction": self_correction_records,
    }

    final_filters = dict(final_plan.filters or {})
    final_filters["agent_mode"] = "multi_agent"
    final_plan.filters = final_filters

    final_plan = _auto_correct_intent(final_plan)
    return _auto_correct_intent(
        _with_plan_context(
            final_plan,
            selected_table=selected_table,
            candidate_tables=resolved_tables,
            candidate_table_ranking=ranked_tables,
            table_selection_reason=table_selection_reason,
            plan_source="llm",
            semantic_judge=judge_result,
            confidence=float(judge_result.get("confidence", 0.8)),
        )
    )


def call_llm_fix_sql(question: str, failed_sql: str, error_msg: str,
                     config: Dict, db_path: str = None) -> QueryPlan:
    """
    调用 LLM 修正失败的 SQL

    Args:
        question: 原始用户问题
        failed_sql: 执行失败的 SQL
        error_msg: 错误信息
        config: 配置字典
        db_path: 数据库路径

    Returns:
        修正后的 QueryPlan
    """
    from app.services.schema_meta import build_schema_prompt

    api_key, url, model, timeout = _get_llm_config(config)
    schema_prompt = build_schema_prompt(db_path or "")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_prompt = (
        "你是一个 SQL 修正助手。用户之前生成的 SQL 执行失败了，请根据错误信息修正 SQL。\n\n"
        f"# 当前时间\n{now_str}\n\n"
        "# 数据库 Schema\n"
        f"{schema_prompt}\n\n"
        "# 关键规则\n"
        "1. 直接查询表即可。\n"
        "2. SQL 中的值必须使用 `?` 占位符（DuckDB 参数化查询）。\n"
        "3. 如果是列表查询，SELECT 中包含必要的展示字段。\n"
        "4. 只允许 SELECT 查询。\n\n"
        "# 输出格式\n"
        "严格输出一个 JSON 对象：\n"
        '{"intent": "count|list", "sql": "...", "params": [...], "filters": {...}}\n'
    )

    user_prompt = (
        f"原始问题: {question}\n"
        f"失败的 SQL: {failed_sql}\n"
        f"错误信息: {error_msg}\n\n"
        "请修正 SQL 并直接输出 JSON。"
    )

    content = _call_llm_chat(api_key, url, model, timeout, system_prompt, user_prompt)
    obj = _parse_llm_json(content)

    intent = obj.get("intent", "list")
    sql = obj.get("sql", "")
    params = obj.get("params", [])
    filters = obj.get("filters", {})

    return _auto_correct_intent(QueryPlan(intent=intent, sql=sql, params=params, filters=filters))
