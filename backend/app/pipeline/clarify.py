"""Clarification requests (spec 5.2, M-18).

Options are always concrete and clickable — the user picks, never retypes. When
the round limit is hit the first option becomes the default, but the assumption
is returned so the answer can state it: an unstated default assumption is a
silent error.
"""

from dataclasses import dataclass
from enum import Enum

_KIND_LABELS = {
    "metric": "指标口径",
    "time": "时间范围",
    "dimension": "分组维度",
    "entity": "筛选取值",
    "dataset": "数据集",
}


class ClarifyKind(str, Enum):
    METRIC = "metric"
    TIME = "time"
    DIMENSION = "dimension"
    ENTITY = "entity"
    DATASET = "dataset"


@dataclass(frozen=True, slots=True)
class ClarifyOption:
    value: str
    label: str
    hint: str = ""


@dataclass(frozen=True, slots=True)
class ClarifyRequest:
    kind: ClarifyKind
    target: str
    question: str
    options: tuple[ClarifyOption, ...] = ()


def default_assumption(request: ClarifyRequest) -> tuple[str, str] | None:
    if not request.options:
        return None
    chosen = request.options[0]
    label = _KIND_LABELS[request.kind.value]
    return request.target, f"{label}未确认，已默认按「{chosen.label}」处理"