from dataclasses import replace

from app.semantic.enums import Aggregation, AggregationBehavior, MetricKind, SemanticType
from app.semantic.lint import is_publishable, lint_dataset
from app.semantic.loader import load_dataset
from app.semantic.model import DatasetDef, FieldDef, MetricDef
from tests.semantic.factories import build_orders_dataset


def _codes(dataset: DatasetDef) -> set[str]:
    return {issue.code for issue in lint_dataset(dataset)}


def test_well_configured_dataset_passes(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    assert lint_dataset(dataset) == []
    assert is_publishable(dataset) is True


def test_field_without_business_name_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_fields = tuple(
        replace(item, business_name="") if item.name == "amount" else item
        for item in dataset.fields
    )
    dataset = replace(dataset, fields=broken_fields)

    assert "FIELD_NO_BUSINESS_NAME" in _codes(dataset)
    assert is_publishable(dataset) is False


def test_enum_field_without_dictionary_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_fields = tuple(
        replace(item, enum_values=()) if item.name == "region_code" else item
        for item in dataset.fields
    )
    dataset = replace(dataset, fields=broken_fields)

    assert "ENUM_NO_DICTIONARY" in _codes(dataset)


def test_metric_referencing_missing_field_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_metrics = tuple(
        replace(item, source_field="deleted_column") if item.name == "sales_revenue" else item
        for item in dataset.metrics
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "METRIC_BAD_FIELD_REF" in _codes(dataset)


def test_metric_referencing_missing_metric_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_metrics = tuple(
        replace(item, expression="(sales_revenue - ghost_metric) / sales_revenue")
        if item.name == "gross_margin_rate"
        else item
        for item in dataset.metrics
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "METRIC_BAD_METRIC_REF" in _codes(dataset)


def test_metric_with_disallowed_aggregation_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    # province does not allow SUM; a metric summing it must be rejected.
    broken_metrics = (
        MetricDef(
            name="bad_metric",
            business_name="错误指标",
            kind=MetricKind.ATOMIC.value,
            time_field="completed_date",
            source_field="province",
            aggregation=Aggregation.SUM.value,
        ),
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "METRIC_AGG_NOT_ALLOWED" in _codes(dataset)


def test_ratio_metric_marked_additive_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_metrics = tuple(
        replace(item, aggregation_behavior=AggregationBehavior.ADDITIVE.value)
        if item.name == "gross_margin_rate"
        else item
        for item in dataset.metrics
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "RATIO_METRIC_ADDITIVE" in _codes(dataset)


def test_metric_time_field_must_be_a_date_field(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    broken_metrics = tuple(
        replace(item, time_field="province") if item.name == "sales_revenue" else item
        for item in dataset.metrics
    )
    dataset = replace(dataset, metrics=broken_metrics)

    assert "METRIC_NO_TIME_FIELD" in _codes(dataset)


def test_dataset_without_grain_is_flagged(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")
    dataset = replace(dataset, grain="")

    assert "DATASET_NO_GRAIN" in _codes(dataset)
