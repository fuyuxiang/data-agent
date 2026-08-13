"""Prompt for the one LLM stage.

Deliberately omits enum values: dictionaries are queried by the resolver
(spec 4.3), so the model reports what the user said ("华东") and never guesses
a physical value. Also omits any SQL vocabulary — the model's only job is to
fill in slots.
"""

import json

from app.semantic.model import DatasetDef

_SYSTEM = """你是一个企业数据问答系统的意图识别模块。

你的唯一任务：把用户的自然语言问题转换为结构化意图 JSON。

严格要求：
1. 绝对不要生成 SQL、表名、列名或任何查询语句。SQL 由系统的编译器生成。
2. 只能使用下方给出的指标名、维度名与过滤字段名，不得自行发明或推测。
3. 过滤条件只填写用户口语中的表述（spoken_values），不要翻译成数据库里的取值。
4. 每个关键槽位都要给出置信度 confidence，取值 0 到 1；不确定就给低分，不要为了填满而猜。
5. 如果问题不是数据查询（例如要求下单、修改数据、闲聊），kind 填 unsupported。
6. 如果做了任何默认假设（例如「最近」按本月理解），写入 assumptions 数组。

只输出 JSON，不要输出解释文字。JSON 结构：
{
  "kind": "aggregate | trend | ranking | detail | unsupported",
  "metrics": ["指标名"],
  "dimensions": ["维度名"],
  "filters": [{"field": "字段名", "operator": "eq|ne|in|not_in|gt|gte|lt|lte|between",
               "spoken_values": ["用户原话"]}],
  "time": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
           "grain": "day|week|month|quarter|year", "expression": "用户原话"},
  "comparison": "none|mom|yoy|wow|qoq|ytd|mtd|qtd|previous_period",
  "sort": {"by": "指标名", "descending": true, "limit": 10},
  "confidence": {"overall": 0.0, "metric": 0.0, "time": 0.0,
                 "dimension": 0.0, "filter": 0.0},
  "assumptions": ["做出的默认假设"]
}"""


def _metric_lines(dataset: DatasetDef) -> str:
    lines = []
    for metric in dataset.metrics:
        parts = [f"- {metric.name}（{metric.business_name}）"]
        if metric.synonyms:
            parts.append(f"同义词：{'、'.join(metric.synonyms)}")
        if metric.description:
            parts.append(f"口径：{metric.description}")
        lines.append("；".join(parts))
    return "\n".join(lines)


def _field_lines(dataset: DatasetDef, *, groupable: bool) -> str:
    lines = []
    for field in dataset.fields:
        if groupable and not field.is_groupable:
            continue
        if not groupable and not field.is_filterable:
            continue
        label = f"- {field.name}（{field.business_name or field.name}）"
        if field.synonyms:
            label += f"；同义词：{'、'.join(field.synonyms)}"
        lines.append(label)
    return "\n".join(lines)


def build_intent_prompt(
    dataset: DatasetDef, question: str, slot_state: dict | None = None
) -> tuple[str, str]:
    blocks = [
        f"数据集：{dataset.name}（{dataset.business_name}）",
        f"数据粒度：{dataset.grain}",
        f"适用场景：{dataset.applicable_scenario}",
        f"禁用场景：{dataset.forbidden_scenario}",
        f"可用指标\n{_metric_lines(dataset)}",
        f"可用维度\n{_field_lines(dataset, groupable=True)}",
        f"可用过滤字段\n{_field_lines(dataset, groupable=False)}",
    ]

    if slot_state:
        # Follow-up questions like 「那华南呢」 only replace one slot.
        blocks.append(
            "上一轮的查询状态（用户可能只是想改动其中一部分）\n"
            + json.dumps(slot_state, ensure_ascii=False, sort_keys=True)
        )

    blocks.append(f"用户问题：{question}")
    return _SYSTEM, "\n\n".join(blocks)