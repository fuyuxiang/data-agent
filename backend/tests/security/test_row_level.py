"""Row-level security injection on the compiled AST.

The contract: row policies are injected into every SELECT that reads the
physical table, ANDed with user filters, and surfaced as business-value
AppliedRowFilter records for citation. Failing closed (raising instead of
silently widening scope) is non-negotiable when a policy references a field
the dataset no longer has.
"""

from datetime import date

import pytest
import sqlglot

from app.compiler.query import compile_intent
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.security.principal import load_principal
from app.security.rewrite import inject_row_policies
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


def _rewrite(session, username, **intent_overrides):
    dataset = load_dataset(session, "orders")
    compiled = compile_intent(dataset, _intent(**intent_overrides))
    principal = load_principal(session, username)
    ast, applied = inject_row_policies(compiled.ast, dataset, principal)
    return ast.sql(dialect="postgres"), applied


def test_row_policy_is_injected_into_where(env):
    sql, applied = _rewrite(env, "east_manager")

    assert "region_code" in sql
    assert "'EC'" in sql
    assert len(applied) == 1
    assert applied[0].field_business_name == "大区"
    assert applied[0].values == ("华东",)


def test_admin_query_is_untouched(env):
    dataset = load_dataset(env, "orders")
    compiled = compile_intent(dataset, _intent())
    principal = load_principal(env, "admin")

    ast, applied = inject_row_policies(compiled.ast, dataset, principal)
    assert ast.sql(dialect="postgres") == compiled.sql_compact
    assert applied == ()


def test_policy_is_anded_not_ored_with_user_filters(env):
    from app.intent.schema import FilterCondition, FilterOperator

    sql, _ = _rewrite(
        env,
        "east_manager",
        filters=[
            FilterCondition(
                field="channel",
                operator=FilterOperator.EQ,
                values=["online"],
                spoken_values=["线上"],
            )
        ],
    )
    upper = sql.upper()
    assert " OR " not in upper
    assert upper.count(" AND ") >= 2


def test_user_cannot_widen_scope_by_filtering_another_region(env):
    """A user asking for South China while scoped to East gets an empty scope,
    never South China data."""
    from app.intent.schema import FilterCondition, FilterOperator

    sql, _ = _rewrite(
        env,
        "east_manager",
        filters=[
            FilterCondition(
                field="region_code",
                operator=FilterOperator.IN,
                values=["SC"],
                spoken_values=["华南"],
            )
        ],
    )
    # Both predicates survive; their conjunction is unsatisfiable, which is correct.
    assert "'SC'" in sql and "'EC'" in sql
    assert " OR " not in sql.upper()


def test_multiple_roles_apply_all_policies(env):
    sql, applied = _rewrite(env, "multi_role")

    assert "region_code" in sql
    assert "channel" in sql
    assert {item.field_business_name for item in applied} == {"大区", "渠道"}


def test_policy_reaches_every_cte_of_a_comparison_query(env):
    sql, _ = _rewrite(env, "east_manager", comparison=ComparisonKind.MOM)

    # Both the current and the baseline CTE must carry the restriction;
    # patching only the outer query would leak the baseline period.
    assert sql.count("region_code") >= 2


def test_policy_values_are_physical_in_sql_and_business_in_citation(env):
    sql, applied = _rewrite(env, "east_manager")
    assert "华东" not in sql
    assert applied[0].values == ("华东",)


def test_unmapped_policy_value_stays_physical_in_citation(meta_session):
    """A policy value with no dictionary entry must still be shown, not dropped."""
    from app.security.orm import RoleRow, RowPolicyRow, UserRow

    build_orders_dataset(meta_session)
    role = RoleRow(
        name="odd",
        max_sensitivity="public",
        row_policies=[
            RowPolicyRow(
                dataset_name="orders", field_name="region_code", operator="in", values=["ZZ"]
            )
        ],
    )
    meta_session.add(UserRow(username="odd_user", roles=[role]))
    meta_session.flush()

    sql, applied = _rewrite(meta_session, "odd_user")
    assert "'ZZ'" in sql
    assert applied[0].values == ("ZZ",)


def test_injected_sql_still_parses(env):
    sql, _ = _rewrite(env, "multi_role", comparison=ComparisonKind.YOY)
    assert sqlglot.parse_one(sql, dialect="postgres") is not None


def test_policy_on_field_absent_from_dataset_is_rejected(meta_session):
    from app.security.orm import RoleRow, RowPolicyRow, UserRow
    from app.security.rewrite import RowPolicyConfigError

    build_orders_dataset(meta_session)
    role = RoleRow(
        name="stale",
        max_sensitivity="public",
        row_policies=[
            RowPolicyRow(
                dataset_name="orders", field_name="deleted_field", operator="in", values=["x"]
            )
        ],
    )
    meta_session.add(UserRow(username="stale_user", roles=[role]))
    meta_session.flush()

    # Fail closed: a stale policy must never silently widen access.
    with pytest.raises(RowPolicyConfigError):
        _rewrite(meta_session, "stale_user")