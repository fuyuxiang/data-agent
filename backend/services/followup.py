"""Carry forward a business scope without confirming a superseded period."""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TIME_CLARIFICATION = "请选择本次分析的时间范围"
_TIME_MENTION = re.compile(
    r"\d{4}\s*(?:年|[-/])|(?:\d{1,2}|十一|十二|[一二三四五六七八九十])\s*月|"
    r"(?:本|上|下|这)(?:个)?月|今年|去年|前年|明年|季度|半年|(?:本|上|下|这)周|今天|昨天|前天|明天|"
    r"(?:最近?|过去|近)\s*(?:\d+|[一二三四五六七八九十两]+)\s*(?:个)?(?:月|天|日|周|年)|"
    r"旺季|淡季|节假日|假期|春节|国庆|五一|财年|年初|年末|"
    r"时间(?:范围|改|换|调整)|日期|\b(?:month|year|quarter|period|january|february|march|april|may|june|"
    r"july|august|september|october|november|december)\b", re.IGNORECASE,
)
_SCOPE_EDIT = re.compile(r"改(?:为|成|看|用|按|查)|换(?:成|为|看)|调整(?:为|成)|仅(?:看|统计)|只(?:看|统计)")
_MONTHS = {name: index for index, name in enumerate(
    ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), 1,
)}


def valid_time_range(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        return date.fromisoformat(value["start"]) <= date.fromisoformat(value["end"])
    except (KeyError, TypeError, ValueError):
        return False


def _period(question: str, previous: dict, timezone: str) -> dict[str, str]:
    """Resolve common dates only; comparisons and business seasons need a choice.

    Date-only ends are inclusive, matching the governed metric compiler.
    """
    try:
        today = datetime.now(ZoneInfo(timezone)).date()
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
        if dates:
            value = {"start": dates[0], "end": dates[-1]}
            return value if len(dates) == 2 and valid_time_range(value) else {}
        months = re.findall(r"(?:(\d{4})\s*年\s*)?(\d{1,2}|十一|十二|[一二三四五六七八九十])\s*月", question)
        if len(months) == 1:
            if re.search(r"今年|去年|前年|明年|同比|环比", question):
                return {}
            year, month = months[0]
            year = int(year or str(previous.get("start") or today.isoformat())[:4])
            month = int(month) if month.isdigit() else _MONTHS[month]
            return {"start": date(year, month, 1).isoformat(),
                    "end": date(year, month, calendar.monthrange(year, month)[1]).isoformat()}
        relative = re.findall(r"本(?:个)?月|这(?:个)?月|上(?:个)?月|今年|去年|今天|昨天|前天", question)
        if months or len(relative) != 1:
            return {}
        if relative[0] in {"本月", "本个月", "这个月", "这月"}:
            start, end = today.replace(day=1), today
        elif relative[0] in {"上月", "上个月"}:
            end = today.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
        elif relative[0] in {"今天", "昨天", "前天"}:
            start = end = today - timedelta(days={"今天": 0, "昨天": 1, "前天": 2}[relative[0]])
        elif relative[0] == "今年":
            start, end = today.replace(month=1, day=1), today
        else:
            start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        return {"start": start.isoformat(), "end": end.isoformat()}
    except (ValueError, KeyError, ZoneInfoNotFoundError):
        return {}


def followup_payload(payload: dict, previous: dict) -> tuple[dict, dict | None]:
    """Explicit fields win; scope changes produce a reviewable new brief."""
    inherited = {key: previous[key] for key in ("time_range", "timezone", "coverage", "dimensions", "metrics")
                 if key in previous}
    raw = dict(payload.get("contract") or {}) if isinstance(payload.get("contract"), dict) else dict(payload)
    question = str(raw.get("objective") or payload.get("objective") or payload.get("message") or "")
    raw.setdefault("objective", question)
    time_question = re.sub(r"(?:同一|相同|原有?|沿用(?:原)?)(?:时间范围|统计时间|月份|时间)", "", question)
    scope_question = re.sub(
        r"(?:改(?:为|成)|换(?:为|成))\s*(?:Word|PPT|PowerPoint|PDF|Excel|HTML|DOCX|XLSX|网页报告|表格|图表|报告)",
        "整理交付格式", question, flags=re.IGNORECASE,
    )
    changed_time = ("time_range" in raw and raw["time_range"] != previous.get("time_range", {}))
    changed_time = changed_time or bool(_TIME_MENTION.search(time_question))
    changed_scope = any(key in raw and raw[key] != previous.get(key) for key in ("coverage", "dimensions", "metrics"))
    scope_change = None
    if changed_time:
        value = raw.get("time_range") if "time_range" in raw else _period(
            question, previous.get("time_range") or {}, str(raw.get("timezone") or previous.get("timezone") or "Asia/Shanghai"),
        )
        raw["time_range"] = value or {}
        raw.setdefault("coverage", f"按本次问题调整统计时间：{question}。其他筛选条件沿用原分析。")
        scope_change = {
            "kind": "time", "requires_time_selection": not valid_time_range(value),
            "message": "本次问题调整了统计时间，请核对新的时间范围；其他口径沿用原分析。",
        }
        if scope_change["requires_time_selection"]:
            raw["unresolved"] = list(dict.fromkeys([*(raw.get("unresolved") or []), TIME_CLARIFICATION]))
    elif changed_scope or _SCOPE_EDIT.search(scope_question):
        raw.setdefault("metrics", [])
        raw.setdefault("dimensions", ["整体"])
        raw.setdefault("coverage", f"按本次问题调整统计口径：{question}。未变更的条件沿用原分析。")
        scope_change = {"kind": "scope", "requires_time_selection": False,
                        "message": "本次问题调整了统计口径，请核对范围、维度和指标后开始分析。"}
    merged = {**inherited, **raw}
    result = {**payload, "contract": merged}
    if scope_change:
        result["confirm_required"] = True
    return result, scope_change
