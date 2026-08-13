"""Result validation (spec M-15).

Blocking issues stop the answer: reporting a number that came from an empty or
all-NULL result is exactly the silent-error failure mode the product exists to
prevent. Warnings still answer, but say what looks off.
"""

from dataclasses import dataclass
from enum import Enum

from app.execution.runner import QueryResult

# A change beyond this multiple against the baseline is treated as suspicious.
_MAGNITUDE_FACTOR = 10.0


class ValidationCode(str, Enum):
    EMPTY_RESULT = "empty_result"
    ALL_NULL = "all_null"
    FILTER_TOO_NARROW = "filter_too_narrow"
    ROW_COUNT_TRUNCATED = "row_count_truncated"
    MAGNITUDE_SHIFT = "magnitude_shift"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: ValidationCode
    severity: str  # "block" | "warn"
    message: str


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_result(
    result: QueryResult,
    *,
    has_filters: bool,
    comparison_columns: dict[str, str] | None = None,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []

    if result.row_count == 0:
        if has_filters:
            issues.append(
                ValidationIssue(
                    ValidationCode.FILTER_TOO_NARROW,
                    "block",
                    "当前筛选条件下没有数据，可能是过滤条件过窄，请确认筛选范围",
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    ValidationCode.EMPTY_RESULT,
                    "block",
                    "该时间范围内没有数据",
                )
            )
        return tuple(issues)

    all_null_indexes = [
        index
        for index, _ in enumerate(result.columns)
        if all(row[index] is None for row in result.rows)
    ]
    if all_null_indexes:
        names = "、".join(result.columns[index] for index in all_null_indexes)
        # Spec 5.5: an aggregate over no rows still returns one row with NULL —
        # report this as "no data" rather than "metric broken" so the user is
        # not sent chasing a phantom bug.
        issues.append(
            ValidationIssue(
                ValidationCode.ALL_NULL,
                "block",
                f"该查询没有数据，字段 {names} 的取值全部为空",
            )
        )

    if result.truncated:
        issues.append(
            ValidationIssue(
                ValidationCode.ROW_COUNT_TRUNCATED,
                "warn",
                f"结果已截断至 {result.row_count} 行，可能不完整",
            )
        )

    for current, baseline in (comparison_columns or {}).items():
        if current not in result.columns or baseline not in result.columns:
            continue
        current_index = result.columns.index(current)
        baseline_index = result.columns.index(baseline)

        for row in result.rows:
            current_value = row[current_index]
            baseline_value = row[baseline_index]
            if not (_is_number(current_value) and _is_number(baseline_value)):
                continue
            if baseline_value == 0:
                continue
            if abs(current_value) > abs(baseline_value) * _MAGNITUDE_FACTOR:
                issues.append(
                    ValidationIssue(
                        ValidationCode.MAGNITUDE_SHIFT,
                        "warn",
                        f"{current} 相比对比期变化超过 {int(_MAGNITUDE_FACTOR)} 倍，建议核对口径",
                    )
                )
                break

    return tuple(issues)