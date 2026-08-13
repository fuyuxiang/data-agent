"""Stage 5 of the pipeline: security rewrites and guardrails.

Fixed order, and the order is the point:

1. row policies   — inject before anything else reads the shape of the query
2. masking        — rewrite projections
3. forced limit   — bound the result set
4. AST whitelist  — check the tree that will actually execute
5. cost estimate  — plan the final statement, not an earlier draft

Verified Query SQL enters at the same door (spec 3.2): a stored statement that
skipped this would be a standing privilege-escalation path.
"""

from dataclasses import dataclass

import sqlglot
from sqlalchemy.engine import Connection
from sqlglot import exp

from app.compiler.query import CompiledQuery
from app.core.config import Settings
from app.security.columns import apply_masking
from app.security.guardrails import CostEstimate, assert_affordable
from app.security.principal import Principal
from app.security.rewrite import AppliedRowFilter, inject_row_policies
from app.security.whitelist import (
    AstRejectedError,
    assert_allowed_functions,
    assert_select_only,
    assert_within_dataset,
    enforce_limit,
)
from app.semantic.model import DatasetDef


@dataclass(frozen=True, slots=True)
class SecuredQuery:
    sql: str
    display_sql: str
    ast: exp.Expression
    applied_row_filters: tuple[AppliedRowFilter, ...]
    masked_field_names: tuple[str, ...]
    cost: CostEstimate
    row_limit: int


def _run(
    ast: exp.Expression,
    dataset: DatasetDef,
    principal: Principal,
    connection: Connection,
    settings: Settings,
) -> SecuredQuery:
    ast, applied = inject_row_policies(ast, dataset, principal)
    ast, masked = apply_masking(ast, dataset, principal)
    ast = enforce_limit(ast, settings.max_result_rows)

    assert_select_only(ast)
    assert_within_dataset(ast, {dataset.physical_table})
    assert_allowed_functions(ast)

    sql = ast.sql(dialect="postgres")
    cost = assert_affordable(connection, sql, settings)

    return SecuredQuery(
        sql=sql,
        display_sql=ast.sql(dialect="postgres", pretty=True),
        ast=ast,
        applied_row_filters=applied,
        masked_field_names=masked,
        cost=cost,
        row_limit=settings.max_result_rows,
    )


def secure_compiled(
    compiled: CompiledQuery,
    dataset: DatasetDef,
    principal: Principal,
    connection: Connection,
    settings: Settings,
) -> SecuredQuery:
    return _run(compiled.ast, dataset, principal, connection, settings)


def secure_verified_sql(
    sql: str,
    dataset: DatasetDef,
    principal: Principal,
    connection: Connection,
    settings: Settings,
) -> SecuredQuery:
    try:
        ast = sqlglot.parse_one(sql, dialect="postgres")
    except sqlglot.ParseError as error:
        raise AstRejectedError(f"固定 SQL 无法解析：{error}") from error
    if ast is None:
        raise AstRejectedError("固定 SQL 为空")

    return _run(ast, dataset, principal, connection, settings)