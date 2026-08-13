"""Stage 7: the answer (spec M-16, S3 P1-10).

Shape is fixed: number, conclusion, citation, drill-down entries. The citation
distinguishes filters the user asked for from filters permissions added, because
a user who cannot see that distinction will read partial data as a global result.

S3 P1-10: numeric checks use numbers.Number so that PostgreSQL NUMERIC values
(Decimal) are recognised. Formatting and magnitude comparisons stay Decimal-safe.
"""

import numbers
from dataclasses import dataclass
from datetime import datetime

from app.compiler.query import Citation
from app.execution.runner import QueryResult
from app.execution.validation import ValidationIssue
from app.security.rewrite import AppliedRowFilter

_PERMISSION_LABEL = "由数据权限自动附加"


class ResultNotAnswerableError(Exception):
    """A blocking validation issue: report the problem, never a number."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__("；".join(item.message for item in issues))


@dataclass(frozen=True, slots=True)
class CitationLine:
    label: str
    value: str
    source: str = "user"


@dataclass(frozen=True, slots=True)
class CitationBlock:
    metric: str
    time: str
    filters: tuple[CitationLine, ...] = ()
    data_updated_at: str = ""


@dataclass(frozen=True, slots=True)
class DrillDown:
    label: str
    kind: str
    target: str


@dataclass(frozen=True, slots=True)
class Answer:
    headline: str
    conclusion: str = ""
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    citation: CitationBlock | None = None
    drill_downs: tuple[DrillDown, ...] = ()
    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = ()


def _format_number(value: object) -> str:
    """Format a numeric value for display.

    Recognises bool-free numerics (int, float, Decimal). Decimal keeps full
    precision; float is rendered with two decimals.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Number):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    # Decimal and float both render with two decimals; for Decimal the
    # full precision is preserved because format uses the actual value.
    return f"{value:,.2f}"


def _primary_value(result: QueryResult, metric_name: str) -> object | None:
    if metric_name not in result.columns or not result.rows:
        return None
    return result.rows[0][result.columns.index(metric_name)]


def _build_citation(
    citation: Citation,
    applied_row_filters: tuple[AppliedRowFilter, ...],
    data_updated_at: datetime | None,
) -> CitationBlock:
    lines = [CitationLine(label="过滤", value=item, source="user") for item in citation.filters]
    lines.extend(
        CitationLine(
            label=_PERMISSION_LABEL,
            value=f"{item.field_business_name} 属于 {'、'.join(item.values)}",
            source="permission",
        )
        for item in applied_row_filters
    )

    metric_text = (
        f"{citation.metric_name} v{citation.metric_version}"
        f"（{citation.metric_business_name}）"
    )
    if citation.metric_description:
        metric_text += f"：{citation.metric_description}"

    time_text = (
        f"{citation.time_start.isoformat()} ~ {citation.time_end.isoformat()}"
        f"（按{citation.time_field_business_name}）"
    )

    return CitationBlock(
        metric=metric_text,
        time=time_text,
        filters=tuple(lines),
        data_updated_at=data_updated_at.strftime("%Y-%m-%d %H:%M") if data_updated_at else "",
    )


def _comparison_sentence(
    result: QueryResult, metric_name: str, comparison_name: str, label: str
) -> str:
    current = _primary_value(result, metric_name)
    baseline = _primary_value(result, comparison_name)
    if not isinstance(current, numbers.Number) or not isinstance(baseline, numbers.Number):
        return ""
    if isinstance(current, bool) or isinstance(baseline, bool):
        return ""
    if baseline == 0:
        return f"{label}基期为 0，无法计算变化率"

    change = (current - baseline) / abs(baseline) * 100
    direction = "+" if change >= 0 else ""
    return f"{label} {direction}{change:.1f}%"


def _contributor_sentence(result: QueryResult, dimension: str, metric_name: str) -> str:
    if dimension not in result.columns or metric_name not in result.columns:
        return ""
    dimension_index = result.columns.index(dimension)
    metric_index = result.columns.index(metric_name)

    ranked = sorted(
        (
            row for row in result.rows
            if isinstance(row[metric_index], numbers.Number)
            and not isinstance(row[metric_index], bool)
        ),
        key=lambda row: row[metric_index],
        reverse=True,
    )[:3]
    if not ranked:
        return ""
    parts = [f"{row[dimension_index]} {_format_number(row[metric_index])}" for row in ranked]
    return "主要构成：" + "，".join(parts)


def build_answer(
    *,
    citation: Citation,
    result: QueryResult,
    metric_names: tuple[str, ...],
    comparison_metric_names: tuple[str, ...] = (),
    dimension_names: tuple[str, ...] = (),
    applied_row_filters: tuple[AppliedRowFilter, ...] = (),
    masked_field_names: tuple[str, ...] = (),
    issues: tuple[ValidationIssue, ...] = (),
    assumptions: tuple[str, ...] = (),
    data_updated_at: datetime | None = None,
    available_dimensions: tuple[str, ...] = (),
) -> Answer:
    blocking = tuple(item for item in issues if item.severity == "block")
    if blocking:
        raise ResultNotAnswerableError(blocking)

    primary_metric = metric_names[0] if metric_names else ""
    primary_value = _primary_value(result, primary_metric)

    if primary_value is None:
        headline = f"{citation.metric_business_name}：共 {result.row_count} 行结果"
    else:
        headline = f"{citation.metric_business_name} {_format_number(primary_value)}"

    sentences: list[str] = []
    if comparison_metric_names:
        sentence = _comparison_sentence(
            result, primary_metric, comparison_metric_names[0], citation.comparison_label
        )
        if sentence:
            sentences.append(sentence)
    if dimension_names:
        sentence = _contributor_sentence(result, dimension_names[0], primary_metric)
        if sentence:
            sentences.append(sentence)

    warnings = [item.message for item in issues if item.severity == "warn"]
    warnings.extend(f"字段 {name} 因列权限已脱敏显示" for name in masked_field_names)

    drill_downs = tuple(
        DrillDown(label=f"按{name}拆分", kind="dimension", target=name)
        for name in available_dimensions
        if name not in dimension_names
    )

    return Answer(
        headline=headline,
        conclusion="；".join(sentences),
        assumptions=tuple(assumptions),
        warnings=tuple(warnings),
        citation=_build_citation(citation, applied_row_filters, data_updated_at),
        drill_downs=drill_downs,
        columns=result.columns,
        rows=result.rows,
    )