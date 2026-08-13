"""AST whitelist: the final gate before execution.

Whitelist, not blacklist: the statement must be a SELECT (optionally with
CTEs) and every physical table it touches must be explicitly allowed.
"""

import pytest
import sqlglot

from app.security.whitelist import (
    AstRejectedError,
    assert_select_only,
    assert_within_dataset,
    enforce_limit,
)


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect="postgres")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE sample.orders",
        "DELETE FROM sample.orders",
        "UPDATE sample.orders SET amount = 0",
        "INSERT INTO sample.orders (amount) VALUES (1)",
        "TRUNCATE sample.orders",
        "ALTER TABLE sample.orders ADD COLUMN x int",
        "CREATE TABLE t AS SELECT 1",
        "GRANT SELECT ON sample.orders TO public",
        "COPY sample.orders TO '/tmp/x.csv'",
    ],
)
def test_ddl_and_dml_are_rejected(sql):
    with pytest.raises(AstRejectedError):
        assert_select_only(_parse(sql))


def test_plain_select_is_accepted():
    assert assert_select_only(_parse("SELECT SUM(amount) FROM sample.orders")) is None


def test_cte_select_is_accepted():
    sql = "WITH a AS (SELECT amount FROM sample.orders) SELECT SUM(amount) FROM a"
    assert assert_select_only(_parse(sql)) is None


def test_multiple_statements_are_rejected():
    # A stacked statement is the classic injection shape.
    statements = sqlglot.parse("SELECT 1; DROP TABLE sample.orders", dialect="postgres")
    with pytest.raises(AstRejectedError):
        for statement in statements:
            assert_select_only(statement)


def test_select_into_is_rejected():
    with pytest.raises(AstRejectedError):
        assert_select_only(_parse("SELECT * INTO backup FROM sample.orders"))


def test_locking_clause_is_rejected():
    with pytest.raises(AstRejectedError):
        assert_select_only(_parse("SELECT amount FROM sample.orders FOR UPDATE"))


def test_query_outside_allowed_tables_is_rejected():
    ast = _parse("SELECT * FROM finance.revenue")
    with pytest.raises(AstRejectedError):
        assert_within_dataset(ast, {"sample.orders"})


def test_query_on_allowed_table_passes():
    ast = _parse("SELECT SUM(amount) FROM sample.orders")
    assert assert_within_dataset(ast, {"sample.orders"}) is None


def test_cte_names_are_not_mistaken_for_tables():
    sql = "WITH current_period AS (SELECT amount FROM sample.orders) SELECT * FROM current_period"
    assert assert_within_dataset(_parse(sql), {"sample.orders"}) is None


def test_union_reaching_another_table_is_rejected():
    sql = "SELECT amount FROM sample.orders UNION ALL SELECT amount FROM finance.revenue"
    with pytest.raises(AstRejectedError):
        assert_within_dataset(_parse(sql), {"sample.orders"})


def test_subquery_reaching_another_table_is_rejected():
    sql = "SELECT * FROM sample.orders WHERE amount > (SELECT MAX(amount) FROM finance.revenue)"
    with pytest.raises(AstRejectedError):
        assert_within_dataset(_parse(sql), {"sample.orders"})


def test_limit_is_added_when_absent():
    ast = enforce_limit(_parse("SELECT amount FROM sample.orders"), 1000)
    assert "LIMIT 1000" in ast.sql(dialect="postgres").upper()


def test_existing_smaller_limit_is_kept():
    ast = enforce_limit(_parse("SELECT amount FROM sample.orders LIMIT 10"), 1000)
    assert "LIMIT 10" in ast.sql(dialect="postgres").upper()


def test_oversized_limit_is_clamped():
    ast = enforce_limit(_parse("SELECT amount FROM sample.orders LIMIT 999999"), 1000)
    upper = ast.sql(dialect="postgres").upper()
    assert "LIMIT 1000" in upper
    assert "999999" not in upper


def test_rejection_reason_is_admin_facing():
    with pytest.raises(AstRejectedError) as excinfo:
        assert_select_only(_parse("DROP TABLE sample.orders"))
    assert excinfo.value.reason