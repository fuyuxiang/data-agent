"""Skill resolution: from a user question to a ranked set of skills.

Resolution is deliberately deterministic and explainable.  Every candidate
carries the reasons it matched so the UI can answer "为什么系统选了这个技能",
and an explicitly requested skill (``@数据分析`` / ``/数据分析``) always wins
over scoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import SkillDefinition

# Explicit request syntax: @skill / /skill, matched against id or name.
_EXPLICIT = re.compile(r"[@/]\s*([A-Za-z0-9一-鿿][A-Za-z0-9一-鿿 _-]{0,39})")

# Words that only carry weight when the question also contains a data verb.
_INTENT_MARKERS: dict[str, tuple[str, ...]] = {
    "query": ("多少", "是多少", "查询", "查一下", "统计", "多少个", "列出", "看"),
    "trend": ("趋势", "走势", "变化", "同比", "环比", "每月", "趋势图", "增长"),
    "attribution": ("为什么", "原因", "归因", "导致", "造成", "下降原因", "什么影响"),
    "forecast": ("预测", "预计", "未来", "下个月", "下月", "展望", "会怎样"),
    "anomaly": ("异常", "波动", "突变", "离群", "反常"),
    "compare": ("对比", "相比", "排名", "top", "最高", "最低", "哪个"),
    "research": ("研究", "调研", "综述", "深入研究", "报告"),
    "file": ("excel", "表格", "csv", "文件", "上传"),
    "visual": ("图", "图表", "可视化", "画", "趋势图", "饼图", "柱状"),
}

# Function words carry no signal when scoring example-question overlap.
_STOPWORDS = frozenset({
    "帮我", "请", "把", "这份", "那个", "这个", "一个", "一下", "我们", "你们", "他们",
    "的", "了", "是", "和", "与", "在", "对", "有", "会", "要", "看看", "看下",
})

# Tokens that, when matched, are strong evidence for a skill.
_STRONG_TOKENS = ("深度研究", "ppt", "excel", "报告", "预测", "归因", "可视化", "导出", "下载")


@dataclass(frozen=True)
class Scored:
    definition: SkillDefinition
    score: float
    reasons: tuple[str, ...]

    def to_public(self) -> dict:
        return {
            "id": self.definition.id,
            "name": self.definition.name,
            "description": self.definition.description,
            "category": self.definition.category,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class Resolution:
    query: str
    selected: tuple[SkillDefinition, ...]
    candidates: tuple[Scored, ...]
    explicit: tuple[str, ...]
    rejected: tuple[dict, ...]

    def to_public(self) -> dict:
        return {
            "query": self.query,
            "explicit": list(self.explicit),
            "selected": [item.to_card() for item in self.selected],
            "candidates": [item.to_public() for item in self.candidates],
            "rejected": list(self.rejected),
        }


def extract_explicit(query: str) -> tuple[str, ...]:
    """Return the skill names a user asked for by name, if any.

    The capture is greedy so that names containing spaces ("Excel 分析") survive,
    which means it can also swallow the rest of the question.  Callers resolve
    the token with :func:`match_known` to trim it back.
    """
    return tuple(dict.fromkeys(match.strip() for match in _EXPLICIT.findall(str(query or ""))))


def match_known(token: str, definitions: Sequence[SkillDefinition]) -> str:
    """Trim a greedy ``@``/``/`` token down to the longest known skill.

    Returns the skill's canonical id, or an empty string when the token names
    nothing the workspace knows about — a typo must not silently bind a skill.
    """
    resolver = SkillResolver(definitions)
    whole = token.strip()
    if not whole:
        return ""
    matched = resolver.by_name(whole)
    if matched is not None:
        return matched.id
    words = whole.split()
    for count in range(len(words) - 1, 0, -1):
        prefix = " ".join(words[:count])
        if len(prefix) < 2:
            continue
        matched = resolver.by_name(prefix)
        if matched is not None:
            return matched.id
    return ""


def _normalize(text: str) -> str:
    return re.sub(r"[\s,，。？?！!、:：;；'\"“”‘’()（）]", "", str(text or "")).lower()


def _text_score(definition: SkillDefinition, query: str) -> tuple[float, list[str]]:
    """Score a skill by lexical overlap with its name, triggers and examples."""
    haystack = query.lower()
    normalized_query = _normalize(query)
    score = 0.0
    reasons: list[str] = []

    name = definition.name.lower()
    if name and name in haystack:
        score += 3.0
        reasons.append(f"问题提到「{definition.name}」")

    for trigger in definition.triggers:
        token = str(trigger).lower()
        if not token:
            continue
        if token in haystack:
            score += 1.4
            reasons.append(f"命中触发场景「{trigger}」")
        elif _normalize(token) and _normalize(token) in normalized_query:
            score += 1.0
            reasons.append(f"命中触发场景「{trigger}」")

    for example in definition.example_questions:
        tokens = [
            token for token in re.split(r"[\s,，。？?！!、]", example)
            if len(token) > 1 and token not in _STOPWORDS
        ]
        overlap = sum(1 for token in tokens if _normalize(token) in normalized_query)
        if overlap >= 2 or (len(example) > 6 and _normalize(example) in normalized_query):
            score += 1.8
            reasons.append("与示例问题相近")
            break

    for token in _STRONG_TOKENS:
        if token in haystack and token in (name + " " + definition.description).lower():
            score += 0.8
            reasons.append(f"问题包含「{token}」")

    if definition.description and _normalize(definition.description[:12]) and (
        _normalize(definition.description[:12]) in normalized_query
    ):
        score += 0.6
        reasons.append("与技能描述接近")

    return score, reasons


def _intent_scores(definition: SkillDefinition, query: str) -> tuple[float, list[str]]:
    """Boost skills whose category matches the question's analytical intent."""
    haystack = query.lower()
    matched_markers = {
        intent: [word for word in words if word in haystack]
        for intent, words in _INTENT_MARKERS.items()
    }
    matched = {intent: hits for intent, hits in matched_markers.items() if hits}
    if not matched:
        return 0.0, []

    category_affinity = {
        "数据分析": {"query", "trend", "attribution", "anomaly", "compare"},
        "深度研究": {"research", "attribution"},
        "报告": {"research", "visual"},
        "办公": {"file", "visual"},
        "文件分析": {"file"},
        "可视化": {"visual", "compare", "trend"},
    }.get(definition.category, set())

    score = 0.0
    reasons: list[str] = []
    for intent in sorted(matched):
        if intent in category_affinity:
            score += 0.9
            reasons.append(f"问题包含「{matched[intent][0]}」，属于{_intent_label(intent)}")
    return score, reasons


def _intent_label(intent: str) -> str:
    return {
        "query": "取数", "trend": "趋势", "attribution": "归因", "forecast": "预测",
        "anomaly": "异常", "compare": "对比", "research": "研究", "file": "文件",
        "visual": "可视化",
    }.get(intent, intent)


class SkillResolver:
    """Rank skills against a question, honouring explicit requests first."""

    def __init__(self, definitions: Sequence[SkillDefinition], *, limit: int = 5):
        self.definitions = list(definitions)
        self.limit = limit

    def by_name(self, name: str) -> SkillDefinition | None:
        needle = _normalize(name)
        for definition in self.definitions:
            if _normalize(definition.name) == needle or definition.id == needle:
                return definition
        for definition in self.definitions:
            if needle and (needle in _normalize(definition.name) or needle in definition.id):
                return definition
        return None

    def resolve(
        self,
        query: str,
        *,
        explicit: Iterable[str] | None = None,
        rejected: Iterable[dict] | None = None,
        threshold: float = 2.0,
    ) -> Resolution:
        requested = tuple(dict.fromkeys(
            str(value) for value in (explicit if explicit is not None else extract_explicit(query))
        ))

        chosen: list[SkillDefinition] = []
        matched_explicit: list[str] = []
        for name in requested:
            definition = self.by_name(name)
            if definition is not None:
                chosen.append(definition)
                matched_explicit.append(name)
        chosen_ids = {item.id for item in chosen}

        scored: list[Scored] = []
        for definition in self.definitions:
            if definition.id in chosen_ids:
                continue
            lexical, lexical_reasons = _text_score(definition, query)
            intent, intent_reasons = _intent_scores(definition, query)
            total = lexical + intent
            if total <= 0:
                continue
            scored.append(Scored(definition, total, tuple(lexical_reasons + intent_reasons)))
        scored.sort(key=lambda item: (-item.score, item.definition.name))
        top = scored[: self.limit]
        if not chosen:
            # A single strong match is enough; several weak ones mean no skill is
            # auto-selected at all. Forcing a weak match would push a vague
            # question into a report or export skill that clearly does not fit.
            strong = [item for item in top if item.score >= max(threshold * 3, 4.0)]
            chosen = [item.definition for item in strong] or [
                item.definition for item in top[:1] if item.score >= threshold
            ]

        return Resolution(
            query=str(query or ""),
            selected=tuple(chosen),
            candidates=tuple(top),
            explicit=matched_explicit,
            rejected=tuple(rejected or ()),
        )
