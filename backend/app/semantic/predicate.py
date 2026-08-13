"""Predicate DSL for structured filters (S4 P1-06).

Replaces free-form SQL strings (MetricDef.fixed_filter) with a typed
expression tree. Three benefits:

1. **No subqueries**: only field, operator, literal_value combinations.
2. **Field lineage**: fields mentioned in predicates automatically contribute
   to required_field_lineage, so column permissions apply.
3. **No arbitrary functions**: operator enum is closed; values are typed.

DSL structure:

    Predicate = Comparison(field, op, value)
              | And(left, right)
              | Or(left, right)
              | Not(inner)

Limitations (intentional): complex CASE / sub-select are out of scope. These
were the column-permission bypass path under the old fixed_filter string.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Union

from sqlglot import exp


class PredicateOp(str, Enum):
    """Closed set of allowed operators.

    Adding a new operator here is the only way to extend the DSL; the SQL
    compiler refuses anything else.
    """

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    LIKE = "like"


@dataclass(frozen=True)
class Comparison:
    """field op value: e.g., status = 'active'"""

    field: str
    op: PredicateOp
    value: Any = None  # None for is_null/is_not_null


@dataclass(frozen=True)
class And:
    """Conjunction: left AND right"""

    left: "Predicate"
    right: "Predicate"


@dataclass(frozen=True)
class Or:
    """Disjunction: left OR right"""

    left: "Predicate"
    right: "Predicate"


@dataclass(frozen=True)
class Not:
    """Negation: NOT inner"""

    inner: "Predicate"


Predicate = Union[Comparison, And, Or, Not]


# --- Validation -----------------------------------------------------------

class PredicateError(Exception):
    """Raised when a predicate is invalid (e.g., IN with non-list value)."""


def validate(predicate: Predicate, known_fields: set[str]) -> None:
    """Validate a predicate references only known fields and uses correct value types.

    Raises PredicateError on:
    - Unknown field name
    - IN / NOT_IN with non-list value
    - IS_NULL / IS_NOT_NULL with non-None value
    - Other ops with None value
    """
    if isinstance(predicate, Comparison):
        if predicate.field not in known_fields:
            raise PredicateError(f"Unknown field: {predicate.field}")
        if predicate.op in (PredicateOp.IN, PredicateOp.NOT_IN):
            if not isinstance(predicate.value, (list, tuple)):
                raise PredicateError(
                    f"{predicate.op.value} requires a list value, got {type(predicate.value).__name__}"
                )
        elif predicate.op in (PredicateOp.IS_NULL, PredicateOp.IS_NOT_NULL):
            if predicate.value is not None:
                raise PredicateError(
                    f"{predicate.op.value} requires no value, got {predicate.value!r}"
                )
        else:
            if predicate.value is None:
                raise PredicateError(
                    f"{predicate.op.value} requires a non-None value"
                )
    elif isinstance(predicate, (And, Or)):
        validate(predicate.left, known_fields)
        validate(predicate.right, known_fields)
    elif isinstance(predicate, Not):
        validate(predicate.inner, known_fields)
    else:
        raise PredicateError(f"Unknown predicate type: {type(predicate).__name__}")


# --- Field extraction ------------------------------------------------------

def referenced_fields(predicate: Predicate) -> frozenset[str]:
    """Return all field names this predicate references (for lineage)."""
    if isinstance(predicate, Comparison):
        return frozenset({predicate.field})
    if isinstance(predicate, And) or isinstance(predicate, Or):
        return referenced_fields(predicate.left) | referenced_fields(predicate.right)
    if isinstance(predicate, Not):
        return referenced_fields(predicate.inner)
    return frozenset()


# --- SQL compilation -------------------------------------------------------

_OP_TO_SQLGLOT: dict[PredicateOp, str] = {
    PredicateOp.EQ: "EQ",
    PredicateOp.NE: "NEQ",
    PredicateOp.LT: "LT",
    PredicateOp.LTE: "LTE",
    PredicateOp.GT: "GT",
    PredicateOp.GTE: "GTE",
    PredicateOp.IN: "IN",
    PredicateOp.NOT_IN: "NOT_IN",
    PredicateOp.IS_NULL: "IS",
    PredicateOp.IS_NOT_NULL: "IS_NOT",
    PredicateOp.LIKE: "LIKE",
}


def _comparison_to_sqlglot(comp: Comparison) -> exp.Expression:
    """Convert a Comparison to a sqlglot boolean expression."""
    column = exp.column(comp.field)

    if comp.op in (PredicateOp.IS_NULL, PredicateOp.IS_NOT_NULL):
        is_expr = exp.Is(this=column, expression=exp.Null())
        if comp.op == PredicateOp.IS_NOT_NULL:
            return exp.Not(this=is_expr)
        return is_expr

    if comp.op in (PredicateOp.IN, PredicateOp.NOT_IN):
        values = [exp.Literal.string(str(v)) if isinstance(v, str) else exp.Literal.number(v) for v in comp.value]
        in_expr = exp.In(this=column, expressions=values)
        if comp.op == PredicateOp.NOT_IN:
            return exp.Not(this=in_expr)
        return in_expr

    if comp.op == PredicateOp.LIKE:
        return exp.Like(this=column, expression=exp.Literal.string(str(comp.value)))

    # EQ / NE / LT / LTE / GT / GTE
    if isinstance(comp.value, str):
        literal: exp.Expression = exp.Literal.string(comp.value)
    elif isinstance(comp.value, bool):
        literal = exp.Boolean(this=comp.value)
    elif isinstance(comp.value, (int, float)):
        literal = exp.Literal.number(comp.value)
    elif comp.value is None:
        literal = exp.Null()
    else:
        raise PredicateError(f"Unsupported value type: {type(comp.value).__name__}")

    op_name = _OP_TO_SQLGLOT[comp.op]
    return getattr(exp, op_name)(this=column, expression=literal)


def to_sqlglot(predicate: Predicate) -> exp.Expression:
    """Compile a Predicate to a sqlglot boolean expression.

    Use as the body of an exp.Where() in a SELECT statement.
    """
    if isinstance(predicate, Comparison):
        return _comparison_to_sqlglot(predicate)
    if isinstance(predicate, And):
        return exp.And(
            this=to_sqlglot(predicate.left),
            expression=to_sqlglot(predicate.right),
        )
    if isinstance(predicate, Or):
        return exp.Or(
            this=to_sqlglot(predicate.left),
            expression=to_sqlglot(predicate.right),
        )
    if isinstance(predicate, Not):
        return exp.Not(this=to_sqlglot(predicate.inner))
    raise PredicateError(f"Unknown predicate type: {type(predicate).__name__}")


# --- Parsing (from dict) ---------------------------------------------------

def from_dict(data: dict) -> Predicate:
    """Build a Predicate from a JSON-serialisable dict.

    Format examples:
    - {"field": "status", "op": "eq", "value": "active"}
    - {"and": [{"field": "x", "op": "gt", "value": 0},
                {"field": "y", "op": "in", "value": ["a", "b"]}]}
    - {"not": {"field": "z", "op": "is_null"}}
    """
    if "field" in data:
        return Comparison(
            field=data["field"],
            op=PredicateOp(data["op"]),
            value=data.get("value"),
        )
    if "and" in data:
        items = data["and"]
        if len(items) < 2:
            raise PredicateError("'and' requires at least 2 predicates")
        result = from_dict(items[0])
        for item in items[1:]:
            result = And(result, from_dict(item))
        return result
    if "or" in data:
        items = data["or"]
        if len(items) < 2:
            raise PredicateError("'or' requires at least 2 predicates")
        result = from_dict(items[0])
        for item in items[1:]:
            result = Or(result, from_dict(item))
        return result
    if "not" in data:
        return Not(from_dict(data["not"]))
    raise PredicateError(f"Unknown predicate dict: {data}")


# --- Compatibility shims ---------------------------------------------------

def from_legacy_filter(legacy: str, known_fields: set[str]) -> Predicate:
    """Best-effort conversion from old fixed_filter string to Predicate.

    Supports the simple cases the old filter covered:
    - "field = 'value'"
    - "field IN ('a', 'b')"
    - "field IS NULL"

    Returns None when the legacy form is too complex to translate safely.
    The semantic revision publish gate then refuses to release metrics
    that still carry the old string.
    """
    import re

    s = legacy.strip()
    if not s:
        raise PredicateError("Empty legacy filter")

    # Refuse subqueries — DSL does not support them and they were the
    # primary column-permission bypass path.
    if re.search(r"\bSELECT\b", s, re.IGNORECASE):
        raise PredicateError(f"Subqueries are not allowed: {legacy!r}")

    # IS NULL / IS NOT NULL
    m = re.match(r"^(\w+)\s+IS\s+(NOT\s+)?NULL$", s, re.IGNORECASE)
    if m:
        field, neg = m.group(1), m.group(2)
        return Comparison(
            field=field,
            op=PredicateOp.IS_NOT_NULL if neg else PredicateOp.IS_NULL,
        )

    # IN / NOT IN
    m = re.match(r"^(\w+)\s+(NOT\s+)?IN\s*\(([^()]*)\)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        field, neg, body = m.group(1), m.group(2), m.group(3)
        items = [v.strip().strip("'\"") for v in body.split(",") if v.strip()]
        return Comparison(
            field=field,
            op=PredicateOp.NOT_IN if neg else PredicateOp.IN,
            value=tuple(items),
        )

    # field op value (eq/ne/lt/lte/gt/gte/like)
    m = re.match(r"^(\w+)\s*(=|!=|<>|>=|<=|>|<|LIKE)\s*(.+)$", s, re.IGNORECASE)
    if m:
        field, op_str, value_str = m.group(1), m.group(2), m.group(3).strip()
        op_map = {
            "=": PredicateOp.EQ, "!=": PredicateOp.NE, "<>": PredicateOp.NE,
            ">": PredicateOp.GT, ">=": PredicateOp.GTE,
            "<": PredicateOp.LT, "<=": PredicateOp.LTE,
            "LIKE": PredicateOp.LIKE,
        }
        op = op_map[op_str.upper() if op_str.upper() == "LIKE" else op_str]

        # Coerce value
        value: Any
        if value_str.startswith("'") and value_str.endswith("'"):
            value = value_str[1:-1]
        elif value_str.lower() == "true":
            value = True
        elif value_str.lower() == "false":
            value = False
        else:
            try:
                value = int(value_str)
            except ValueError:
                try:
                    value = float(value_str)
                except ValueError:
                    raise PredicateError(
                        f"Cannot coerce legacy filter value: {value_str!r}"
                    )

        return Comparison(field=field, op=op, value=value)

    raise PredicateError(f"Cannot translate legacy filter: {legacy!r}")
