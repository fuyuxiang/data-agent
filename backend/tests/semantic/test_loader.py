import pytest

from app.semantic.enums import Aggregation, MetricKind
from app.semantic.loader import load_dataset
from app.semantic.model import UnknownFieldError, UnknownMetricError
from tests.semantic.factories import build_orders_dataset


def test_load_dataset_returns_fields_and_metrics(meta_session):
    build_orders_dataset(meta_session)

    dataset = load_dataset(meta_session, "orders")

    assert dataset.physical_table == "sample.orders"
    assert dataset.field("amount").semantic_type == "amount"
    assert dataset.metric("sales_revenue").kind == MetricKind.ATOMIC.value


def test_field_lookup_rejects_unknown_name(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    with pytest.raises(UnknownFieldError):
        dataset.field("no_such_field")


def test_metric_lookup_rejects_unknown_name(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    with pytest.raises(UnknownMetricError):
        dataset.metric("no_such_metric")


def test_resolve_enum_maps_business_value_to_physical(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    assert dataset.resolve_enum("region_code", "华东") == "EC"
    assert dataset.resolve_enum("region_code", "华东地区") == "EC"
    assert dataset.resolve_enum("region_code", "火星") is None


def test_allowed_aggregations_exclude_sum_for_ratio_like_fields(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    assert Aggregation.SUM.value in dataset.field("amount").allowed_aggregations
    assert Aggregation.SUM.value not in dataset.field("province").allowed_aggregations


def test_model_is_immutable(meta_session):
    build_orders_dataset(meta_session)
    dataset = load_dataset(meta_session, "orders")

    with pytest.raises(Exception):
        dataset.field("amount").business_name = "tampered"
