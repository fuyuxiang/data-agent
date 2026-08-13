"""Column permissions and masking.

Two distinct mechanisms:

- DENY removes the field from the semantic view entirely, so the model never
  learns a forbidden field exists at recall time.
- MASK keeps the field queryable but replaces its value in the projection.

Filtering on a masked field is refused: the value would be recoverable by
probing which filters return rows.
"""

from datetime import date

import pytest

from app.intent.schema import (
    FieldConfidence,
    FilterCondition,
    FilterOperator,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.security.columns import (
    PermissionDeniedError,
    apply_masking,
    assert_intent_permitted,
    visible_dataset,
)
from app.security.principal import load_principal
from app.semantic.loader import load_dataset
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def env(meta_session):
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


def _intent(**overrides) -> QueryIntent:
    payload = {
        "kind": IntentKind.AGGREGATE,
        "dataset": "orders",
        "metrics": ["sales_revenue"],
        "time": TimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
            grain=TimeGrain.MONTH,
            expression="本月",
        ),
        "confidence": FieldConfidence(overall=0.9),
        "raw_question": "本月销售额",
    }
    payload.update(overrides)
    return QueryIntent(**payload)


def test_denied_field_is_absent_from_visible_dataset(env):
    dataset = load_dataset(env, "orders")
    visible = visible_dataset(dataset, load_principal(env, "east_manager"))

    assert not visible.has_field("cost")
    assert visible.has_field("amount")


def test_metrics_depending_on_denied_fields_are_removed(env):
    dataset = load_dataset(env, "orders")
    visible = visible_dataset(dataset, load_principal(env, "east_manager"))
    names = {metric.name for metric in visible.metrics}

    # total_cost reads cost directly; gross_margin_rate is computed from it.
    assert "total_cost" not in names
    assert "gross_margin_rate" not in names
    assert "sales_revenue" in names


def test_admin_sees_everything(env):
    dataset = load_dataset(env, "orders")
    visible = visible_dataset(dataset, load_principal(env, "admin"))

    assert len(visible.fields) == len(dataset.fields)
    assert len(visible.metrics) == len(dataset.metrics)


def test_masked_field_stays_visible_in_the_model(env):
    dataset = load_dataset(env, "orders")
    # analyst is masked on customer_name, not denied: it is still queryable.
    visible = visible_dataset(dataset, load_principal(env, "analyst"))
    assert visible.has_field("customer_name")


def test_querying_a_denied_metric_is_refused(env):
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "east_manager")

    with pytest.raises(PermissionDeniedError):
        assert_intent_permitted(dataset, _intent(metrics=["total_cost"]), principal)


def test_grouping_by_a_denied_field_is_refused(env):
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "east_manager")

    with pytest.raises(PermissionDeniedError):
        assert_intent_permitted(dataset, _intent(dimensions=["cost"]), principal)


def test_filtering_on_a_denied_field_is_refused(env):
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "east_manager")
    intent = _intent(
        filters=[
            FilterCondition(
                field="cost", operator=FilterOperator.GT, values=["100"], spoken_values=["一百"]
            )
        ]
    )

    with pytest.raises(PermissionDeniedError):
        assert_intent_permitted(dataset, intent, principal)


def test_permission_error_leaks_no_metadata(env):
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "east_manager")

    with pytest.raises(PermissionDeniedError) as excinfo:
        assert_intent_permitted(dataset, _intent(metrics=["total_cost"]), principal)

    message = str(excinfo.value)
    for leak in ("cost", "total_cost", "orders", "sample", "region_code"):
        assert leak not in message


def test_filtering_on_a_masked_field_is_refused(env):
    """Masking hides the value; filtering on it would reveal it by inference."""
    dataset = load_dataset(env, "orders")
    principal = load_principal(env, "analyst")
    intent = _intent(
        filters=[
            FilterCondition(
                field="customer_name",
                operator=FilterOperator.EQ,
                values=["ACME"],
                spoken_values=["ACME"],
            )
        ]
    )

    with pytest.raises(PermissionDeniedError):
        assert_intent_permitted(dataset, intent, principal)


def test_permitted_intent_passes_silently(env):
    dataset = load_dataset(env, "orders")
    assert assert_intent_permitted(dataset, _intent(), load_principal(env, "admin")) is None


def test_masking_rewrites_the_projection(env):
    from app.compiler.query import compile_intent

    dataset = load_dataset(env, "orders")
    intent = _intent(kind=IntentKind.DETAIL, dimensions=["customer_name", "province"])
    compiled = compile_intent(dataset, intent)

    ast, masked = apply_masking(compiled.ast, dataset, load_principal(env, "analyst"))
    sql = ast.sql(dialect="postgres")

    assert "***" in sql
    assert masked == ("客户名称",)
    # The alias must survive so downstream column mapping still works.
    assert "customer_name" in sql


def test_masking_is_a_noop_for_permitted_columns(env):
    from app.compiler.query import compile_intent

    dataset = load_dataset(env, "orders")
    compiled = compile_intent(dataset, _intent(dimensions=["province"]))

    ast, masked = apply_masking(compiled.ast, dataset, load_principal(env, "analyst"))
    assert masked == ()
    assert ast.sql(dialect="postgres") == compiled.sql_compact