"""Semantic lint (spec M-07).

A dataset with any ERROR-level issue must not be usable for querying.
This is the first quality gate: misconfigured semantics do not raise errors
at query time, they return plausible wrong numbers.
"""

import re
from dataclasses import dataclass
from enum import Enum

from app.semantic.enums import AggregationBehavior, MetricKind, SemanticType
from app.semantic.model import DatasetDef

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Tokens that may appear in a composite/ratio expression without being a metric.
_EXPRESSION_KEYWORDS = frozenset({"nullif", "coalesce", "case", "when", "then", "else", "end"})


class LintSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class LintIssue:
    code: str
    severity: str
    target: str
    message: str


def _check_dataset(dataset: DatasetDef) -> list[LintIssue]:
    issues: list[LintIssue] = []
    if not dataset.grain.strip():
        issues.append(
            LintIssue(
                code="DATASET_NO_GRAIN",
                severity=LintSeverity.WARNING.value,
                target=dataset.name,
                message="数据集未声明粒度，问数时无法判断是否需要去重",
            )
        )
    return issues


def _check_fields(dataset: DatasetDef) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for item in dataset.fields:
        if not item.business_name.strip():
            issues.append(
                LintIssue(
                    code="FIELD_NO_BUSINESS_NAME",
                    severity=LintSeverity.ERROR.value,
                    target=f"{dataset.name}.{item.name}",
                    message="字段缺少业务名，意图识别无法命中该字段",
                )
            )
        if item.semantic_type == SemanticType.ENUM.value and not item.enum_values:
            issues.append(
                LintIssue(
                    code="ENUM_NO_DICTIONARY",
                    severity=LintSeverity.ERROR.value,
                    target=f"{dataset.name}.{item.name}",
                    message="枚举字段缺少值字典，用户说业务值时必然查空",
                )
            )
        if item.default_aggregation != "none" and (
            item.default_aggregation not in item.allowed_aggregations
        ):
            issues.append(
                LintIssue(
                    code="METRIC_AGG_NOT_ALLOWED",
                    severity=LintSeverity.ERROR.value,
                    target=f"{dataset.name}.{item.name}",
                    message="字段默认聚合不在允许聚合列表内",
                )
            )
    return issues


def _check_metric_time_field(dataset: DatasetDef, metric) -> list[LintIssue]:
    if not metric.time_field.strip() or not dataset.has_field(metric.time_field):
        return [
            LintIssue(
                code="METRIC_NO_TIME_FIELD",
                severity=LintSeverity.ERROR.value,
                target=f"{dataset.name}.{metric.name}",
                message="指标未声明有效的时间口径字段",
            )
        ]
    if dataset.field(metric.time_field).semantic_type != SemanticType.DATE.value:
        return [
            LintIssue(
                code="METRIC_NO_TIME_FIELD",
                severity=LintSeverity.ERROR.value,
                target=f"{dataset.name}.{metric.name}",
                message="指标的时间口径字段不是日期类型",
            )
        ]
    return []


def _check_metrics(dataset: DatasetDef) -> list[LintIssue]:
    issues: list[LintIssue] = []
    metric_names = {item.name for item in dataset.metrics}

    for metric in dataset.metrics:
        target = f"{dataset.name}.{metric.name}"
        issues.extend(_check_metric_time_field(dataset, metric))

        if metric.kind in (MetricKind.ATOMIC.value, MetricKind.DERIVED.value):
            if not metric.source_field or not dataset.has_field(metric.source_field):
                issues.append(
                    LintIssue(
                        code="METRIC_BAD_FIELD_REF",
                        severity=LintSeverity.ERROR.value,
                        target=target,
                        message="指标引用了不存在的字段",
                    )
                )
            elif metric.aggregation not in dataset.field(metric.source_field).allowed_aggregations:
                issues.append(
                    LintIssue(
                        code="METRIC_AGG_NOT_ALLOWED",
                        severity=LintSeverity.ERROR.value,
                        target=target,
                        message=f"字段 {metric.source_field} 不允许 {metric.aggregation} 聚合",
                    )
                )

        if metric.kind in (MetricKind.COMPOSITE.value, MetricKind.RATIO.value):
            referenced = {
                token
                for token in _IDENTIFIER_RE.findall(metric.expression)
                if token.casefold() not in _EXPRESSION_KEYWORDS
            }
            missing = referenced - metric_names
            if missing:
                issues.append(
                    LintIssue(
                        code="METRIC_BAD_METRIC_REF",
                        severity=LintSeverity.ERROR.value,
                        target=target,
                        message=f"指标表达式引用了不存在的指标：{', '.join(sorted(missing))}",
                    )
                )

        if (
            metric.kind == MetricKind.RATIO.value
            and metric.aggregation_behavior != AggregationBehavior.RECALCULATE.value
        ):
            issues.append(
                LintIssue(
                    code="RATIO_METRIC_ADDITIVE",
                    severity=LintSeverity.ERROR.value,
                    target=target,
                    message="比率指标必须标注为 recalculate，否则汇总与下钻必然算错",
                )
            )

    return issues


def lint_dataset(dataset: DatasetDef) -> list[LintIssue]:
    return [
        *_check_dataset(dataset),
        *_check_fields(dataset),
        *_check_metrics(dataset),
    ]


def is_publishable(dataset: DatasetDef) -> bool:
    """A dataset is publishable when it has no ERROR-level issue."""
    return not any(
        issue.severity == LintSeverity.ERROR.value for issue in lint_dataset(dataset)
    )
