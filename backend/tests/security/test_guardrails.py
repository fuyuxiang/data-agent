"""Cost guardrails via EXPLAIN (FORMAT JSON) without ANALYZE."""

import pytest

from app.core.config import Settings
from app.security.guardrails import (
    CostVerdict,
    QueryTooExpensiveError,
    assert_affordable,
    estimate_cost,
)


def _settings(**overrides) -> Settings:
    base = {"cost_warn_rows": 100, "cost_reject_rows": 1000}
    base.update(overrides)
    return Settings(**base)


def test_small_query_passes(sample_conn):
    estimate = estimate_cost(sample_conn, "SELECT * FROM sample.orders", _settings())

    assert estimate.verdict == CostVerdict.PASS
    assert estimate.estimated_rows > 0
    assert estimate.estimated_cost > 0


def test_estimate_does_not_execute_the_query(sample_conn):
    """EXPLAIN without ANALYZE must not touch rows.

    The division is written against columns rather than constants so the planner
    cannot fold it: only actual execution would divide by zero.
    """
    estimate = estimate_cost(
        sample_conn,
        "SELECT amount / (quantity - quantity) FROM sample.orders",
        _settings(),
    )
    assert estimate.verdict in (CostVerdict.PASS, CostVerdict.WARN)


def test_query_over_warn_threshold_warns(sample_conn):
    estimate = estimate_cost(
        sample_conn, "SELECT * FROM sample.orders", _settings(cost_warn_rows=1)
    )

    assert estimate.verdict == CostVerdict.WARN
    assert estimate.message


def test_query_over_reject_threshold_is_rejected(sample_conn):
    estimate = estimate_cost(
        sample_conn,
        "SELECT * FROM sample.orders",
        _settings(cost_warn_rows=1, cost_reject_rows=2),
    )
    assert estimate.verdict == CostVerdict.REJECT


def test_assert_affordable_raises_on_reject(sample_conn):
    settings = _settings(cost_warn_rows=1, cost_reject_rows=2)

    with pytest.raises(QueryTooExpensiveError) as excinfo:
        assert_affordable(sample_conn, "SELECT * FROM sample.orders", settings)

    assert excinfo.value.estimate.verdict == CostVerdict.REJECT


def test_assert_affordable_returns_estimate_on_warn(sample_conn):
    estimate = assert_affordable(
        sample_conn, "SELECT * FROM sample.orders", _settings(cost_warn_rows=1)
    )
    assert estimate.verdict == CostVerdict.WARN


def test_aggregate_query_estimates_one_row(sample_conn):
    estimate = estimate_cost(
        sample_conn, "SELECT SUM(amount) FROM sample.orders", _settings()
    )
    assert estimate.estimated_rows == 1


def test_unparseable_plan_fails_closed(sample_conn):
    """If the plan cannot be read, reject rather than assume it is cheap."""
    with pytest.raises(QueryTooExpensiveError):
        assert_affordable(sample_conn, "SELECT * FROM sample.no_such_table", _settings())