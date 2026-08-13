"""Answer assembly: number + conclusion + citation + drill-downs."""

from datetime import date, datetime

import pytest

from app.compiler.query import Citation
from app.execution.runner import QueryResult
from app.execution.validation import ValidationCode, ValidationIssue
from app.pipeline.answer import build_answer
from app.security.rewrite import AppliedRowFilter


def _citation(**overrides) -> Citation:
    payload = {
        "metric_name": "sales_revenue",
        "metric_business_name": "销售额",
        "metric_version": 3,
        "metric_description": "已完成订单含税金额",
        "time_field_business_name": "完成日期",
        "time_start": date(2026, 8, 1),
        "time_end": date(2026, 8, 12),
        "filters": ("大区 属于 华东",),
        "comparison_label": "",
    }
    payload.update(overrides)
    return Citation(**payload)


def _result(columns=("sales_revenue",), rows=((42_350_000,),)) -> QueryResult:
    return QueryResult(
        columns=tuple(columns),
        rows=tuple(rows),
        row_count=len(rows),
        truncated=False,
        elapsed_ms=12,
    )


def _build(**overrides):
    payload = {
        "citation": _citation(),
        "result": _result(),
        "metric_names": ("sales_revenue",),
        "comparison_metric_names": (),
        "dimension_names": (),
        "applied_row_filters": (),
        "masked_field_names": (),
        "issues": (),
        "assumptions": (),
        "data_updated_at": datetime(2026, 8, 12, 9, 0),
    }
    payload.update(overrides)
    return build_answer(**payload)


def test_headline_states_the_number():
    answer = _build()
    assert "42,350,000" in answer.headline or "4,235" in answer.headline


def test_citation_shows_metric_with_version():
    answer = _build()
    assert "sales_revenue" in answer.citation.metric
    assert "v3" in answer.citation.metric


def test_citation_shows_time_basis_field():
    """The same metric on a different date field is the classic two-numbers bug."""
    answer = _build()
    assert "完成日期" in answer.citation.time
    assert "2026-08-01" in answer.citation.time


def test_citation_shows_data_updated_at():
    answer = _build()
    assert "2026-08-12" in answer.citation.data_updated_at


def test_user_filters_are_marked_as_user_sourced():
    answer = _build()
    line = next(item for item in answer.citation.filters if "华东" in item.value)
    assert line.source == "user"


def test_permission_filters_are_marked_and_labelled():
    answer = _build(
        applied_row_filters=(AppliedRowFilter(field_business_name="大区", values=("华东",)),)
    )
    permission_lines = [item for item in answer.citation.filters if item.source == "permission"]

    assert permission_lines
    # Spec M-16: this must be explicit, or users draw global conclusions from
    # partial data.
    assert "数据权限自动附加" in permission_lines[0].label


def test_masked_fields_are_reported_as_a_warning():
    answer = _build(masked_field_names=("客户名称",))
    assert any("客户名称" in item for item in answer.warnings)


def test_comparison_answer_states_the_change():
    answer = _build(
        citation=_citation(comparison_label="环比"),
        result=_result(
            columns=("sales_revenue", "sales_revenue_comparison"),
            rows=((1_124_000, 1_000_000),),
        ),
        comparison_metric_names=("sales_revenue_comparison",),
    )

    assert "环比" in answer.conclusion
    assert "12.4%" in answer.conclusion


def test_comparison_with_zero_baseline_does_not_divide():
    answer = _build(
        citation=_citation(comparison_label="环比"),
        result=_result(
            columns=("sales_revenue", "sales_revenue_comparison"), rows=((500, 0),)
        ),
        comparison_metric_names=("sales_revenue_comparison",),
    )
    assert answer.conclusion


def test_dimension_answer_names_top_contributors():
    answer = _build(
        result=_result(
            columns=("province", "sales_revenue"),
            rows=(("江苏", 300), ("浙江", 200), ("上海", 100)),
        ),
        dimension_names=("province",),
    )

    assert "江苏" in answer.conclusion


def test_assumptions_appear_in_the_answer():
    answer = _build(assumptions=("指标口径未确认，已默认按「销售额」处理",))
    # Spec 5.2: an unstated default assumption is a silent error.
    assert any("默认" in item for item in answer.assumptions)


def test_warning_level_issues_are_surfaced():
    issues = (
        ValidationIssue(ValidationCode.MAGNITUDE_SHIFT, "warn", "变化超过 10 倍，建议核对口径"),
    )
    answer = _build(issues=issues)
    assert any("10 倍" in item for item in answer.warnings)


def test_blocking_issue_is_rejected_before_answering():
    from app.pipeline.answer import ResultNotAnswerableError

    issues = (ValidationIssue(ValidationCode.EMPTY_RESULT, "block", "该时间范围内没有数据"),)
    with pytest.raises(ResultNotAnswerableError):
        _build(issues=issues)


def test_drill_downs_offer_unused_dimensions():
    answer = _build(available_dimensions=("province", "channel"))
    labels = {item.label for item in answer.drill_downs}

    assert any("省份" in label or "province" in label for label in labels)


def test_drill_downs_exclude_dimensions_already_grouped():
    answer = _build(
        dimension_names=("province",),
        result=_result(columns=("province", "sales_revenue"), rows=(("江苏", 300),)),
        available_dimensions=("province", "channel"),
    )
    targets = {item.target for item in answer.drill_downs}
    assert "province" not in targets


def test_rows_and_columns_are_carried_for_the_result_table():
    answer = _build(
        result=_result(columns=("province", "sales_revenue"), rows=(("江苏", 300),))
    )
    assert answer.columns == ("province", "sales_revenue")
    assert answer.rows == (("江苏", 300),)