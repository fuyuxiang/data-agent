"""Security pipeline: composes row/mask/limit/whitelist/cost in fixed order."""

from datetime import date

import pytest

from app.compiler.query import compile_intent
from app.core.config import Settings
from app.intent.schema import (
    ComparisonKind,
    FieldConfidence,
    IntentKind,
    QueryIntent,
    TimeGrain,
    TimeRange,
)
from app.security.pipeline import secure_compiled, secure_verified_sql
from app.security.principal import load_principal
from app.security.whitelist import AstRejectedError
from app.semantic.loader import load_dataset
from tests.security.factories import build_principals, user_id_for
from tests.semantic.factories import build_orders_dataset


@pytest.fixture
def env(meta_session):
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


def _settings(**overrides) -> Settings:
    base = {"max_result_rows": 1000, "cost_warn_rows": 10_000, "cost_reject_rows": 100_000}
    base.update(overrides)
    return Settings(**base)


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


def _secure(session, sample_conn, user_id, **intent_overrides):
    dataset = load_dataset(session, "orders")
    compiled = compile_intent(dataset, _intent(**intent_overrides))
    principal = load_principal(session, user_id)
    return secure_compiled(compiled, dataset, principal, sample_conn, _settings())


def test_pipeline_applies_row_policy_and_limit(env, sample_conn):
    secured = _secure(env, sample_conn, user_id_for(env, "east_manager"))

    assert "'EC'" in secured.sql
    assert "LIMIT 1000" in secured.sql.upper()
    assert secured.row_limit == 1000
    assert secured.applied_row_filters[0].field_business_name == "大区"


def test_pipeline_reports_masked_fields(env, sample_conn):
    secured = _secure(
        env, sample_conn, user_id_for(env, "analyst"), kind=IntentKind.DETAIL, dimensions=["customer_name"]
    )
    assert secured.masked_field_names == ("客户名称",)
    assert "***" in secured.sql


def test_pipeline_output_is_executable(env, sample_conn):
    from sqlalchemy import text

    secured = _secure(env, sample_conn, user_id_for(env, "east_manager"), dimensions=["province"])
    rows = sample_conn.execute(text(secured.sql)).fetchall()
    assert rows is not None


def test_comparison_query_survives_the_full_pipeline(env, sample_conn):
    from sqlalchemy import text

    secured = _secure(env, sample_conn, user_id_for(env, "east_manager"), comparison=ComparisonKind.MOM)
    assert secured.sql.count("region_code") >= 2
    sample_conn.execute(text(secured.sql)).fetchall()


def test_cost_estimate_is_attached(env, sample_conn):
    secured = _secure(env, sample_conn, user_id_for(env, "admin"))
    assert secured.cost.estimated_rows >= 0


def test_expensive_query_is_rejected_by_pipeline(env, sample_conn):
    from app.security.guardrails import QueryTooExpensiveError

    dataset = load_dataset(env, "orders")
    compiled = compile_intent(dataset, _intent())
    principal = load_principal(env, user_id_for(env, "admin"))

    with pytest.raises(QueryTooExpensiveError):
        secure_compiled(
            compiled,
            dataset,
            principal,
            sample_conn,
            _settings(cost_warn_rows=1, cost_reject_rows=1),
        )


def test_verified_sql_also_gets_row_policy(env, sample_conn):
    """The whole point of routing Verified Query through the same pipeline."""
    secured = secure_verified_sql(
        "SELECT SUM(amount) AS sales_revenue FROM sample.orders",
        load_dataset(env, "orders"),
        load_principal(env, user_id_for(env, "east_manager")),
        sample_conn,
        _settings(),
    )

    assert "region_code" in secured.sql
    assert "'EC'" in secured.sql


def test_verified_sql_touching_another_table_is_rejected(env, sample_conn):
    with pytest.raises(AstRejectedError):
        secure_verified_sql(
            "SELECT 1 FROM finance.revenue",
            load_dataset(env, "orders"),
            load_principal(env, user_id_for(env, "admin")),
            sample_conn,
            _settings(),
        )


def test_verified_sql_with_dml_is_rejected(env, sample_conn):
    with pytest.raises(AstRejectedError):
        secure_verified_sql(
            "DELETE FROM sample.orders",
            load_dataset(env, "orders"),
            load_principal(env, user_id_for(env, "admin")),
            sample_conn,
            _settings(),
        )


def test_display_sql_is_pretty_and_matches_executed_sql(env, sample_conn):
    import sqlglot

    secured = _secure(env, sample_conn, user_id_for(env, "east_manager"))
    left = sqlglot.parse_one(secured.sql, dialect="postgres")
    right = sqlglot.parse_one(secured.display_sql, dialect="postgres")

    assert "\n" in secured.display_sql
    # What the user is shown must be the statement that ran.
    assert left.sql(dialect="postgres") == right.sql(dialect="postgres")


def test_whitelist_runs_after_rewrites(env, sample_conn):
    """Order matters: the checked tree must be the one that executes."""
    secured = _secure(env, sample_conn, user_id_for(env, "east_manager"))
    assert "'EC'" in secured.sql
    assert "LIMIT" in secured.sql.upper()