"""Result validation: block empty / all-NULL, warn on truncation / magnitude shift."""

from app.execution.runner import QueryResult
from app.execution.validation import ValidationCode, validate_result


def _result(columns, rows, *, truncated=False) -> QueryResult:
    return QueryResult(
        columns=tuple(columns),
        rows=tuple(tuple(row) for row in rows),
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=1,
    )


def _codes(issues):
    return {issue.code for issue in issues}


def test_normal_result_has_no_issues():
    issues = validate_result(_result(["region", "total"], [("EC", 100)]), has_filters=False)
    assert issues == ()


def test_empty_result_without_filters_is_reported_as_no_data():
    issues = validate_result(_result(["total"], []), has_filters=False)

    assert ValidationCode.EMPTY_RESULT in _codes(issues)
    assert ValidationCode.FILTER_TOO_NARROW not in _codes(issues)


def test_empty_result_with_filters_points_at_the_filters():
    issues = validate_result(_result(["total"], []), has_filters=True)

    # The distinction matters: "no data" and "your filters removed it" need
    # different follow-ups.
    assert ValidationCode.FILTER_TOO_NARROW in _codes(issues)


def test_empty_result_is_blocking():
    issues = validate_result(_result(["total"], []), has_filters=False)
    assert all(issue.severity == "block" for issue in issues)


def test_all_null_metric_is_blocking():
    issues = validate_result(
        _result(["region", "total"], [("EC", None), ("SC", None)]), has_filters=False
    )

    assert ValidationCode.ALL_NULL in _codes(issues)
    assert all(
        issue.severity == "block"
        for issue in issues
        if issue.code == ValidationCode.ALL_NULL
    )


def test_partially_null_metric_is_not_flagged():
    issues = validate_result(
        _result(["region", "total"], [("EC", None), ("SC", 100)]), has_filters=False
    )
    assert ValidationCode.ALL_NULL not in _codes(issues)


def test_truncated_result_warns():
    issues = validate_result(
        _result(["region"], [("EC",)], truncated=True), has_filters=False
    )

    assert ValidationCode.ROW_COUNT_TRUNCATED in _codes(issues)
    assert [
        issue.severity
        for issue in issues
        if issue.code == ValidationCode.ROW_COUNT_TRUNCATED
    ] == ["warn"]


def test_magnitude_shift_warns_but_does_not_block():
    result = _result(
        ["sales_revenue", "sales_revenue_comparison"], [(1_000_000, 1_000)]
    )
    issues = validate_result(
        result,
        has_filters=False,
        comparison_columns={"sales_revenue": "sales_revenue_comparison"},
    )

    assert ValidationCode.MAGNITUDE_SHIFT in _codes(issues)
    # Spec 5.5: answer anyway, but say it looks abnormal.
    assert all(issue.severity == "warn" for issue in issues)


def test_moderate_change_is_not_flagged_as_magnitude_shift():
    result = _result(["sales_revenue", "sales_revenue_comparison"], [(1200, 1000)])
    issues = validate_result(
        result,
        has_filters=False,
        comparison_columns={"sales_revenue": "sales_revenue_comparison"},
    )
    assert ValidationCode.MAGNITUDE_SHIFT not in _codes(issues)


def test_zero_baseline_does_not_raise():
    result = _result(["sales_revenue", "sales_revenue_comparison"], [(500, 0)])
    issues = validate_result(
        result,
        has_filters=False,
        comparison_columns={"sales_revenue": "sales_revenue_comparison"},
    )
    # Growth from zero is unusual but must not crash the validator.
    assert isinstance(issues, tuple)


def test_null_baseline_is_skipped():
    result = _result(["sales_revenue", "sales_revenue_comparison"], [(500, None)])
    issues = validate_result(
        result,
        has_filters=False,
        comparison_columns={"sales_revenue": "sales_revenue_comparison"},
    )
    assert ValidationCode.MAGNITUDE_SHIFT not in _codes(issues)


def test_issue_messages_are_user_facing():
    issues = validate_result(_result(["total"], []), has_filters=True)
    assert all(issue.message for issue in issues)