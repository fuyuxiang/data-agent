"""IntentV2 cross-field validation tests (S2 Task 2, Step 1).

The new intent schema with 7 cross-field validators that enforce
complete contract at validation time, not at compilation.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.intent.v2 import (
    Ambiguity,
    ComparisonKind,
    DomainCandidate,
    FilterCondition,
    FilterOperator,
    IntentKind,
    MetricRef,
    QueryIntentV2,
    SortSpec,
    TimeExpression,
    TimeExpressionKind,
    TimeUnit,
)


class TestAggregateRequiresMetrics:
    """Validator 1: aggregate must have at least one metric (P0-08)."""

    def test_aggregate_with_metrics_passes(self):
        """Aggregate with one metric is valid."""
        intent = QueryIntentV2(
            kind=IntentKind.AGGREGATE,
            dataset="orders",
            metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
        )
        assert intent.kind == IntentKind.AGGREGATE
        assert len(intent.metrics) == 1

    def test_aggregate_without_metrics_fails(self):
        """Aggregate without metrics raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.AGGREGATE,
                dataset="orders",
                metrics=[],
            )
        assert "aggregate kind requires at least one metric" in str(exc_info.value)

    def test_other_kinds_allow_empty_metrics(self):
        """Non-aggregate kinds can have empty metrics."""
        # UNSUPPORTED kind with no metrics should not raise
        intent = QueryIntentV2(
            kind=IntentKind.UNSUPPORTED,
            dataset="orders",
            metrics=[],
        )
        assert len(intent.metrics) == 0


class TestTrendRequiresMetricsAndTime:
    """Validator 2: trend must have metrics and time expression."""

    def test_trend_with_metrics_and_time_passes(self):
        """Trend with both metrics and time is valid."""
        intent = QueryIntentV2(
            kind=IntentKind.TREND,
            dataset="orders",
            metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
            time_expression=TimeExpression(
                kind=TimeExpressionKind.RELATIVE,
                text="本月",
                unit=TimeUnit.MONTH,
                offset=0,
            ),
        )
        assert intent.kind == IntentKind.TREND

    def test_trend_without_metrics_fails(self):
        """Trend without metrics raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.TREND,
                dataset="orders",
                metrics=[],
                time_expression=TimeExpression(
                    kind=TimeExpressionKind.RELATIVE,
                    text="本月",
                    unit=TimeUnit.MONTH,
                    offset=0,
                ),
            )
        assert "trend kind requires at least one metric" in str(exc_info.value)

    def test_trend_without_time_fails(self):
        """Trend without time expression raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.TREND,
                dataset="orders",
                metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
                time_expression=None,
            )
        assert "trend kind requires a time expression" in str(exc_info.value)


class TestDetailRequiresDimensions:
    """Validator 3: detail must have at least one dimension."""

    def test_detail_with_dimensions_passes(self):
        """Detail with dimensions is valid."""
        intent = QueryIntentV2(
            kind=IntentKind.DETAIL,
            dataset="orders",
            dimensions=["order_id", "customer_name"],
        )
        assert intent.kind == IntentKind.DETAIL

    def test_detail_without_dimensions_fails(self):
        """Detail without dimensions raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.DETAIL,
                dataset="orders",
                dimensions=[],
            )
        assert "detail kind requires at least one dimension" in str(exc_info.value)


class TestRankingRequiresMetricsAndDimensions:
    """Validator 4: ranking needs metrics to rank and dimensions to rank by."""

    def test_ranking_with_metrics_and_dimensions_passes(self):
        """Ranking with both metrics and dimensions is valid."""
        intent = QueryIntentV2(
            kind=IntentKind.RANKING,
            dataset="orders",
            metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
            dimensions=["region"],
        )
        assert intent.kind == IntentKind.RANKING

    def test_ranking_without_metrics_fails(self):
        """Ranking without metrics raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.RANKING,
                dataset="orders",
                metrics=[],
                dimensions=["region"],
            )
        assert "ranking kind requires at least one metric" in str(exc_info.value)

    def test_ranking_without_dimensions_fails(self):
        """Ranking without dimensions raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.RANKING,
                dataset="orders",
                metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
                dimensions=[],
            )
        assert "ranking kind requires at least one dimension" in str(exc_info.value)


class TestTimeExpressionConsistency:
    """Validator 5: time expression must be internally consistent."""

    def test_absolute_range_requires_dates(self):
        """Absolute/range time expressions must have start and end dates."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.AGGREGATE,
                dataset="orders",
                metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
                time_expression=TimeExpression(
                    kind=TimeExpressionKind.ABSOLUTE,
                    text="2026-08-01 到 2026-08-31",
                    start=None,  # Missing start
                    end=date(2026, 8, 31),
                ),
            )
        assert "requires start and end dates" in str(exc_info.value)

    def test_range_end_before_start_fails(self):
        """Range with end < start raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.AGGREGATE,
                dataset="orders",
                metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
                time_expression=TimeExpression(
                    kind=TimeExpressionKind.RANGE,
                    text="2026-08-31 到 2026-08-01",
                    start=date(2026, 8, 31),
                    end=date(2026, 8, 1),
                ),
            )
        assert "end must not precede start" in str(exc_info.value)


class TestFilterValuesNotPopulated:
    """Validator 6: model output filters must not have values populated.

    Values are a resolver product; only spoken_values come from LLM.
    """

    def test_filter_with_spoken_values_only_passes(self):
        """Filter with only spoken_values is valid."""
        intent = QueryIntentV2(
            kind=IntentKind.AGGREGATE,
            dataset="orders",
            metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
            filters=[
                FilterCondition(
                    field="region",
                    operator=FilterOperator.IN,
                    spoken_values=["华南", "华东"],
                )
            ],
        )
        assert len(intent.filters) == 1
        assert len(intent.filters[0].values) == 0

    def test_filter_with_values_fails(self):
        """Filter with values populated raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            QueryIntentV2(
                kind=IntentKind.AGGREGATE,
                dataset="orders",
                metrics=[MetricRef(name="sales_revenue", confidence=0.95)],
                filters=[
                    FilterCondition(
                        field="region",
                        operator=FilterOperator.IN,
                        values=["SC", "EC"],  # Should not be populated by model
                        spoken_values=["华南", "华东"],
                    )
                ],
            )
        assert "model output must not include values" in str(exc_info.value)


class TestPerMetricConfidence:
    """per-metric confidence support."""

    def test_metrics_carry_individual_confidence(self):
        """Each metric can have its own confidence level."""
        intent = QueryIntentV2(
            kind=IntentKind.AGGREGATE,
            dataset="orders",
            metrics=[
                MetricRef(name="sales_revenue", confidence=0.95),
                MetricRef(name="order_count", confidence=0.72),
            ],
        )
        assert intent.metrics[0].confidence == 0.95
        assert intent.metrics[1].confidence == 0.72

    def test_metric_confidence_bounds(self):
        """Metric confidence must be in [0, 1]."""
        with pytest.raises(ValidationError):
            MetricRef(name="sales_revenue", confidence=1.5)

        with pytest.raises(ValidationError):
            MetricRef(name="sales_revenue", confidence=-0.1)
