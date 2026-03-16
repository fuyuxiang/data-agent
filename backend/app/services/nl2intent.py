"""
意图识别服务。
"""

from typing import Any, Dict

from app.services.llm_utils import call_llm_chat, get_llm_config, has_llm_config, parse_llm_json

SUPPORTED_INTENTS = {"chat", "search", "list", "count"}

# 闲聊模式
_CHAT_PATTERNS = [
    "你好",
    "您好",
    "hello",
    "hi",
    "嗨",
    "hey",
    "谢谢",
    "感谢",
    "再见",
    "拜拜",
    "bye",
    "你是谁",
    "你叫什么",
    "你能做什么",
    "你会什么",
    "怎么用",
    "使用说明",
    "帮助",
    "今天天气",
    "讲个笑话",
    "你好吗",
]

_QUERY_KEYWORDS = [
    "查询",
    "查看",
    "搜索",
    "查找",
    "查一下",
    "找一下",
    "列出",
    "统计",
    "多少",
    "数量",
    "总数",
    "分布",
    "TOP",
    "top",
    "排名",
    "对比",
    "比较",
    "差异",
    "vs",
    "VS",
    "告警",
    "事件",
    "设备",
    "街道",
    "区县",
    "置信度",
    "工单",
    "最近",
    "今天",
    "昨天",
    "本月",
    "本周",
]

_COUNT_KEYWORDS = {
    "多少",
    "统计",
    "数量",
    "总数",
    "分布",
    "TOP",
    "top",
    "排名",
    "对比",
    "比较",
    "差异",
    "vs",
    "VS",
    "构成",
    "结构",
    "占比",
    "组成",
    "拆分",
}

_STRUCTURED_KEYWORDS = [
    "统计",
    "多少",
    "数量",
    "总数",
    "分布",
    "TOP",
    "top",
    "排名",
    "对比",
    "比较",
    "差异",
    "vs",
    "VS",
    "按街道",
    "按设备",
    "按类型",
    "按区",
    "按算法",
    "GROUP",
    "group",
]

_VISUAL_KEYWORDS = [
    "红色",
    "蓝色",
    "白色",
    "黑色",
    "黄色",
    "绿色",
    "灰色",
    "橙色",
    "棕色",
    "挖掘机",
    "卡车",
    "轿车",
    "货车",
    "吊车",
    "推土机",
    "铲车",
    "摩托",
    "自行车",
    "人",
    "行人",
    "工人",
    "安全帽",
    "围栏",
    "塔吊",
    "电线杆",
    "铁塔",
    "沙地",
    "土坡",
    "工地",
    "草地",
    "马路",
    "停车场",
    "河边",
    "树林",
    "停在",
    "停放",
    "行驶",
    "施工",
    "挖掘",
    "倒塌",
    "倾斜",
    "找图",
    "找图片",
    "搜图",
    "类似的",
    "相似的",
    "像这样",
    "长什么样",
    "有没有",
    "有什么图",
    "图片",
    "照片",
]


def is_visual_query(text: str) -> bool:
    """判断是否为视觉内容描述类查询。"""
    if any(keyword in text for keyword in _STRUCTURED_KEYWORDS):
        return False
    return sum(1 for keyword in _VISUAL_KEYWORDS if keyword in text) >= 1


def parse_intent(text: str) -> str:
    """规则化解析用户意图。"""
    cleaned = text.strip()
    if is_visual_query(cleaned):
        return "search"
    if any(keyword in cleaned for keyword in _QUERY_KEYWORDS):
        if any(keyword in cleaned for keyword in _COUNT_KEYWORDS):
            return "count"
        return "list"

    cleaned_lower = cleaned.lower()
    if any(pattern in cleaned_lower for pattern in _CHAT_PATTERNS):
        return "chat"
    if len(cleaned) <= 6:
        return "chat"
    return "list"


def run_intent_agent(question: str, config: Dict[str, Any], context_prompt: str = "") -> Dict[str, Any]:
    """优先使用 LLM 识别意图，失败时回退到规则判断。"""
    fallback_intent = parse_intent(question)
    if not has_llm_config(config):
        return {
            "intent": fallback_intent,
            "confidence": 0.55,
            "source": "rule_fallback",
            "reason": "LLM 不可用，使用规则回退",
        }

    try:
        api_key, url, model, timeout = get_llm_config(config)
        system_prompt = (
            "你是意图识别 Agent。请将用户问题识别为下列四类之一：chat/search/list/count。\n"
            "注意：search 仅用于视觉内容检索（如图片、照片、相似图检索），\n"
            "涉及数据库表/字段/统计/对比/金额/收入/账期等结构化数据问题必须归类为 list 或 count。\n"
            "若用户当前提问明显是在追问上一轮结果，可结合短会话上下文补全理解；若当前问题已经完整自洽，以当前问题为准。\n"
            "仅输出 JSON：{\"intent\":\"chat|search|list|count\",\"confidence\":0~1,\"reason\":\"...\"}"
        )
        user_prompt = f"{context_prompt}问题: {question}"
        obj = parse_llm_json(call_llm_chat(api_key, url, model, timeout, system_prompt, user_prompt))

        intent = str(obj.get("intent") or fallback_intent).lower()
        if intent not in SUPPORTED_INTENTS:
            intent = fallback_intent

        reason = str(obj.get("reason") or "LLM intent classify")
        if intent == "search" and not is_visual_query(question):
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
