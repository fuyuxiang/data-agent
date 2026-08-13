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


# Function allow-list. Anything the compiler emits *and* anything a
# legitimate analytic query might ask for lives here; everything else is
# rejected by default. The list is deliberately narrow — a function that
# does useful business work is in; anything that talks to the OS, the
# network, or another schema is out.
_FUNCTION_ALLOWLIST: frozenset[str] = frozenset({
    # Aggregates (sqlglot emits these as exp.Sum/Count/... so they
    # never appear in find_all(exp.Anonymous); the names are listed for
    # defence-in-depth and to keep the test table honest).
    "SUM", "AVG", "COUNT", "MIN", "MAX",
    "ARRAY_AGG", "STRING_AGG", "BOOL_AND", "BOOL_OR",
    "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE",
    "PERCENTILE_CONT", "PERCENTILE_DISC",
    "COVAR_POP", "COVAR_SAMP", "CORR",
    # Math
    "ABS", "CEIL", "CEILING", "FLOOR", "ROUND", "TRUNC", "MOD", "POWER", "SQRT",
    "EXP", "LN", "LOG", "SIGN",
    # String
    "LENGTH", "CHAR_LENGTH", "OCTET_LENGTH",
    "LOWER", "UPPER", "INITCAP", "REVERSE",
    "TRIM", "BTRIM", "LTRIM", "RTRIM",
    "SUBSTRING", "SUBSTR", "REPLACE", "TRANSLATE",
    "CONCAT", "CONCAT_WS", "LEFT", "RIGHT", "LPAD", "RPAD",
    "POSITION", "STRPOS", "OVERLAY", "STARTS_WITH", "ENDS_WITH",
    "SPLIT_PART",
    "REGEXP_REPLACE", "REGEXP_MATCHES", "REGEXP_SPLIT_TO_ARRAY",
    # Date / time
    "NOW", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
    "LOCALTIME", "LOCALTIMESTAMP",
    "DATE_TRUNC", "DATE_PART", "EXTRACT",
    "TO_CHAR", "TO_DATE", "TO_TIMESTAMP", "TO_NUMBER",
    "AGE", "DATE_PLUS", "DATE_MINUS",
    "MAKE_DATE", "MAKE_TIME", "MAKE_TIMESTAMP", "MAKE_INTERVAL",
    "JUSTIFY_DAYS", "JUSTIFY_HOURS", "JUSTIFY_INTERVAL",
    # Window
    "ROW_NUMBER", "RANK", "DENSE_RANK",
    "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE", "NTILE",
    # Coercion / utility
    "COALESCE", "NULLIF", "GREATEST", "LEAST",
    # Hashing (useful for de-dup and join keys)
    "MD5", "SHA256", "ENCODE", "DECODE",
})


def assert_allowed_functions(ast: exp.Expression) -> None:
    """Reject any function call outside the allow-list.

    sqlglot recognises the well-known PostgreSQL builtins as concrete
    subclasses (exp.Sum, exp.Count, ...). Anything that lands as
    ``exp.Anonymous`` is unrecognised by sqlglot — the same bucket of
    names most likely to be hostile: ``pg_sleep``, ``pg_read_file``,
    ``dblink``, ``lo_import``, ``pg_catalog.*``, ``information_schema.*``
    extensions. We default-deny that bucket and rely on the Golden Set
    to surface any legitimate function we overlooked.
    """
    for func in ast.find_all(exp.Anonymous):
        name = (func.name or "").upper()
        if name not in _FUNCTION_ALLOWLIST:
            raise AstRejectedError(f"不允许的函数调用: {name}")


def enforce_limit(ast: exp.Expression, max_rows: int) -> exp.Expression:
    """Guarantee an upper bound on returned rows, clamping anything larger."""
    # Only Select / Union support LIMIT. Non-query trees fall through unchanged;
    # assert_select_only will reject them on its own pass.
    if not isinstance(ast, (exp.Select, exp.Union)):
        return ast

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