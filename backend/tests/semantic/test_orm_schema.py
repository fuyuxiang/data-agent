from app.semantic.enums import Aggregation, MetricKind, SemanticType
from app.semantic.orm import DatasetRow, FieldRow, MetricRow


def test_dataset_has_forbidden_scenario_column():
    # Spec M-01: forbidden_scenario is the only mechanism that stops the agent
    # from computing finance-confirmed revenue off an orders table.
    assert "forbidden_scenario" in DatasetRow.__table__.columns


def test_field_carries_allowed_aggregations():
    assert "allowed_aggregations" in FieldRow.__table__.columns
    assert "default_aggregation" in FieldRow.__table__.columns


def test_metric_requires_time_field():
    # Spec 4.4: a metric must declare which date column it is measured on.
    assert MetricRow.__table__.columns["time_field"].nullable is False


def test_enums_cover_spec_values():
    assert SemanticType.AMOUNT.value == "amount"
    assert Aggregation.DISTINCT_COUNT.value == "distinct_count"
    assert MetricKind.RATIO.value == "ratio"