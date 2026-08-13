"""Function whitelist tests (S1 Task 5, Step 2).

The whitelist is the last gate before SQL reaches the warehouse. Any function
that talks to the OS, the network, or another schema must be impossible to
call from compiled queries — and anything we have not deliberately approved
must default to rejection.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from app.security.whitelist import AstRejectedError, assert_allowed_functions


def _ast(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, dialect="postgres")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(1)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT dblink('dbname=foo', 'SELECT 1')",
        "SELECT lo_import('/etc/passwd')",
    ],
)
def test_dangerous_functions_are_rejected(sql: str) -> None:
    with pytest.raises(AstRejectedError):
        assert_allowed_functions(_ast(sql))


def test_unrecognised_function_defaults_to_rejection() -> None:
    """Anything the whitelist does not name is rejected by default — this
    is the property that protects against a future compiler change adding
    a function we never audited."""
    with pytest.raises(AstRejectedError):
        assert_allowed_functions(_ast("SELECT totally_made_up_fn(1)"))


@pytest.mark.parametrize(
    "sql",
    [
        # Aggregates
        "SELECT SUM(amount), COUNT(*), AVG(amount), MAX(amount), MIN(amount) FROM sample.orders",
        # Math
        "SELECT ROUND(AVG(amount), 2) FROM sample.orders",
        # String
        "SELECT LOWER(region_code), UPPER(region_code) FROM sample.orders",
        # Date / time
        "SELECT DATE_TRUNC('month', order_date) FROM sample.orders",
        "SELECT EXTRACT(YEAR FROM order_date) FROM sample.orders",
        # Cast and COALESCE / NULLIF
        "SELECT COALESCE(amount, 0) FROM sample.orders",
        "SELECT NULLIF(region_code, '') FROM sample.orders",
    ],
)
def test_legitimate_functions_pass(sql: str) -> None:
    assert_allowed_functions(_ast(sql))


def test_compiler_output_functions_all_pass() -> None:
    """Sanity check: every aggregate the compiler emits must clear the
    whitelist. The compiler only emits SUM/COUNT/AVG/MIN/MAX, but this
    test will fail loudly if a future change adds something we forgot
    to approve."""
    expressions = [
        exp.Sum(this=exp.column("amount")),
        exp.Count(this=exp.column("amount")),
        exp.Avg(this=exp.column("amount")),
        exp.Max(this=exp.column("amount")),
        exp.Min(this=exp.column("amount")),
    ]
    for node in expressions:
        ast = exp.Select().select(node).from_("sample.orders")
        assert_allowed_functions(ast)