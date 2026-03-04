"""
NL2SQL 规则引擎与 LLM 生成

功能：
1. 意图识别（6种：chat, list, count, search, analysis, skip）
2. 时间范围解析
3. 地点解析（街道/区县）
4. 置信度解析
5. TOP K 解析
6. 规则引擎 SQL 生成
7. LLM SQL 生成
8. SQL 缓存命中
9. SQL 自动修正
"""

import calendar
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
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


def _parse_time_range(text: str) -> Tuple[Optional[str], Optional[str]]:
    """解析时间范围"""
    # 1) 精确日期范围: 2025-01-01 ~ 2025-01-31
    date_matches = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if len(date_matches) >= 2:
        return date_matches[0] + " 00:00:00", date_matches[1] + " 23:59:59"

    # 2) "近N天/小时"
    match = re.search(r"近(\d+)(天|小时)", text)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        end = datetime.now()
        start = end - (timedelta(days=value) if unit == "天" else timedelta(hours=value))
        return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")

    # 3) "YYYY年M月" — 整月范围
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        _, last_day = calendar.monthrange(year, month)
        return f"{year}-{month:02d}-01 00:00:00", f"{year}-{month:02d}-{last_day} 23:59:59"

    # 4) "最近N天"
    match = re.search(r"最近(\d+)(天|小时)", text)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        end = datetime.now()
        start = end - (timedelta(days=value) if unit == "天" else timedelta(hours=value))
        return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")

    return None, None


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
    "告警", "事件", "设备", "街道", "区县", "置信度", "工单",
    "最近", "今天", "昨天", "本月", "本周",
]

# 结构化查询强信号
_STRUCTURED_KEYWORDS = [
    "统计", "多少", "数量", "总数", "分布", "TOP", "top", "排名",
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

_SCHEMA_COUNT_KEYWORDS = [
    "多少字段", "多少列", "多少特征", "字段数量", "列数量", "特征数量",
    "字段个数", "列个数", "特征个数", "字段数", "列数", "特征数",
]

_SCHEMA_LIST_KEYWORDS = [
    "有哪些字段", "所有字段", "字段列表", "列名", "字段名", "字段都有哪些", "当前字段",
]

_ROW_COUNT_HINTS = [
    "多少条", "多少行", "记录数", "总记录", "总条数", "数据量",
]

_RAW_LIST_HINTS = [
    "明细", "详情", "样例", "样本", "原始数据",
]


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
        if any(k in t for k in ["多少", "统计", "数量", "总数", "分布", "TOP", "top", "排名"]):
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


def _is_schema_count_query(text: str) -> bool:
    """是否是字段/列数量问题"""
    t = text.replace(" ", "")
    return any(k in t for k in _SCHEMA_COUNT_KEYWORDS)


def _is_schema_list_query(text: str) -> bool:
    """是否是字段列表问题"""
    t = text.replace(" ", "")
    return any(k in t for k in _SCHEMA_LIST_KEYWORDS)


def _is_row_count_query(text: str) -> bool:
    """是否是数据行数问题"""
    t = text.replace(" ", "")
    return any(k in t for k in _ROW_COUNT_HINTS)


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


def _select_best_table(text: str, table_names: List[str], intent: str) -> Tuple[Optional[str], str]:
    """多表场景下，根据问题和字段匹配自动选择最相关表"""
    if not table_names:
        return None, "未提供候选表"
    if len(table_names) == 1:
        return table_names[0], "仅选择了 1 张表"

    text_lower = text.lower()
    text_norm = _normalize_name_for_match(text)
    best_table = table_names[0]
    best_score = -1
    best_reason = "默认使用首个候选表"

    # 业务关键词 -> 字段关键词
    semantic_hints = {
        "渠道": ["渠道", "channel"],
        "产品": ["产品", "prod", "product"],
        "用户": ["用户", "user", "acct"],
        "收入": ["收入", "fee", "amount", "计费", "出账"],
    }

    for idx, table_name in enumerate(table_names):
        score = 0
        reasons: List[str] = []

        # 1) 表名直接命中（最高优先级）
        table_lower = table_name.lower()
        table_norm = _normalize_name_for_match(_normalize_table_name(table_name))
        if table_lower in text_lower or (table_norm and table_norm in text_norm):
            score += 100
            reasons.append("问题中直接提到表名")

        # 2) 表名片段命中（如 prod/channel）
        name_tokens = [t for t in re.split(r"[_\-.]+", table_lower) if len(t) >= 2]
        token_hits = sum(1 for token in name_tokens if token in text_lower)
        if token_hits:
            score += token_hits * 10
            reasons.append(f"表名关键词命中 {token_hits} 次")

        # 3) 字段命中
        schema = _get_table_schema(table_name)
        col_names = [c.get("column_name", "") for c in schema if c.get("column_name")]
        if col_names:
            col_name_hits = sum(1 for col in col_names if col.lower() in text_lower)
            if col_name_hits:
                score += col_name_hits * 20
                reasons.append(f"字段名命中 {col_name_hits} 次")

            # 4) 意图/语义相关字段命中
            col_names_lower = [c.lower() for c in col_names]
            for hint, keywords in semantic_hints.items():
                if hint in text:
                    semantic_hits = sum(
                        1 for col in col_names_lower if any(k in col for k in keywords)
                    )
                    if semantic_hits:
                        score += min(semantic_hits, 3) * 6
                        reasons.append(f"{hint}相关字段匹配")

            # 5) 统计场景下，能推断分组字段的表优先
            if intent == "count" and _infer_group_by_column(text, col_names):
                score += 8
                reasons.append("可推断统计维度")

        # 稳定排序：同分优先用户选择顺序靠前
        score += max(0, 2 - idx)

        if score > best_score:
            best_score = score
            best_table = table_name
            best_reason = "；".join(reasons) if reasons else "未命中明显特征，按选择顺序兜底"

    return best_table, best_reason


def _infer_group_by_column(text: str, columns: List[str]) -> Optional[Tuple[str, str]]:
    """根据问题文本推断 GROUP BY 字段"""
    text_lower = text.lower()

    # 渠道相关关键词
    if any(k in text_lower for k in ["渠道", "渠道类型", "发展渠道", "渠道分类", "渠道聚类"]):
        for col in columns:
            if "渠道" in col or "channel" in col.lower():
                return col, col
        # 备选：找包含"一级"、"二级"、"三级"的字段
        for col in columns:
            if "一级" in col or "二级" in col or "三级" in col:
                return col, col

    # 省份相关
    if any(k in text_lower for k in ["省", "省份", "市", "城市", "地区"]):
        for col in columns:
            if "省" in col or "prov" in col.lower() or "市" in col or "城市" in col:
                return col, col

    # 时间相关（按月统计）
    if any(k in text_lower for k in ["月", "月份", "每月"]):
        for col in columns:
            if "month" in col.lower() or "月份" in col:
                return col, col

    # 产品相关
    if any(k in text_lower for k in ["产品", "业务", "类型"]):
        for col in columns:
            if "产品" in col or "业务" in col or "prod" in col.lower():
                return col, col

    # 用户相关
    if any(k in text_lower for k in ["用户"]):
        for col in columns:
            if "用户" in col or "user" in col.lower():
                return col, col

    return None


def _build_smart_sql(text: str, table_name: str, intent: str) -> Tuple[str, List, Dict]:
    """根据表结构智能生成 SQL"""
    columns = _get_table_schema(table_name)
    if not columns:
        return "", [], {}

    col_names = [c["column_name"] for c in columns]
    col_types = {c["column_name"]: c["column_type"] for c in columns}

    # 解析时间范围
    start_time, end_time = _parse_time_range(text)

    # 解析 TOP K
    top_k = _parse_top_k(text)

    # 推断 GROUP BY 字段
    group_result = _infer_group_by_column(text, col_names)

    where = []
    params = []

    # 时间字段处理
    time_col = None
    for col in col_names:
        if "month" in col.lower():
            time_col = col
            break

    if start_time and time_col:
        # month_id 是YYYYMM格式
        start_month = start_time[:7].replace("-", "")
        end_month = end_time[:7].replace("-", "")
        where.append(f"{time_col} >= ?")
        params.append(start_month)
        where.append(f"{time_col} <= ?")
        params.append(end_month)

    where_sql = " WHERE " + " AND ".join(where) if where else ""

    if intent == "count":
        # 统计查询
        if group_result:
            group_col, group_alias = group_result
            sql = (
                f"SELECT {group_col} AS {group_alias}, COUNT(*) AS 数量 FROM {table_name}"
                + where_sql
                + f" GROUP BY {group_col} ORDER BY 数量 DESC"
            )
        else:
            # 没有明确的分组字段，返回总数
            sql = f"SELECT COUNT(*) AS 数量 FROM {table_name}" + where_sql

    else:
        # 列表查询
        sql = (
            f"SELECT * FROM {table_name}"
            + where_sql
            + f" LIMIT {top_k}"
        )

    filters = {
        "start_time": start_time,
        "end_time": end_time,
        "top_k": top_k,
    }

    return sql, params, filters


def _build_schema_meta_sql(text: str, table_name: str) -> QueryPlan:
    """字段/列/特征相关问题，使用 information_schema.columns 查询"""
    if _is_schema_count_query(text):
        sql = (
            "SELECT COUNT(*) AS 字段数量 "
            "FROM information_schema.columns "
            "WHERE table_name = ?"
        )
        return QueryPlan(
            intent="count",
            sql=sql,
            params=[table_name],
            filters={"schema_query": True, "schema_action": "count_columns"}
        )

    # 字段列表（默认）
    sql = (
        "SELECT column_name AS 字段名, data_type AS 字段类型 "
        "FROM information_schema.columns "
        "WHERE table_name = ? "
        "ORDER BY ordinal_position "
        "LIMIT 500"
    )
    return QueryPlan(
        intent="list",
        sql=sql,
        params=[table_name],
        filters={"schema_query": True, "schema_action": "list_columns"}
    )


def parse_question(text: str, table_name: str = None) -> QueryPlan:
    """
    使用规则引擎解析用户问题 - 智能版

    Args:
        text: 用户问题
        table_name: 表名

    Returns:
        QueryPlan 查询计划
    """
    intent = _parse_intent(text)

    # 闲聊意图：不生成 SQL
    if intent == "chat":
        return QueryPlan(intent="chat", sql="", params=[], filters={})

    # 向量检索意图：不生成 SQL
    if intent == "search":
        top_k = _parse_top_k(text, default=10)
        return QueryPlan(intent="search", sql="", params=[], filters={"query_text": text, "top_k": top_k})

    # 如果没有指定表名，无法生成 SQL
    if not table_name:
        print("[parse_question] 未指定表名，返回空查询计划")
        return QueryPlan(intent=intent, sql="", params=[], filters={})

    # 字段/列/特征问题优先走 schema 元数据查询
    if _is_schema_count_query(text) or _is_schema_list_query(text):
        plan = _build_schema_meta_sql(text, table_name)
        print(f"[parse_question] 识别为 schema 查询: {plan.filters.get('schema_action')}")
        return plan

    # 使用智能 SQL 生成
    sql, params, filters = _build_smart_sql(text, table_name, intent)
    print(f"[parse_question] 智能生成 SQL: {sql[:100]}...")

    return QueryPlan(intent=intent, sql=sql, params=params, filters=filters)


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
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    candidate = _extract_first_json_object(text) or text
    try:
        return json.loads(candidate)
    except Exception as exc:
        raise RuntimeError(f"LLM 输出不是有效 JSON: {content}") from exc


def _build_nl2sql_system_prompt(schema_prompt: str, table_scope_hint: str = "") -> str:
    """构建 NL2SQL 的 system prompt"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "你是一个专业的 NL2SQL 助手，负责将中文自然语言问题转换为 DuckDB SQL 查询。\n\n"
        f"# 当前时间\n{now_str}\n"
        "所有涉及'最近N天'、'本月'、'今天'、'昨天'等相对时间的表达，都必须基于上面的当前时间计算。\n\n"
        "# 数据库 Schema\n"
        f"{schema_prompt}\n\n"
        f"{table_scope_hint}"
        "# 重要：数据库结构说明\n"
        "所有数据存储在一张统一的 `embeddings` 表中，表名根据实际数据确定。\n"
        "你可以直接查询对应的表。\n\n"
        "# 实体提取规则\n"
        "1. **区分量词和地名**：'20条'中的'条'是量词，不是地名的一部分。\n"
        "2. **常见量词**：条、个、件、次、项、篇、张 — 这些紧跟数字时是数量单位，不是地名前缀。\n"
        "3. **地名后缀识别**：街道、镇、乡、区、县、市、省 — 这些是地名标志。\n"
        "4. **时间表达式**：'最近N天'、'本月'、'今天' 等需要转换为具体日期范围。\n"
        "5. **'最近N条' ≠ '最近N天'**：'最近20条'表示 ORDER BY ... DESC LIMIT 20，"
        "**绝对不要**添加时间过滤条件！\n\n"
        "# intent 分类规则\n"
        "- **count**：用户问'统计'、'数量'、'多少'、'分布'、'TOP'、'排名'，或 SQL 包含 COUNT/SUM/AVG + GROUP BY\n"
        "- **list**：用户问'查询'、'查看'、'详细信息'、'明细'，需要返回逐行记录\n\n"
        "# SQL 生成规则\n"
        "1. 直接查询表即可。\n"
        "2. 时间字段根据实际 schema 确定。\n"
        "3. SQL 中的值必须用 `$1`, `$2`, ... 占位符（DuckDB 参数化查询），对应的值放在 params 数组中。\n"
        "4. 如果是列表查询，SELECT 中包含必要的展示字段。\n"
        "5. 如果是统计查询，建议带 GROUP BY 分组维度和 ORDER BY 数量 DESC。\n"
        "6. 只允许 SELECT 查询，禁止 INSERT/UPDATE/DELETE/DROP 等写操作。\n"
        "7. 列表查询默认 LIMIT 20，除非用户指定了数量。\n\n"
        "# 输出格式\n"
        "严格输出一个 JSON 对象，不要包含任何多余文字、注释、`<think>` 标签或 markdown 代码块：\n"
        '{"intent": "count|list", "sql": "...", "params": [...], "filters": {...}}\n'
    )


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


def _call_deepseek_nl2sql(
    question: str,
    config: Dict,
    fallback: QueryPlan,
    db_path: str,
    table_names: Optional[List[str]] = None
) -> QueryPlan:
    """调用 DeepSeek LLM 生成 SQL"""
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

    system_prompt = _build_nl2sql_system_prompt(schema_prompt, table_scope_hint=table_scope_hint)
    user_prompt = f"问题: {question}\n请直接输出 JSON。"

    content = _call_llm_chat(api_key, url, model, timeout, system_prompt, user_prompt)
    obj = _parse_llm_json(content)

    intent = obj.get("intent") or fallback.intent
    sql = obj.get("sql") or fallback.sql
    params = obj.get("params") or fallback.params
    filters = obj.get("filters") or fallback.filters

    # 确保 filters 至少包含必要键
    merged_filters = dict(fallback.filters)
    if isinstance(filters, dict):
        merged_filters.update(filters)

    return QueryPlan(intent=intent, sql=sql, params=params, filters=merged_filters)


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


def _build_semantic_judge_prompt(question: str, rule_plan: QueryPlan, llm_plan: QueryPlan) -> Tuple[str, str]:
    """构建语义一致性裁决 prompt"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_prompt = (
        "你是 SQL 语义一致性裁决器。请比较两条 SQL 谁更符合用户问题。\n\n"
        f"# 当前时间\n{now_str}\n\n"
        "# 裁决规则\n"
        "1. 只判断语义匹配，不评价性能。\n"
        "2. 若问题是“字段/列/特征数量或列表”，优先选择查询 information_schema.columns 的 SQL。\n"
        "3. 若问题是“数据有多少条/多少行记录”，优先选择 COUNT(*) 统计数据行数的 SQL。\n"
        "4. 若两条都明显不匹配，winner 设为 reject。\n\n"
        "# 输出格式\n"
        "只输出 JSON，不要输出 `<think>` 标签："
        '{"winner":"rule|llm|reject","confidence":0-1,"reason":"简要原因","rule_score":0-100,"llm_score":0-100}\n'
    )

    user_prompt = (
        f"用户问题: {question}\n\n"
        "候选A（rule）:\n"
        f"intent={rule_plan.intent}\n"
        f"sql={rule_plan.sql}\n"
        f"params={rule_plan.params}\n\n"
        "候选B（llm）:\n"
        f"intent={llm_plan.intent}\n"
        f"sql={llm_plan.sql}\n"
        f"params={llm_plan.params}\n\n"
        "请输出 JSON 裁决结果。"
    )
    return system_prompt, user_prompt


def _call_llm_semantic_judge(
    question: str,
    rule_plan: QueryPlan,
    llm_plan: QueryPlan,
    config: Dict
) -> Dict[str, Any]:
    """调用 LLM 对 rule/llm 两条 SQL 进行语义一致性裁决"""
    api_key, url, model, timeout = _get_llm_config(config)
    system_prompt, user_prompt = _build_semantic_judge_prompt(question, rule_plan, llm_plan)
    content = _call_llm_chat(api_key, url, model, timeout, system_prompt, user_prompt)
    obj = _parse_llm_json(content)

    winner = str(obj.get("winner", "rule")).lower()
    if winner not in {"rule", "llm", "reject"}:
        winner = "rule"

    try:
        confidence = float(obj.get("confidence", 0.5))
    except Exception:
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return {
        "winner": winner,
        "confidence": confidence,
        "reason": str(obj.get("reason", ""))[:200],
        "rule_score": obj.get("rule_score"),
        "llm_score": obj.get("llm_score"),
    }


def _plan_looks_like_schema_query(plan: QueryPlan) -> bool:
    sql_upper = (plan.sql or "").upper()
    return "INFORMATION_SCHEMA.COLUMNS" in sql_upper


def _plan_looks_like_row_count(plan: QueryPlan) -> bool:
    sql_upper = (plan.sql or "").upper()
    return (
        "COUNT(*)" in sql_upper and
        "INFORMATION_SCHEMA.COLUMNS" not in sql_upper and
        "GROUP BY" not in sql_upper
    )


def _apply_semantic_hard_rules(question: str, rule_plan: QueryPlan, llm_plan: QueryPlan) -> Optional[Dict[str, Any]]:
    """硬规则兜底（优先于 LLM 裁决）"""
    if _is_schema_count_query(question) or _is_schema_list_query(question):
        rule_schema = _plan_looks_like_schema_query(rule_plan)
        llm_schema = _plan_looks_like_schema_query(llm_plan)
        if rule_schema and not llm_schema:
            return {"winner": "rule", "confidence": 1.0, "reason": "字段/列问题优先 schema 查询（硬规则）"}
        if llm_schema and not rule_schema:
            return {"winner": "llm", "confidence": 1.0, "reason": "字段/列问题优先 schema 查询（硬规则）"}

    if _is_row_count_query(question):
        rule_row_count = _plan_looks_like_row_count(rule_plan)
        llm_row_count = _plan_looks_like_row_count(llm_plan)
        if rule_row_count and not llm_row_count:
            return {"winner": "rule", "confidence": 1.0, "reason": "条数问题优先行数统计（硬规则）"}
        if llm_row_count and not rule_row_count:
            return {"winner": "llm", "confidence": 1.0, "reason": "条数问题优先行数统计（硬规则）"}

    return None


def _with_plan_context(
    plan: QueryPlan,
    selected_table: Optional[str],
    candidate_tables: List[str],
    table_selection_reason: str,
    plan_source: str,
    semantic_judge: Optional[Dict[str, Any]] = None
) -> QueryPlan:
    """统一补充计划上下文信息"""
    filters = dict(plan.filters or {})
    if selected_table:
        filters["selected_table"] = selected_table
    if candidate_tables:
        filters["candidate_tables"] = candidate_tables
    if table_selection_reason:
        filters["table_selection_reason"] = table_selection_reason
    filters["plan_source"] = plan_source
    if semantic_judge:
        filters["semantic_judge"] = semantic_judge
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


def build_query_plan(text: str, config: Dict, db_path: str = None, table_names: List[str] = None) -> QueryPlan:
    """
    构建查询计划 - 完整流程：规则引擎 → LLM fallback

    Args:
        text: 用户问题
        config: 配置字典
        db_path: 数据库路径
        table_names: 可用的表名列表

    Returns:
        QueryPlan 查询计划
    """
    # 解析并选择候选表（多表可用）
    resolved_tables = _resolve_candidate_tables(table_names)
    parsed_intent = _parse_intent(text)
    default_table = None
    table_selection_reason = ""
    if resolved_tables:
        default_table, table_selection_reason = _select_best_table(text, resolved_tables, parsed_intent)
        print(f"[build_query_plan] 候选表: {resolved_tables}")
        print(f"[build_query_plan] 选择表: {default_table}, 原因: {table_selection_reason}")

    llm_cfg = config.get("llm", {})
    llm_enabled = llm_cfg.get("enabled", True)  # 默认启用 LLM
    llm_available = llm_enabled and _has_llm_config(config)

    # 轨道 A：规则引擎
    rule_plan = parse_question(text, table_name=default_table)
    rule_plan = _with_plan_context(
        rule_plan,
        selected_table=default_table,
        candidate_tables=resolved_tables,
        table_selection_reason=table_selection_reason,
        plan_source="rule"
    )

    # 闲聊意图直接返回
    if rule_plan.intent == "chat":
        print("[build_query_plan] 识别为闲聊，跳过 LLM")
        return rule_plan

    # 向量检索意图直接返回
    if rule_plan.intent == "search":
        print("[build_query_plan] 识别为视觉内容检索，跳过 LLM")
        return rule_plan

    allowed_tables = resolved_tables if resolved_tables else None
    rule_valid = _is_plan_sql_valid(rule_plan, allowed_tables=allowed_tables)

    # 轨道 B：LLM（只要可用就执行，和规则轨道做双轨校验）
    llm_plan: Optional[QueryPlan] = None
    llm_valid = False
    llm_error = ""
    if llm_available:
        try:
            llm_plan = _call_deepseek_nl2sql(
                text,
                config,
                rule_plan,
                db_path or "",
                table_names=resolved_tables
            )
            llm_plan = _with_plan_context(
                llm_plan,
                selected_table=default_table,
                candidate_tables=resolved_tables,
                table_selection_reason=table_selection_reason,
                plan_source="llm"
            )
            llm_valid = _is_plan_sql_valid(llm_plan, allowed_tables=allowed_tables)
            print(f"[build_query_plan] LLM 轨道完成, valid={llm_valid}, intent={llm_plan.intent}")
        except Exception as e:
            llm_error = str(e)
            print(f"[build_query_plan] LLM 轨道失败: {llm_error}")
    elif llm_enabled and not llm_available:
        llm_error = "LLM 配置不可用，跳过 LLM 轨道"
        print(f"[build_query_plan] {llm_error}")

    # 双轨都有效：进入语义一致性裁决
    if rule_valid and llm_valid and llm_plan:
        judge_result = _apply_semantic_hard_rules(text, rule_plan, llm_plan)
        if judge_result:
            print(f"[build_query_plan] 硬规则裁决: {judge_result}")
        else:
            try:
                judge_result = _call_llm_semantic_judge(text, rule_plan, llm_plan, config)
                print(f"[build_query_plan] LLM 语义裁决: {judge_result}")
            except Exception as e:
                judge_result = {
                    "winner": "rule",
                    "confidence": 0.0,
                    "reason": f"语义裁决失败，回退规则轨道: {e}",
                }
                print(f"[build_query_plan] {judge_result['reason']}")

        winner = str(judge_result.get("winner", "rule")).lower()
        if winner == "llm":
            return _auto_correct_intent(
                _with_plan_context(
                    llm_plan,
                    selected_table=default_table,
                    candidate_tables=resolved_tables,
                    table_selection_reason=table_selection_reason,
                    plan_source="llm",
                    semantic_judge=judge_result
                )
            )
        if winner == "reject":
            reject_reason = "无法可靠理解你的问题，请补充更明确的统计口径或筛选条件后重试"
            reject_plan = _build_reject_plan(rule_plan.intent, reject_reason, llm_error=llm_error)
            return _with_plan_context(
                reject_plan,
                selected_table=default_table,
                candidate_tables=resolved_tables,
                table_selection_reason=table_selection_reason,
                plan_source="reject",
                semantic_judge=judge_result
            )

        if _should_reject_generic_list(text, rule_plan):
            reject_reason = "当前问题语义较抽象，系统拒绝返回通用样本明细。请补充“按什么维度、什么时间、什么指标”"
            reject_plan = _build_reject_plan(rule_plan.intent, reject_reason, llm_error=llm_error)
            return _with_plan_context(
                reject_plan,
                selected_table=default_table,
                candidate_tables=resolved_tables,
                table_selection_reason=table_selection_reason,
                plan_source="reject",
                semantic_judge=judge_result
            )

        return _auto_correct_intent(
            _with_plan_context(
                rule_plan,
                selected_table=default_table,
                candidate_tables=resolved_tables,
                table_selection_reason=table_selection_reason,
                plan_source="rule",
                semantic_judge=judge_result
            )
        )

    # 单轨可用：直接使用可用轨道
    if llm_valid and llm_plan and not rule_valid:
        judge_result = {
            "winner": "llm",
            "confidence": 1.0,
            "reason": "规则轨道无效，使用 LLM 轨道",
        }
        return _auto_correct_intent(
            _with_plan_context(
                llm_plan,
                selected_table=default_table,
                candidate_tables=resolved_tables,
                table_selection_reason=table_selection_reason,
                plan_source="llm",
                semantic_judge=judge_result
            )
        )

    if rule_valid:
        if _should_reject_generic_list(text, rule_plan):
            reject_reason = "当前问题语义较抽象，系统拒绝返回通用样本明细。请补充“按什么维度、什么时间、什么指标”"
            reject_plan = _build_reject_plan(rule_plan.intent, reject_reason, llm_error=llm_error)
            judge_result = {
                "winner": "reject",
                "confidence": 1.0,
                "reason": "规则轨道仅生成通用明细 SQL，已拒绝执行",
            }
            return _with_plan_context(
                reject_plan,
                selected_table=default_table,
                candidate_tables=resolved_tables,
                table_selection_reason=table_selection_reason,
                plan_source="reject",
                semantic_judge=judge_result
            )

        judge_result = {
            "winner": "rule",
            "confidence": 1.0,
            "reason": "规则轨道有效，LLM 轨道不可用" if not llm_valid else "规则轨道优先",
        }
        if llm_error:
            judge_result["llm_error"] = llm_error[:200]
        return _auto_correct_intent(
            _with_plan_context(
                rule_plan,
                selected_table=default_table,
                candidate_tables=resolved_tables,
                table_selection_reason=table_selection_reason,
                plan_source="rule",
                semantic_judge=judge_result
            )
        )

    # 都无效：尽量返回规则结果（保持兼容）
    if llm_error:
        rule_plan.filters["llm_error"] = llm_error[:200]
    print("[build_query_plan] 双轨都无有效 SQL，回退规则结果")
    return _auto_correct_intent(rule_plan)


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
        "2. SQL 中的值必须用 `$1`, `$2`, ... 占位符（DuckDB 参数化查询）。\n"
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
