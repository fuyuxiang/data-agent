"""The final gate before execution (spec M-10).

A whitelist, not a blacklist: the statement must be a SELECT (optionally with
CTEs) and every table it touches must be explicitly allowed. Anything the
whitelist does not recognise is rejected rather than assumed harmless.

This runs on the tree that will actually execute — after rewriting — so that
problems introduced by the rewrites themselves are caught too.
"""

from sqlglot import exp

_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Subquery)

_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Copy,
    exp.Command,
    exp.Into,
)


class AstRejectedError(Exception):
    """Structural rejection. The reason is for administrators and Trace."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def assert_select_only(ast: exp.Expression) -> None:
    if not isinstance(ast, _ALLOWED_ROOTS):
        raise AstRejectedError(f"仅允许 SELECT 语句，实际为 {type(ast).__name__}")

    for node_type in _FORBIDDEN_NODES:
        node = ast.find(node_type)
        if node is not None:
            raise AstRejectedError(f"语句中包含被禁止的节点 {type(node).__name__}")

    if ast.args.get("locks"):
        raise AstRejectedError("禁止使用行锁子句")


def _cte_names(ast: exp.Expression) -> set[str]:
    return {cte.alias_or_name for cte in ast.find_all(exp.CTE)}


def assert_within_dataset(ast: exp.Expression, allowed_tables: set[str]) -> None:
    """Every physical table read must be in the allow list.

    CTE references are skipped: they resolve to definitions in the same tree,
    which are themselves checked.
    """
    known_ctes = _cte_names(ast)
    normalized = {name.lower() for name in allowed_tables}

    for table in ast.find_all(exp.Table):
        if table.name in known_ctes and not table.db:
            continue
        qualified = f"{table.db}.{table.name}" if table.db else table.name
        if qualified.lower() not in normalized:
            raise AstRejectedError(f"查询访问了未授权的表 {qualified}")


def enforce_limit(ast: exp.Expression, max_rows: int) -> exp.Expression:
    """Guarantee an upper bound on returned rows, clamping anything larger."""
    limited = ast.copy()
    existing = limited.args.get("limit")

    if existing is not None:
        try:
            current = int(existing.expression.this)
        except (AttributeError, TypeError, ValueError):
            current = max_rows + 1
        if current <= max_rows:
            return limited

    return limited.limit(max_rows)